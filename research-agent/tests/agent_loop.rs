//! The agent loop's guardrails, driven by a model that replays a fixed script.

use async_trait::async_trait;
use research_agent::agent::{run, AgentEnd, AgentSpec, AgentState, Env, NoHooks};
use research_agent::budget::Budget;
use research_agent::config::Limits;
use research_agent::llm::{ChatRequest, ChatResponse, LlmError, Model, Usage};
use research_agent::tools::{FinishTool, Tool, ToolOutput};
use research_agent::trace::Tracer;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

struct Scripted {
    turns: Mutex<Vec<ChatResponse>>,
    calls: AtomicUsize,
    seen: Mutex<Vec<Vec<Value>>>,
}

impl Scripted {
    fn new(turns: Vec<ChatResponse>) -> Arc<Scripted> {
        Arc::new(Scripted {
            turns: Mutex::new(turns.into_iter().rev().collect()),
            calls: AtomicUsize::new(0),
            seen: Mutex::new(vec![]),
        })
    }
}

#[async_trait]
impl Model for Scripted {
    fn id(&self) -> &str {
        "scripted"
    }
    fn provider(&self) -> &str {
        "fake"
    }
    async fn chat(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.seen.lock().unwrap().push(req.messages.to_vec());
        self.turns
            .lock()
            .unwrap()
            .pop()
            .ok_or_else(|| LlmError::Other("script exhausted".into()))
    }
}

fn turn(content: Vec<Value>, stop: &str) -> ChatResponse {
    ChatResponse {
        content,
        stop_reason: stop.into(),
        usage: Usage::default(),
        model: "scripted".into(),
        cost_usd: 0.0,
        invalid_tool_inputs: vec![],
        duration_ms: 0,
    }
}

fn call(id: &str, name: &str, input: Value) -> Value {
    json!({"type": "tool_use", "id": id, "name": name, "input": input})
}

struct Echo {
    delay: Duration,
    count: AtomicUsize,
}

#[async_trait]
impl Tool for Echo {
    fn name(&self) -> &str {
        "echo"
    }
    fn description(&self) -> String {
        "echo".into()
    }
    fn input_schema(&self) -> Value {
        json!({"type": "object", "additionalProperties": false, "required": ["text"], "properties": {"text": {"type": "string"}}})
    }
    async fn call(&self, _agent: &str, input: Value) -> ToolOutput {
        tokio::time::sleep(self.delay).await;
        self.count.fetch_add(1, Ordering::SeqCst);
        ToolOutput::ok(input["text"].as_str().unwrap_or("").to_string())
    }
}

