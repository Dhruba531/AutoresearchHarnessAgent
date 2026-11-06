//! End-to-end runs of the whole system against the scripted fake model.

mod fake;

use fake::FakeModel;
use research_agent::config::{Config, Limits};
use research_agent::research::{ResearchSystem, RunState};
use research_agent::store::RunStore;
use research_agent::trace::read_events;
use serde_json::json;
use std::sync::atomic::Ordering;
use std::sync::Arc;

fn system(dir: &std::path::Path, model: Arc<FakeModel>, limits: Limits) -> ResearchSystem {
    let mut cfg = Config::from_env();
    cfg.data_dir = dir.to_path_buf();
    cfg.prompts_dir = None;
    cfg.limits = limits;
    cfg.max_retries = 0;
    ResearchSystem::with_parts(
        cfg,
        model.clone(),
        model.clone(),
        model,
        fake::search_backend(),
        Arc::new(fake::fetcher()),
        None,
        reqwest::Client::new(),
    )
}

fn body(report: &str) -> &str {
    report.split("\n\n## Sources").next().unwrap()
}

#[tokio::test]
async fn full_run_produces_a_cited_report() {
    let dir = tempfile::tempdir().unwrap();
    let model = Arc::new(FakeModel::default());
    let sys = system(dir.path(), model.clone(), Limits::default());
    let out = sys
        .research("Give me an overview of Rust's history and tooling")
        .await
        .unwrap();

    assert_eq!(out.status, "done");
    assert_eq!(out.subagents, 2);
    assert_eq!(
        model.sub_calls.load(Ordering::SeqCst),
        6,
        "two subagents × (search, fetch, complete)"
    );
    assert_eq!(model.lead_calls.load(Ordering::SeqCst), 2);
    assert_eq!(out.sources_cited, 2);

    // Citations were inserted in place; the text itself is unchanged.
    assert!(
        out.report
            .contains("Rust 1.0 was released on 15 May 2015 [1]."),
        "{}",
        out.report
    );
    assert!(out
        .report
        .contains("Mozilla sponsored its early development [1]."));
    assert!(out
        .report
        .contains("Cargo is Rust's build tool and package manager [2]."));
    assert!(out.report.contains("## Sources\n\n1. [Announcing Rust 1.0](https://blog.rust-lang.org/2015/05/15/Rust-1.0.html) (2015-05-15)"));
    assert_eq!(
        research_agent::citations::strip_markers(body(&out.report)),
        fake::REPORT
    );

    // Artifacts on disk.
    let store = RunStore::open(&dir.path().join("runs"), &out.run_id).unwrap();
    assert_eq!(store.read_text("report.raw.md").unwrap(), fake::REPORT);
    let sub = store.read_text("subagents/sub-1.1.md").unwrap();
    assert!(sub.contains("## Findings") && sub.contains("Rust 1.0 was released"));
    assert!(store
        .read_text("memory.json")
        .unwrap()
        .contains("Breadth-first"));
    let state: RunState = store.read_json("state.json").unwrap();
    assert_eq!(format!("{:?}", state.phase), "Done");
    assert_eq!(
        state
            .subagents
            .iter()
            .filter(|r| r.status == "done")
            .count(),
        2
    );
    let meta = store.meta().unwrap();
    assert_eq!(meta.status, "done");
    assert!(meta.cost_usd > 0.0);

    // The trace shows parallel delegation and every tool call.
    let events = read_events(&store.path("trace.jsonl"));
    let kinds: Vec<&str> = events.iter().map(|e| e.kind.as_str()).collect();
    assert!(kinds.contains(&"run_started") && kinds.contains(&"run_finished"));
    assert_eq!(kinds.iter().filter(|k| **k == "subagent_done").count(), 2);
    assert!(events
        .iter()
        .any(|e| e.kind == "tool_call" && e.data["tool"] == "run_subagents"));
    assert!(events.iter().any(|e| e.kind == "thinking"));
    let cites = events.iter().find(|e| e.kind == "citations").unwrap();
    assert_eq!(cites.data["dropped"], 1);
}

#[tokio::test]
async fn resume_continues_without_rerunning_finished_subagents() {
    let dir = tempfile::tempdir().unwrap();
    let model = Arc::new(FakeModel::default());
    *model.fail_lead_call.lock().unwrap() = Some(2);
    let sys = system(dir.path(), model.clone(), Limits::default());
    let id = sys.create_run("Rust history and tooling", vec![]).unwrap();

    let err = sys.execute(&id).await.unwrap_err();
    assert!(format!("{err:#}").contains("resume"), "{err:#}");
    let store = RunStore::open(&dir.path().join("runs"), &id).unwrap();
    assert_eq!(store.meta().unwrap().status, "failed");
    let state: RunState = store.read_json("state.json").unwrap();
    assert_eq!(
        state.subagents.len(),
        2,
        "subagent results were checkpointed"
    );
    let sub_calls = model.sub_calls.load(Ordering::SeqCst);

    let out = sys.execute(&id).await.unwrap();
    assert_eq!(out.status, "done");
    assert_eq!(
        model.sub_calls.load(Ordering::SeqCst),
        sub_calls,
        "no subagent ran again"
    );
    assert!(out.report.contains("[1]"));
    let events = read_events(&store.path("trace.jsonl"));
    assert!(events.iter().any(|e| e.kind == "run_resumed"));
    // Spend carries over across sessions.
    assert!(store.meta().unwrap().cost_usd > 0.0179);
}

#[tokio::test]
async fn lead_context_is_reset_from_memory_when_it_grows() {
    let dir = tempfile::tempdir().unwrap();
    let model = Arc::new(FakeModel::default());
    let limits = Limits {
        lead_context_tokens: 500,
        ..Limits::default()
    };
    let sys = system(dir.path(), model.clone(), limits);
    let out = sys.research("Rust history and tooling").await.unwrap();
    assert_eq!(out.status, "done");
    assert!(*model.seen_handoff.lock().unwrap());
    let store = RunStore::open(&dir.path().join("runs"), &out.run_id).unwrap();
    let state: RunState = store.read_json("state.json").unwrap();
    assert!(state.handoffs >= 1);
    assert!(read_events(&store.path("trace.jsonl"))
        .iter()
        .any(|e| e.kind == "handoff"));
}

#[tokio::test]
async fn subagent_limits_are_enforced_in_code() {
    let dir = tempfile::tempdir().unwrap();
    let model = Arc::new(FakeModel::default());
    let many: Vec<serde_json::Value> = (0..5)
        .map(|i| json!({"title": format!("Rust tooling {i}"), "objective": format!("Cargo question {i}"), "output_format": "list", "tool_budget": 3}))
        .collect();
    *model.tasks.lock().unwrap() = Some(json!(many));
    let limits = Limits {
        max_subagents_per_round: 4,
        max_subagents_total: 3,
        ..Limits::default()
    };
    let sys = system(dir.path(), model.clone(), limits);
    let out = sys.research("Rust tooling").await.unwrap();
    assert_eq!(out.subagents, 3, "the run-wide cap wins");
    let store = RunStore::open(&dir.path().join("runs"), &out.run_id).unwrap();
    let lead: serde_json::Value = serde_json::to_value(
        store
            .read_json::<RunState>("state.json")
            .unwrap()
            .lead
            .messages,
    )
    .unwrap();
    let text = lead.to_string();
    assert!(text.contains("Only the first 4 tasks ran"));
    assert!(text.contains("1 of 4 new tasks did not run"));
}
