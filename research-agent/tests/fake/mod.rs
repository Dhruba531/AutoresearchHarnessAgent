//! A scripted stand-in for a model provider. It answers the way a well-behaved
//! model would for each agent role (recognised by its system prompt), so tests
//! exercise the real orchestration, tools, budgets, checkpoints and citations
//! without any network calls.

#![allow(dead_code)]

use async_trait::async_trait;
use research_agent::llm::{ChatRequest, ChatResponse, LlmError, Model, Usage};
use research_agent::tools::fetch::Fetcher;
use research_agent::tools::search::{SearchBackend, SearchHit};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

pub const REPORT: &str = "# Rust at a glance\n\nRust is a systems language focused on safety and speed.\n\n\
## History\n\nRust 1.0 was released on 15 May 2015. Mozilla sponsored its early development.\n\n\
## Tooling\n\n- Cargo is Rust's build tool and package manager.\n- Crates are published to crates.io.\n\n\
Overall, the ecosystem is mature.";

#[derive(Default)]
pub struct FakeModel {
    pub lead_calls: AtomicU32,
    pub sub_calls: AtomicU32,
    pub cite_calls: AtomicU32,
    pub judge_calls: AtomicU32,
    /// Fail the lead's Nth call once (1-based), with a non-retryable error.
    pub fail_lead_call: Mutex<Option<u32>>,
    /// Tasks the lead delegates on its first turn.
    pub tasks: Mutex<Option<Value>>,
    pub seen_handoff: Mutex<bool>,
}

fn tool_use(id: &str, name: &str, input: Value) -> Value {
    json!({"type": "tool_use", "id": id, "name": name, "input": input})
}

fn reply(content: Vec<Value>, stop: &str) -> ChatResponse {
    ChatResponse {
        content,
        stop_reason: stop.into(),
        usage: Usage {
            input_tokens: 1000,
            output_tokens: 200,
            ..Usage::default()
        },
        model: "fake-model".into(),
        cost_usd: 0.002,
        invalid_tool_inputs: vec![],
        duration_ms: 1,
    }
}

fn first_user_text(req: &ChatRequest<'_>) -> String {
    match &req.messages[0]["content"] {
        Value::String(s) => s.clone(),
        v => v.to_string(),
    }
}

fn assistant_turns(req: &ChatRequest<'_>) -> usize {
    req.messages
        .iter()
        .filter(|m| m["role"] == "assistant")
        .count()
}

pub fn default_tasks() -> Value {
    json!([
        {"title": "Rust history", "objective": "When was Rust 1.0 released and who sponsored it?",
         "output_format": "dates and organisations", "guidance": "official blog", "boundaries": "no tooling", "tool_budget": 5},
        {"title": "Rust tooling", "objective": "What is Cargo and where are crates published?",
         "output_format": "short list", "guidance": "official docs", "boundaries": "no history", "tool_budget": 5}
    ])
}

impl FakeModel {
    fn lead(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        let n = self.lead_calls.fetch_add(1, Ordering::SeqCst) + 1;
        {
            let mut f = self.fail_lead_call.lock().unwrap();
            if *f == Some(n) {
                *f = None;
                return Err(LlmError::BadRequest("injected failure".into()));
            }
        }
        let first = first_user_text(req);
        if first.contains("fresh context") {
            *self.seen_handoff.lock().unwrap() = true;
            assert!(
                first.contains("### plan"),
                "handoff message should carry the plan from memory"
            );
            return Ok(reply(
                vec![tool_use(
                    "lead-done",
                    "complete_task",
                    json!({"report": REPORT}),
                )],
                "tool_use",
            ));
        }
        let last = req.messages.last().unwrap();
        let after_results = last["content"].to_string().contains("subagent result");
        if assistant_turns(req) == 0 {
            let tasks = self
                .tasks
                .lock()
                .unwrap()
                .clone()
                .unwrap_or_else(default_tasks);
            return Ok(reply(
                vec![
                    json!({"type": "thinking", "thinking": "Two independent parts: history and tooling.", "signature": "x"}),
                    json!({"type": "text", "text": "Planning two subagents."}),
                    tool_use(
                        "lead-1",
                        "memory_save",
                        json!({"key": "plan", "content": "Breadth-first: history + tooling."}),
                    ),
                    tool_use("lead-2", "run_subagents", json!({"tasks": tasks})),
                ],
                "tool_use",
            ));
        }
        if after_results || assistant_turns(req) >= 1 {
            return Ok(reply(
                vec![tool_use(
                    "lead-done",
                    "complete_task",
                    json!({"report": REPORT}),
                )],
                "tool_use",
            ));
        }
        unreachable!()
    }

