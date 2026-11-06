//! Evaluations. Start small: a set of ~20 queries that look like real use shows
//! the effect of a prompt change long before a large benchmark would. Each case
//! is researched end to end, then graded by the judge; results go to
//! `research-data/evals/<id>/` as `results.jsonl` and `summary.md`.
//!
//! Eval file format (JSON Lines): `{"id": "...", "query": "...", "expected": "...optional...", "kind": "simple|comparison|breadth"}`

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::agent::Env;
use crate::budget::Budget;
use crate::judge::{judge, Grade, JudgeInput};
use crate::llm::Model;
use crate::research::{ResearchSystem, RunState};
use crate::store::RunStore;
use crate::trace::Tracer;
use crate::util::{atomic_write, new_id, now_iso};

pub const STARTER_SET: &str = include_str!("../evals/starter.jsonl");

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EvalCase {
    pub id: String,
    pub query: String,
    #[serde(default)]
    pub expected: Option<String>,
    #[serde(default)]
    pub kind: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EvalResult {
    pub case: EvalCase,
    pub run_id: Option<String>,
    pub grade: Option<Grade>,
    pub error: Option<String>,
    pub usd: f64,
    pub tool_calls: u64,
    pub subagents: usize,
    pub seconds: u64,
}

pub fn parse_cases(text: &str) -> anyhow::Result<Vec<EvalCase>> {
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with("//") && !l.starts_with('#'))
        .enumerate()
        .map(|(i, l)| {
            serde_json::from_str(l).map_err(|e| anyhow::anyhow!("eval line {}: {e}", i + 1))
        })
        .collect()
}

pub fn load_cases(path: Option<&Path>) -> anyhow::Result<Vec<EvalCase>> {
    match path {
        Some(p) => parse_cases(&std::fs::read_to_string(p)?),
        None => parse_cases(STARTER_SET),
    }
}

/// Grade a finished run.
pub async fn judge_run(
    sys: &ResearchSystem,
    judge_model: Arc<dyn Model>,
    run_id: &str,
    expected: Option<&str>,
) -> anyhow::Result<Grade> {
    let store = RunStore::open(&sys.config.runs_dir(), run_id)?;
    let meta = store.meta()?;
    let state: RunState = store.read_json("state.json")?;
    let report = store
        .report()
        .ok_or_else(|| anyhow::anyhow!("run {run_id} has no report yet"))?;
    let env = Env {
        budget: Arc::new(Budget::unlimited()),
        tracer: Arc::new(Tracer::null()),
        max_retries: sys.config.max_retries,
    };
    let input = JudgeInput {
        query: &meta.query,
        expected,
        report: &report,
        spend: &state.spend,
        subagents: state.subagents.len(),
    };
    let grade = judge(judge_model, &env, &sys.prompts.judge, &input).await?;
    store.write_json("grade.json", &grade)?;
    Ok(grade)
}

/// Research and grade every case, one after another.
pub async fn run_eval(
    sys: &ResearchSystem,
    judge_model: Arc<dyn Model>,
    cases: &[EvalCase],
    out_dir: Option<PathBuf>,
) -> anyhow::Result<(PathBuf, Vec<EvalResult>)> {
    let dir = out_dir.unwrap_or_else(|| sys.config.data_dir.join("evals").join(new_id()));
    std::fs::create_dir_all(&dir)?;
    let mut results = Vec::new();
    let mut lines = String::new();
    for (i, case) in cases.iter().enumerate() {
        eprintln!(
            "[eval {}/{}] {}: {}",
            i + 1,
            cases.len(),
            case.id,
            case.query
        );
        let started = std::time::Instant::now();
        let mut r = EvalResult {
            case: case.clone(),
            run_id: None,
            grade: None,
            error: None,
            usd: 0.0,
            tool_calls: 0,
            subagents: 0,
            seconds: 0,
        };
        match sys.create_run(&case.query, vec![format!("eval:{}", case.id)]) {
            Ok(id) => {
                r.run_id = Some(id.clone());
                match sys.execute(&id).await {
                    Ok(out) => {
                        r.usd = out.spend.usd;
                        r.tool_calls = out.spend.tool_calls;
                        r.subagents = out.subagents;
                        match judge_run(sys, judge_model.clone(), &id, case.expected.as_deref())
                            .await
                        {
                            Ok(g) => r.grade = Some(g),
                            Err(e) => r.error = Some(format!("{e:#}")),
                        }
                    }
                    Err(e) => r.error = Some(format!("{e:#}")),
                }
            }
            Err(e) => r.error = Some(format!("{e:#}")),
        }
        r.seconds = started.elapsed().as_secs();
        lines.push_str(&serde_json::to_string(&r)?);
        lines.push('\n');
        atomic_write(&dir.join("results.jsonl"), lines.as_bytes())?;
        results.push(r);
    }
    atomic_write(&dir.join("summary.md"), summary(&results).as_bytes())?;
    Ok((dir, results))
}

pub fn summary(results: &[EvalResult]) -> String {
    let graded: Vec<&Grade> = results.iter().filter_map(|r| r.grade.as_ref()).collect();
    let mean = |f: fn(&Grade) -> f64| {
        if graded.is_empty() {
            0.0
        } else {
            graded.iter().map(|g| f(g)).sum::<f64>() / graded.len() as f64
        }
    };
    let passed = graded.iter().filter(|g| g.pass).count();
    let usd: f64 = results.iter().map(|r| r.usd).sum();
    let mut out = format!(
        "# Eval summary ({})\n\n{} cases · {} graded · **{} passed ({:.0}%)** · total cost ${:.2}\n\n\
         | criterion | mean |\n|---|---|\n| overall | {:.2} |\n| factual accuracy | {:.2} |\n| citation accuracy | {:.2} |\n\
         | completeness | {:.2} |\n| source quality | {:.2} |\n| tool efficiency | {:.2} |\n\n\
         | case | pass | overall | subagents | tool calls | cost | run | note |\n|---|---|---|---|---|---|---|---|\n",
        now_iso(),
        results.len(),
        graded.len(),
        passed,
        if graded.is_empty() { 0.0 } else { 100.0 * passed as f64 / graded.len() as f64 },
        usd,
        mean(|g| g.overall),
        mean(|g| g.factual_accuracy),
        mean(|g| g.citation_accuracy),
        mean(|g| g.completeness),
        mean(|g| g.source_quality),
        mean(|g| g.tool_efficiency),
    );
    for r in results {
        let (pass, overall, note) = match (&r.grade, &r.error) {
            (Some(g), _) => (
                if g.pass { "✓" } else { "✗" },
                format!("{:.2}", g.overall),
                crate::util::clip(&g.rationale, 120),
            ),
            (None, Some(e)) => ("—", "—".into(), crate::util::clip(e, 120)),
            _ => ("—", "—".into(), String::new()),
        };
        out.push_str(&format!(
            "| {} | {pass} | {overall} | {} | {} | ${:.3} | {} | {} |\n",
            r.case.id,
            r.subagents,
            r.tool_calls,
            r.usd,
            r.run_id.as_deref().unwrap_or("—"),
            note.replace('|', "/").replace('\n', " ")
        ));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn starter_set_parses() {
        let cases = parse_cases(STARTER_SET).unwrap();
        assert!(cases.len() >= 15);
        let mut ids: Vec<&str> = cases.iter().map(|c| c.id.as_str()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), cases.len(), "ids are unique");
    }
}
