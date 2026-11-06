//! "Let agents improve themselves": given a run's trace, a model diagnoses its
//! failure modes and proposes concrete edits to the prompts and tool
//! descriptions. The output is advice in `doctor.md`; nothing is changed
//! automatically. Edit the prompts (`research-agent prompts --dump DIR`), point
//! `RESEARCH_PROMPTS_DIR` at them, and re-run the eval set to check the effect.

use std::sync::Arc;

use crate::llm::{chat_with_retry, ChatRequest, Model};
use crate::research::RunState;
use crate::store::RunStore;
use crate::trace::{read_events, summarize};
use crate::util::clip;

/// A compact, content-light digest of a run for diagnosis.
pub fn digest(store: &RunStore) -> anyhow::Result<String> {
    let meta = store.meta()?;
    let state: RunState = store.read_json("state.json").unwrap_or_default();
    let events = read_events(&store.path("trace.jsonl"));
    let summary = summarize(&events);
    let mut out = format!(
        "Question: {}\nStatus: {} · cost ${:.3} · {} subagents · {} rounds · {} context handoffs · lead turns {}\n\n## Tool usage\n\n{}\n## Subagent tasks\n",
        meta.query,
        meta.status,
        state.spend.usd,
        state.subagents.len(),
        state.rounds,
        state.handoffs,
        state.lead.turns,
        summary.to_markdown()
    );
    for r in &state.subagents {
        out.push_str(&format!(
            "\n- {} [{}; {} tool calls of {}; ${:.3}; ended: {}] {}\n  objective: {}\n  boundaries: {}\n  findings: {}\n  gaps: {}\n",
            r.id,
            r.status,
            r.tool_calls,
            r.task.tool_budget,
            r.usd,
            r.end,
            r.task.title,
            clip(&r.task.objective, 300),
            clip(&r.task.boundaries, 200),
            clip(&r.findings, 400),
            clip(&r.gaps, 200)
        ));
    }
    if let Ok(c) = store.read_json::<crate::citations::CitedReport>("citations.json") {
        out.push_str(&format!(
            "\n## Citations\n\n{} claims cited from {} sources; {} proposed citations dropped.\n",
            c.claims,
            c.cited.len(),
            c.dropped
        ));
    }
    Ok(out)
}

pub async fn diagnose(
    model: Arc<dyn Model>,
    system: &str,
    prompts: &crate::prompts::Prompts,
    store: &RunStore,
    max_retries: u32,
) -> anyhow::Result<String> {
    let digest = digest(store)?;
    let msg = format!(
        "<prompts>\n<lead>\n{}\n</lead>\n<subagent>\n{}\n</subagent>\n<citations>\n{}\n</citations>\n</prompts>\n\n<run_digest>\n{}\n</run_digest>",
        prompts.lead, prompts.subagent, prompts.citations, digest
    );
    let messages = vec![serde_json::json!({"role": "user", "content": msg})];
    let req = ChatRequest {
        system,
        tools: &[],
        messages: &messages,
        max_tokens: 16_000,
        effort: Some("high"),
        thinking_display: None,
    };
    let resp = chat_with_retry(model.as_ref(), &req, max_retries).await?;
    let text = resp.text();
    store.write_text("doctor.md", &text)?;
    Ok(text)
}