    fn sub(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        self.sub_calls.fetch_add(1, Ordering::SeqCst);
        let task = first_user_text(req);
        let history = task.contains("Rust history");
        let (query, url) = if history {
            (
                "rust 1.0 release",
                "https://blog.rust-lang.org/2015/05/15/Rust-1.0.html",
            )
        } else {
            ("cargo package manager", "https://doc.rust-lang.org/cargo/")
        };
        match assistant_turns(req) {
            0 => Ok(reply(
                vec![tool_use("s1", "web_search", json!({"query": query}))],
                "tool_use",
            )),
            1 => Ok(reply(
                vec![tool_use("s2", "web_fetch", json!({"url": url}))],
                "tool_use",
            )),
            _ => {
                let findings = if history {
                    "Rust 1.0 was released on 15 May 2015; Mozilla sponsored early development."
                } else {
                    "Cargo is the build tool and package manager; crates are published to crates.io."
                };
                Ok(reply(
                    vec![tool_use(
                        "s3",
                        "complete_task",
                        json!({"findings": findings, "sources": [url], "confidence": "high", "gaps": ""}),
                    )],
                    "tool_use",
                ))
            }
        }
    }

    fn cite(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        self.cite_calls.fetch_add(1, Ordering::SeqCst);
        let text = first_user_text(req);
        // Map URLs to the ids the citation agent was shown.
        let mut ids: HashMap<String, String> = HashMap::new();
        for part in text.split("<source id=\"").skip(1) {
            let id = part.split('"').next().unwrap().to_string();
            let url = part
                .split("url=\"")
                .nth(1)
                .unwrap()
                .split('"')
                .next()
                .unwrap()
                .to_string();
            ids.insert(url, id);
        }
        let blog = ids
            .get("https://blog.rust-lang.org/2015/05/15/Rust-1.0.html")
            .cloned()
            .unwrap_or_default();
        let cargo = ids
            .get("https://doc.rust-lang.org/cargo/")
            .cloned()
            .unwrap_or_default();
        let citations = json!([
            {"claim": "Rust 1.0 was released on 15 May 2015.", "sources": [blog]},
            {"claim": "Mozilla sponsored its early development.", "sources": [blog]},
            {"claim": "Cargo is Rust's build tool and package manager.", "sources": [cargo]},
            {"claim": "This sentence is not in the report at all.", "sources": [cargo]}
        ]);
        Ok(reply(
            vec![tool_use(
                "c1",
                "add_citations",
                json!({"citations": citations}),
            )],
            "tool_use",
        ))
    }

    fn judge(&self, _req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        self.judge_calls.fetch_add(1, Ordering::SeqCst);
        Ok(reply(
            vec![tool_use(
                "j1",
                "submit_grade",
                json!({
                "factual_accuracy": 0.9, "citation_accuracy": 0.8, "completeness": 0.85, "source_quality": 0.9,
                "tool_efficiency": 0.7, "overall": 0.85, "pass": true, "rationale": "Accurate and cited."}),
            )],
            "tool_use",
        ))
    }
}

#[async_trait]
impl Model for FakeModel {
    fn id(&self) -> &str {
        "fake-model"
    }

    fn provider(&self) -> &str {
        "fake"
    }

    async fn chat(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        let s = req.system;
        if s.starts_with("You are the lead researcher") {
            self.lead(req)
        } else if s.starts_with("You are a research subagent") {
            self.sub(req)
        } else if s.contains("attribute claims") {
            self.cite(req)
        } else if s.contains("grade the output") {
            self.judge(req)
        } else {
            Ok(reply(
                vec![json!({"type": "text", "text": "## Diagnosis\nAll good."})],
                "end_turn",
            ))
        }
    }
}

pub fn search_backend() -> SearchBackend {
    SearchBackend::Static(vec![
        SearchHit {
            title: "Announcing Rust 1.0".into(),
            url: "https://blog.rust-lang.org/2015/05/15/Rust-1.0.html".into(),
            snippet: "Rust 1.0 release announcement, May 2015".into(),
            published: Some("2015-05-15".into()),
        },
        SearchHit {
            title: "The Cargo Book".into(),
            url: "https://doc.rust-lang.org/cargo/".into(),
            snippet: "Cargo is the Rust package manager".into(),
            published: None,
        },
        SearchHit {
            title: "Rust spam listicle".into(),
            url: "https://spam.example/rust".into(),
            snippet: "rust release cargo".into(),
            published: None,
        },
    ])
}

pub fn fetcher() -> Fetcher {
    let mut pages = HashMap::new();
    pages.insert(
        "https://blog.rust-lang.org/2015/05/15/Rust-1.0.html".to_string(),
        ("Announcing Rust 1.0".to_string(), "Today we are very proud to announce the 1.0 release of Rust, on May 15, 2015. Mozilla sponsored the project.".to_string()),
    );
    pages.insert(
        "https://doc.rust-lang.org/cargo/".to_string(),
        ("The Cargo Book".to_string(), "Cargo is the Rust package manager. Cargo downloads dependencies and compiles packages; crates live on crates.io.".to_string()),
    );
    Fetcher::Static(pages)
}
