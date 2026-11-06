//! The agent loop every agent in the system runs: call the model, execute the
//! tool calls it asks for (in parallel), append the results, repeat until it
//! calls its finish tool.
//!
//! Properties the rest of the system relies on:
//! * **Append-only history.** Nothing earlier in the conversation is edited, so
//!   thinking blocks stay valid and the prompt cache keeps hitting. Reminders
//!   (budget left, turns left) are appended after the tool results.
//! * **Validated inputs.** Every tool input is checked against the tool's schema;
//!   invalid JSON, unknown tools and bad fields become error results the model can
//!   correct. A tool call cut off by the output limit is never run.
//! * **Budgets in code.** A per-agent tool-call cap, a turn cap, and the run-wide
//!   spend/time budget. Over the cap, tools return "budget used up; finish now".
//! * **Resumable.** The state is plain data. Given a saved state whose last
//!   message has unanswered tool calls, the loop executes them first.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Instant;

use crate::budget::Budget;
use crate::llm::{chat_with_retry, text_of, ChatRequest, Model};
use crate::tools::{definition, validate, Tool, ToolOutput};
use crate::trace::Tracer;

pub struct AgentSpec {
    pub name: String,
    pub model: Arc<dyn Model>,
    pub system: String,
    /// Client tools, including the finish tool.
    pub tools: Vec<Arc<dyn Tool>>,
    /// Provider server tools (e.g. Claude's web search), passed through as-is.
    pub server_tools: Vec<Value>,
    pub finish_tool: String,
    pub max_turns: u32,
    pub max_tool_calls: u32,
    pub max_tokens: u32,
    pub effort: Option<String>,
    pub thinking_display: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct AgentState {
    pub messages: Vec<Value>,
    pub turns: u32,
    pub tool_calls: u32,
    pub nudges: u32,
    pub usd: f64,
    pub last_prompt_tokens: u64,
}

impl AgentState {
    pub fn new(first_message: &str) -> AgentState {
        AgentState {
            messages: vec![json!({"role": "user", "content": first_message})],
            ..AgentState::default()
        }
    }

    pub fn push_user_text(&mut self, text: &str) {
        self.messages
            .push(json!({"role": "user", "content": [{"type": "text", "text": text}]}));
    }

