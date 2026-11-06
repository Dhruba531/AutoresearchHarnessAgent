//! The HTTP API, the judge, the eval runner and the doctor, against the fake model.

mod fake;

use fake::FakeModel;
use futures::StreamExt;
use research_agent::config::{Config, Limits};
use research_agent::evals::{self, EvalCase};
use research_agent::research::ResearchSystem;
use research_agent::server::{router, BroadcastSink};
use research_agent::store::RunStore;
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;

fn system(dir: &std::path::Path, model: Arc<FakeModel>) -> ResearchSystem {
    let mut cfg = Config::from_env();
    cfg.data_dir = dir.to_path_buf();
    cfg.prompts_dir = None;
    cfg.limits = Limits::default();
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

#[tokio::test]
async fn http_api_runs_and_streams_events() {
    let dir = tempfile::tempdir().unwrap();
    let (tx, _) = tokio::sync::broadcast::channel(256);
    let mut sys = system(dir.path(), Arc::new(FakeModel::default()));
    sys.add_sink(Arc::new(BroadcastSink(tx.clone())));
    let app = router(Arc::new(sys), tx, Some("secret".into()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let http = reqwest::Client::new();

    let page = http.get(&base).send().await.unwrap().text().await.unwrap();
    assert!(page.contains("<title>Research Agent</title>"));
    let denied = http.get(format!("{base}/api/runs")).send().await.unwrap();
    assert_eq!(denied.status(), 401);

    let started: Value = http
        .post(format!("{base}/api/runs"))
        .bearer_auth("secret")
        .json(&json!({"query": "Rust history and tooling"}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let id = started["run_id"].as_str().unwrap().to_string();

    let mut detail = Value::Null;
    for _ in 0..100 {
        detail = http
            .get(format!("{base}/api/runs/{id}"))
            .bearer_auth("secret")
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        if detail["meta"]["status"] == "done" {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(detail["meta"]["status"], "done", "{detail}");
    assert!(detail["report"].as_str().unwrap().contains("[1]"));
    assert_eq!(detail["subagents"].as_array().unwrap().len(), 2);

    let list: Value = http
        .get(format!("{base}/api/runs?token=secret"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list[0]["id"], id.as_str());

    // SSE replays the finished run's events; read until run_finished.
    let resp = http
        .get(format!("{base}/api/runs/{id}/events?token=secret"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["content-type"], "text/event-stream");
    let mut body = String::new();
    let mut stream = resp.bytes_stream();
    let read = async {
        while let Some(Ok(chunk)) = stream.next().await {
            body.push_str(&String::from_utf8_lossy(&chunk));
            if body.contains("event: run_finished") {
                break;
            }
        }
    };
    tokio::time::timeout(Duration::from_secs(5), read)
        .await
        .expect("SSE replay");
    assert!(body.contains("event: subagent_done"));

    let missing = http
        .get(format!("{base}/api/runs/nope/events?token=secret"))
        .send()
        .await
        .unwrap();
    assert_eq!(missing.status(), 404);
    let traversal = http
        .get(format!("{base}/api/runs/..%2F..%2Fetc/events?token=secret"))
        .send()
        .await
        .unwrap();
    assert_eq!(traversal.status(), 404);
}

#[tokio::test]
async fn judge_eval_and_doctor() {
    let dir = tempfile::tempdir().unwrap();
    let model = Arc::new(FakeModel::default());
    let sys = system(dir.path(), model.clone());
    let out = sys.research("Rust history and tooling").await.unwrap();

    let grade = evals::judge_run(&sys, model.clone(), &out.run_id, Some("May 2015"))
        .await
        .unwrap();
    assert!(grade.pass);
    assert_eq!(grade.overall, 0.85);
    let store = RunStore::open(&dir.path().join("runs"), &out.run_id).unwrap();
    assert!(store.read_text("grade.json").is_some());

    let digest = research_agent::doctor::digest(&store).unwrap();
    assert!(digest.contains("## Subagent tasks") && digest.contains("sub-1.2"));
    let text = research_agent::doctor::diagnose(
        model.clone(),
        &sys.prompts.doctor,
        &sys.prompts,
        &store,
        0,
    )
    .await
    .unwrap();
    assert!(text.contains("Diagnosis"));
    assert!(store.read_text("doctor.md").is_some());

    let cases = vec![
        EvalCase {
            id: "a".into(),
            query: "Rust history".into(),
            expected: Some("2015".into()),
            kind: None,
        },
        EvalCase {
            id: "b".into(),
            query: "Rust tooling".into(),
            expected: None,
            kind: None,
        },
    ];
    let (eval_dir, results) = evals::run_eval(
        &sys,
        model.clone(),
        &cases,
        Some(dir.path().join("eval-out")),
    )
    .await
    .unwrap();
    assert_eq!(results.len(), 2);
    assert!(results
        .iter()
        .all(|r| r.grade.as_ref().is_some_and(|g| g.pass)));
    let summary = std::fs::read_to_string(eval_dir.join("summary.md")).unwrap();
    assert!(summary.contains("**2 passed (100%)**"), "{summary}");
    assert_eq!(
        std::fs::read_to_string(eval_dir.join("results.jsonl"))
            .unwrap()
            .lines()
            .count(),
        2
    );
}
