//! LLM-as-judge: one call, one rubric, scores from 0.0 to 1.0 and a pass/fail.
//! A single judge prompt scoring every criterion together was the most
//! consistent with human judgement; it checks factual accuracy, citation
//! accuracy, completeness, source quality and tool efficiency.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;

use crate::agent::{self, AgentEnd, AgentSpec, AgentState, Env, NoHooks};
use crate::budget::Spend;
use crate::llm::Model;
use crate::tools::{FinishTool, Tool};
use crate::util::clip;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Grade {
    pub factual_accuracy: f64,
    pub citation_accuracy: f64,
    pub completeness: f64,
    pub source_quality: f64,
    pub tool_efficiency: f64,
    pub overall: f64,
    pub pass: bool,
    pub rationale: String,
}

fn grade_tool() -> FinishTool {
    let score = json!({"type": "number", "minimum": 0.0, "maximum": 1.0});
    FinishTool {
        name: "submit_grade".into(),
        description: "Submit the grade.".into(),
        schema: json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["factual_accuracy", "citation_accuracy", "completeness", "source_quality", "tool_efficiency", "overall", "pass", "rationale"],
            "properties": {
                "factual_accuracy": score, "citation_accuracy": score, "completeness": score,
                "source_quality": score, "tool_efficiency": score, "overall": score,
                "pass": {"type": "boolean"}, "rationale": {"type": "string"}
            }
        }),
    }
}

pub struct JudgeInput<'a> {
    pub query: &'a str,
    pub expected: Option<&'a str>,
    pub report: &'a str,
    pub spend: &'a Spend,
    pub subagents: usize,
}

pub async fn judge(
    model: Arc<dyn Model>,
    env: &Env,
    system: &str,
    input: &JudgeInput<'_>,
) -> anyhow::Result<Grade> {
    let tool: Arc<dyn Tool> = Arc::new(grade_tool());
    let spec = AgentSpec {
        name: "judge".into(),
        model,
        system: system.to_string(),
        tools: vec![tool],
        server_tools: vec![],
        finish_tool: "submit_grade".into(),
        max_turns: 3,
        max_tool_calls: 2,
        max_tokens: 16_000,
        effort: Some("high".into()),
        thinking_display: None,
    };
    let s = input.spend;
    let msg = format!(
        "<question>\n{}\n</question>\n\n{}<report>\n{}\n</report>\n\n<process>\nsubagents: {}\ntool calls: {}\nmodel calls: {}\nweb searches (server-side): {}\ncost: ${:.3}\n</process>",
        input.query,
        input.expected.map(|e| format!("<expected_answer>\n{e}\n</expected_answer>\n\n")).unwrap_or_default(),
        clip(input.report, 120_000),
        input.subagents,
        s.tool_calls,
        s.model_calls,
        s.web_searches,
        s.usd
    );
    let (end, _) = agent::run(&spec, env, AgentState::new(&msg), &NoHooks).await;
    match end {
        AgentEnd::Finished(v) => Ok(parse(&v)),
        AgentEnd::Error(e) => anyhow::bail!("judge failed: {e}"),
        other => anyhow::bail!("judge did not submit a grade ({other:?})"),
    }
}

fn parse(v: &Value) -> Grade {
    let f = |k: &str| v[k].as_f64().unwrap_or(0.0).clamp(0.0, 1.0);
    Grade {
        factual_accuracy: f("factual_accuracy"),
        citation_accuracy: f("citation_accuracy"),
        completeness: f("completeness"),
        source_quality: f("source_quality"),
        tool_efficiency: f("tool_efficiency"),
        overall: f("overall"),
        pass: v["pass"].as_bool().unwrap_or(false),
        rationale: v["rationale"].as_str().unwrap_or("").to_string(),
    }
}