    /// The text of the latest assistant message that has any.
    pub fn last_text(&self) -> String {
        self.messages
            .iter()
            .rev()
            .filter(|m| m["role"] == "assistant")
            .map(|m| {
                m["content"]
                    .as_array()
                    .map(|c| text_of(c))
                    .unwrap_or_default()
            })
            .find(|t| !t.trim().is_empty())
            .unwrap_or_default()
    }
}

#[derive(Debug, Clone)]
pub enum AgentEnd {
    /// The agent called its finish tool with this (validated) input.
    Finished(Value),
    /// The agent stopped with text and no finish call, even after a nudge.
    Text(String),
    OutOfTurns,
    Budget(String),
    Error(String),
}

pub struct Env {
    pub budget: Arc<Budget>,
    pub tracer: Arc<Tracer>,
    pub max_retries: u32,
}

#[async_trait]
pub trait Hooks: Send + Sync {
    /// Called after each turn is appended (checkpointing, context handoffs).
    async fn after_turn(&self, _state: &mut AgentState) {}
    /// Called right after the model's reply is appended, before its tools run,
    /// so a crash mid-tool leaves a checkpoint whose pending calls can be re-run.
    async fn after_reply(&self, _state: &AgentState) {}
    /// Text appended after a turn's tool results.
    fn reminder(&self, _state: &AgentState, _spec: &AgentSpec) -> Option<String> {
        None
    }
    /// Sees every model response (e.g. to harvest server-tool sources).
    fn on_response(&self, _content: &[Value]) {}
}

pub struct NoHooks;
impl Hooks for NoHooks {}

enum Decision {
    Run(Arc<dyn Tool>),
    Finish(Arc<dyn Tool>),
    Reject(String),
}

/// Run an agent until it finishes or a limit stops it.
pub async fn run(
    spec: &AgentSpec,
    env: &Env,
    mut state: AgentState,
    hooks: &dyn Hooks,
) -> (AgentEnd, AgentState) {
    let mut defs: Vec<Value> = spec.tools.iter().map(|t| definition(t.as_ref())).collect();
    defs.extend(spec.server_tools.iter().cloned());
    let tracer = &env.tracer;

    // A resumed state may end in an assistant turn: run its pending tool calls,
    // re-send a paused server-tool turn, or ask a finished-text turn to continue.
    let mut pending: Option<(Vec<Value>, String)> = None;
    if let Some(last) = state.messages.last().filter(|m| m["role"] == "assistant") {
        let content = last["content"].as_array().cloned().unwrap_or_default();
        if content.iter().any(|b| b["type"] == "tool_use") {
            pending = Some((content, "tool_use".into()));
        } else if !content.iter().any(|b| b["type"] == "server_tool_use") {
            state.push_user_text("Continue your task from where you left off.");
        }
    }

    loop {
        let (content, stop, invalid) = match pending.take() {
            Some((c, s)) => (c, s, Vec::new()),
            None => {
                if state.turns >= spec.max_turns {
                    return (AgentEnd::OutOfTurns, state);
                }
                if let Some(reason) = env.budget.exhausted() {
                    return (AgentEnd::Budget(reason), state);
                }
                let req = ChatRequest {
                    system: &spec.system,
                    tools: &defs,
                    messages: &state.messages,
                    max_tokens: spec.max_tokens,
                    effort: spec.effort.as_deref(),
                    thinking_display: spec.thinking_display.as_deref(),
                };
                let resp = match chat_with_retry(spec.model.as_ref(), &req, env.max_retries).await {
                    Ok(r) => r,
                    Err(e) => {
                        tracer.emit(&spec.name, "agent_error", json!({"message": e.to_string()}));
                        return (AgentEnd::Error(e.to_string()), state);
                    }
                };
                env.budget.record_call(&resp.usage, resp.cost_usd);
                state.usd += resp.cost_usd;
                state.turns += 1;
                state.last_prompt_tokens = resp.usage.prompt_tokens();
                tracer.emit(
                    &spec.name,
                    "model_call",
                    json!({
                        "model": resp.model, "stop_reason": resp.stop_reason, "ms": resp.duration_ms,
                        "prompt_tokens": resp.usage.prompt_tokens(), "cache_read_tokens": resp.usage.cache_read_input_tokens,
                        "output_tokens": resp.usage.output_tokens, "web_searches": resp.usage.web_search_requests,
                        "usd": resp.cost_usd, "turn": state.turns,
                    }),
                );
                let thinking: String = resp
                    .content
                    .iter()
                    .filter(|b| b["type"] == "thinking")
                    .filter_map(|b| b["thinking"].as_str())
                    .collect::<Vec<_>>()
                    .join("\n");
                if tracer.content && !thinking.trim().is_empty() {
                    tracer.emit(
                        &spec.name,
                        "thinking",
                        json!({"text": crate::util::clip(&thinking, 4000)}),
                    );
                }
                hooks.on_response(&resp.content);
                let server_calls = resp
                    .content
                    .iter()
                    .filter(|b| b["type"] == "server_tool_use")
                    .count() as u32;
                if server_calls > 0 {
                    state.tool_calls += server_calls;
                    env.budget.record_tool_calls(server_calls as u64);
                    for b in resp
                        .content
                        .iter()
                        .filter(|b| b["type"] == "server_tool_use")
                    {
                        tracer.emit(&spec.name, "tool_call", json!({
                            "tool": format!("server:{}", b["name"].as_str().unwrap_or("?")), "ok": true, "ms": 0,
                            "input": if tracer.content { b["input"].clone() } else { Value::Null },
                        }));
                    }
                }
                state
                    .messages
                    .push(json!({"role": "assistant", "content": resp.content}));
                hooks.after_reply(&state).await;
                if resp.stop_reason == "pause_turn" {
                    // The server-side tool loop paused; re-send as-is to let it resume.
                    hooks.after_turn(&mut state).await;
                    continue;
                }
                (resp.content, resp.stop_reason, resp.invalid_tool_inputs)
            }
        };

        let uses: Vec<&Value> = content.iter().filter(|b| b["type"] == "tool_use").collect();
        if uses.is_empty() {
            if stop == "max_tokens" {
                state.push_user_text(
                    "Your reply was cut off at the output limit. Continue, more concisely.",
                );
                hooks.after_turn(&mut state).await;
                continue;
            }
            if state.nudges == 0 && !spec.finish_tool.is_empty() {
                state.nudges += 1;
                let f = &spec.finish_tool;
                state.push_user_text(&format!(
                    "You ended your turn without calling {f}. If your work is done, call {f} now with your complete \
                     result; otherwise continue working."
                ));
                hooks.after_turn(&mut state).await;
                continue;
            }
            return (AgentEnd::Text(text_of(&content)), state);
        }

        // Decide what runs: in order, so the tool-call cap is applied deterministically.
        let truncated = stop == "max_tokens";
        let mut plan: Vec<(String, String, Value, Decision)> = Vec::new();
        let mut granted = 0u64;
        for u in &uses {
            let id = u["id"].as_str().unwrap_or("").to_string();
            let name = u["name"].as_str().unwrap_or("").to_string();
            let input = u["input"].clone();
            let decision = if truncated {
                Decision::Reject("Your output hit the token limit while writing this tool call, so it did not run. Retry with a shorter input.".into())
            } else if let Some((_, raw)) = invalid.iter().find(|(i, _)| *i == id) {
                Decision::Reject(json!({"INVALID_JSON": raw}).to_string())
            } else {
                match spec.tools.iter().find(|t| t.name() == name) {
                    None => Decision::Reject(format!(
                        "unknown tool '{name}'; available: {}",
                        spec.tools.iter().map(|t| t.name().to_string()).collect::<Vec<_>>().join(", ")
                    )),
                    Some(t) => match validate(&t.input_schema(), &input) {
                        Err(e) => Decision::Reject(format!("invalid input: {e}")),
                        Ok(()) if name == spec.finish_tool => Decision::Finish(t.clone()),
                        Ok(()) if state.tool_calls >= spec.max_tool_calls => Decision::Reject(format!(
                            "Your tool-call budget ({}) is used up, so this call did not run. Call {} now with what you have.",
                            spec.max_tool_calls, spec.finish_tool
                        )),
                        Ok(()) => {
                            state.tool_calls += 1;
                            granted += 1;
                            Decision::Run(t.clone())
                        }
                    },
                }
            };
            plan.push((id, name, input, decision));
        }
        env.budget.record_tool_calls(granted);

        let outputs: Vec<(ToolOutput, u128)> =
            futures::future::join_all(plan.iter().map(|(_, _, input, d)| async move {
                let t0 = Instant::now();
                let out = match d {
                    Decision::Run(t) | Decision::Finish(t) => {
                        t.call(&spec.name, input.clone()).await
                    }
                    Decision::Reject(msg) => ToolOutput::err(msg.clone()),
                };
                (out, t0.elapsed().as_millis())
            }))
            .await;

        let mut blocks = Vec::with_capacity(plan.len() + 1);
        let mut finished: Option<Value> = None;
        for ((id, name, input, d), (out, ms)) in plan.iter().zip(outputs) {
            tracer.emit(
                &spec.name,
                "tool_call",
                json!({
                    "tool": name, "ok": !out.is_error, "ms": ms, "output_chars": out.content.len(),
                    "input": if tracer.content { input.clone() } else { Value::Null },
                    "error": if out.is_error { tracer.text(&out.content, 400) } else { Value::Null },
                }),
            );
            if matches!(d, Decision::Finish(_)) && !out.is_error {
                finished = Some(input.clone());
            }
            let text = if out.content.is_empty() {
                "(no output)".to_string()
            } else {
                out.content
            };
            blocks.push(json!({"type": "tool_result", "tool_use_id": id, "content": text, "is_error": out.is_error}));
        }
        if finished.is_none() {
            if let Some(r) = hooks.reminder(&state, spec) {
                blocks.push(json!({"type": "text", "text": r}));
            }
        }
        state
            .messages
            .push(json!({"role": "user", "content": blocks}));
        hooks.after_turn(&mut state).await;
        if let Some(input) = finished {
            return (AgentEnd::Finished(input), state);
        }
    }
}