fn finish() -> Arc<dyn Tool> {
    Arc::new(FinishTool {
        name: "complete_task".into(),
        description: "finish".into(),
        schema: json!({"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}),
    })
}

fn spec(model: Arc<Scripted>, echo: Arc<Echo>, max_tool_calls: u32) -> AgentSpec {
    AgentSpec {
        name: "sub-1.1".into(),
        model,
        system: "test".into(),
        tools: vec![echo, finish()],
        server_tools: vec![],
        finish_tool: "complete_task".into(),
        max_turns: 10,
        max_tool_calls,
        max_tokens: 1000,
        effort: None,
        thinking_display: None,
    }
}

fn env() -> Env {
    Env {
        budget: Arc::new(Budget::new(Limits::default())),
        tracer: Arc::new(Tracer::null()),
        max_retries: 0,
    }
}

fn results(messages: &[Value], idx: usize) -> Vec<Value> {
    messages[idx]["content"].as_array().unwrap().clone()
}

#[tokio::test]
async fn parallel_calls_budget_and_validation() {
    let model = Scripted::new(vec![
        turn(
            vec![
                call("a", "echo", json!({"text": "one"})),
                call("b", "echo", json!({"text": "two"})),
                call("c", "echo", json!({"text": "three"})),
                call("d", "echo", json!({"wrong": 1})),
                call("e", "nope", json!({})),
            ],
            "tool_use",
        ),
        turn(
            vec![call("f", "complete_task", json!({"answer": "done"}))],
            "tool_use",
        ),
    ]);
    let echo = Arc::new(Echo {
        delay: Duration::from_millis(200),
        count: AtomicUsize::new(0),
    });
    let t0 = Instant::now();
    let (end, state) = run(
        &spec(model.clone(), echo.clone(), 2),
        &env(),
        AgentState::new("go"),
        &NoHooks,
    )
    .await;
    assert!(
        matches!(end, AgentEnd::Finished(ref v) if v["answer"] == "done"),
        "{end:?}"
    );
    assert!(
        t0.elapsed() < Duration::from_millis(390),
        "two 200 ms tools ran concurrently"
    );
    assert_eq!(
        echo.count.load(Ordering::SeqCst),
        2,
        "the third call is over budget"
    );
    let r = results(&state.messages, 2);
    assert_eq!(r.len(), 5, "every tool_use gets a result, in one message");
    assert_eq!(r[0]["content"], "one");
    assert!(r[2]["content"]
        .as_str()
        .unwrap()
        .contains("budget (2) is used up"));
    assert!(r[3]["content"]
        .as_str()
        .unwrap()
        .contains("input.text is required"));
    assert!(r[4]["content"].as_str().unwrap().contains("unknown tool"));
    assert_eq!(r[4]["is_error"], true);
    assert_eq!(state.tool_calls, 2);
}

#[tokio::test]
async fn truncated_and_invalid_json_calls_never_run() {
    let mut bad = turn(vec![call("x", "echo", json!({}))], "tool_use");
    bad.invalid_tool_inputs = vec![("x".into(), "{\"text\": \"unterminated".into())];
    let model = Scripted::new(vec![
        turn(
            vec![call("t", "echo", json!({"text": "partial"}))],
            "max_tokens",
        ),
        bad,
        turn(
            vec![call("f", "complete_task", json!({"answer": "ok"}))],
            "tool_use",
        ),
    ]);
    let echo = Arc::new(Echo {
        delay: Duration::ZERO,
        count: AtomicUsize::new(0),
    });
    let (end, state) = run(
        &spec(model, echo.clone(), 10),
        &env(),
        AgentState::new("go"),
        &NoHooks,
    )
    .await;
    assert!(matches!(end, AgentEnd::Finished(_)));
    assert_eq!(echo.count.load(Ordering::SeqCst), 0);
    assert!(results(&state.messages, 2)[0]["content"]
        .as_str()
        .unwrap()
        .contains("token limit"));
    assert!(results(&state.messages, 4)[0]["content"]
        .as_str()
        .unwrap()
        .contains("INVALID_JSON"));
}

#[tokio::test]
async fn nudges_once_then_accepts_text() {
    let model = Scripted::new(vec![
        turn(
            vec![json!({"type": "text", "text": "I think I'm done."})],
            "end_turn",
        ),
        turn(
            vec![json!({"type": "text", "text": "Final answer in prose."})],
            "end_turn",
        ),
    ]);
    let echo = Arc::new(Echo {
        delay: Duration::ZERO,
        count: AtomicUsize::new(0),
    });
    let (end, state) = run(
        &spec(model.clone(), echo, 10),
        &env(),
        AgentState::new("go"),
        &NoHooks,
    )
    .await;
    assert!(matches!(end, AgentEnd::Text(ref t) if t == "Final answer in prose."));
    let nudge = state.messages[2]["content"][0]["text"].as_str().unwrap();
    assert!(nudge.contains("without calling complete_task"));
}

#[tokio::test]
async fn resumed_state_runs_pending_calls_first() {
    let mut state = AgentState::new("go");
    state.messages.push(
        json!({"role": "assistant", "content": [call("p", "echo", json!({"text": "pending"}))]}),
    );
    let model = Scripted::new(vec![turn(
        vec![call("f", "complete_task", json!({"answer": "ok"}))],
        "tool_use",
    )]);
    let echo = Arc::new(Echo {
        delay: Duration::ZERO,
        count: AtomicUsize::new(0),
    });
    let (end, state) = run(
        &spec(model.clone(), echo.clone(), 10),
        &env(),
        state,
        &NoHooks,
    )
    .await;
    assert!(matches!(end, AgentEnd::Finished(_)));
    assert_eq!(echo.count.load(Ordering::SeqCst), 1);
    assert_eq!(model.calls.load(Ordering::SeqCst), 1);
    // The first request the model saw already held the pending call's result.
    let first = &model.seen.lock().unwrap()[0];
    assert_eq!(first[2]["content"][0]["tool_use_id"], "p");
    assert_eq!(state.messages.len(), 5);
}

#[tokio::test]
async fn errors_and_budget_stop_the_agent() {
    let model = Scripted::new(vec![]);
    let echo = Arc::new(Echo {
        delay: Duration::ZERO,
        count: AtomicUsize::new(0),
    });
    let (end, _) = run(
        &spec(model, echo.clone(), 10),
        &env(),
        AgentState::new("go"),
        &NoHooks,
    )
    .await;
    assert!(matches!(end, AgentEnd::Error(ref e) if e.contains("script exhausted")));

    let tight = Env {
        budget: Arc::new(Budget::new(Limits {
            max_usd: 0.0,
            ..Limits::default()
        })),
        tracer: Arc::new(Tracer::null()),
        max_retries: 0,
    };
    let model = Scripted::new(vec![turn(vec![], "end_turn")]);
    let (end, _) = run(
        &spec(model.clone(), echo, 10),
        &tight,
        AgentState::new("go"),
        &NoHooks,
    )
    .await;
    assert!(matches!(end, AgentEnd::Budget(_)));
    assert_eq!(model.calls.load(Ordering::SeqCst), 0);
}
