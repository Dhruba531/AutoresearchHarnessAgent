//! Tracing: every model call, tool call, subagent start/finish, context handoff
//! and budget warning becomes one JSON line in the run's `trace.jsonl`. That is
//! what lets you see *why* an agent "did not find the obvious answer": bad
//! queries, poor sources, or failing tools.
//!
//! With `RESEARCH_TRACE_CONTENT=0` the trace keeps only structure (which tools,
//! how long, how many tokens, which errors) and no queries, outputs or thinking.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::Write;
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::util::{clip, now_iso};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub ts: String,
    pub run_id: String,
    pub agent: String,
    pub kind: String,
    #[serde(default)]
    pub data: Value,
}

/// Something that wants to see events as they happen (the CLI, the web UI).
pub trait Sink: Send + Sync {
    fn on_event(&self, e: &Event);
}

pub struct Tracer {
    pub run_id: String,
    pub content: bool,
    file: Option<Mutex<std::fs::File>>,
    sinks: Vec<Arc<dyn Sink>>,
}

impl Tracer {
    pub fn open(path: &Path, run_id: &str, content: bool, sinks: Vec<Arc<dyn Sink>>) -> Tracer {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .ok()
            .map(Mutex::new);
        Tracer {
            run_id: run_id.to_string(),
            content,
            file,
            sinks,
        }
    }

    /// A tracer that records nothing.
    pub fn null() -> Tracer {
        Tracer {
            run_id: String::new(),
            content: false,
            file: None,
            sinks: Vec::new(),
        }
    }

    pub fn emit(&self, agent: &str, kind: &str, data: Value) {
        let e = Event {
            ts: now_iso(),
            run_id: self.run_id.clone(),
            agent: agent.to_string(),
            kind: kind.to_string(),
            data,
        };
        if let Some(f) = &self.file {
            if let Ok(line) = serde_json::to_string(&e) {
                let mut f = f.lock().unwrap();
                let _ = writeln!(f, "{line}");
            }
        }
        for s in &self.sinks {
            s.on_event(&e);
        }
    }

    /// `text` (clipped) when content tracing is on, else null.
    pub fn text(&self, text: &str, max: usize) -> Value {
        if self.content {
            json!(clip(text, max))
        } else {
            Value::Null
        }
    }
}

pub fn read_events(path: &Path) -> Vec<Event> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|l| serde_json::from_str(l).ok())
        .collect()
}

#[derive(Default, Debug, Serialize)]
pub struct AgentStats {
    pub model_calls: u64,
    pub tool_calls: u64,
    pub tool_errors: u64,
    pub usd: f64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub tools: BTreeMap<String, u64>,
    pub model_ms: u64,
    pub tool_ms: u64,
}

#[derive(Default, Debug, Serialize)]
pub struct TraceSummary {
    pub agents: BTreeMap<String, AgentStats>,
    pub errors: Vec<String>,
    pub repeated_queries: Vec<String>,
    pub handoffs: u64,
    pub budget_warnings: u64,
    pub events: usize,
}

/// Aggregate a trace into per-agent statistics, errors and repeated queries.
pub fn summarize(events: &[Event]) -> TraceSummary {
    let mut s = TraceSummary {
        events: events.len(),
        ..TraceSummary::default()
    };
    let mut queries: BTreeMap<String, u32> = BTreeMap::new();
    for e in events {
        let a = s.agents.entry(e.agent.clone()).or_default();
        let d = &e.data;
        let num = |k: &str| d[k].as_u64().unwrap_or(0);
        match e.kind.as_str() {
            "model_call" => {
                a.model_calls += 1;
                a.usd += d["usd"].as_f64().unwrap_or(0.0);
                a.input_tokens += num("prompt_tokens");
                a.output_tokens += num("output_tokens");
                a.model_ms += num("ms");
            }
            "tool_call" => {
                a.tool_calls += 1;
                a.tool_ms += num("ms");
                *a.tools
                    .entry(d["tool"].as_str().unwrap_or("?").to_string())
                    .or_default() += 1;
                if d["ok"] == false {
                    a.tool_errors += 1;
                    let msg = d["error"].as_str().unwrap_or("(error text not traced)");
                    s.errors.push(format!(
                        "{} · {}: {}",
                        e.agent,
                        d["tool"].as_str().unwrap_or("?"),
                        clip(msg, 200)
                    ));
                }
                if let Some(q) = d.pointer("/input/query").and_then(Value::as_str) {
                    *queries.entry(q.to_lowercase()).or_default() += 1;
                }
            }
            "error" | "agent_error" => s.errors.push(format!(
                "{} · {}",
                e.agent,
                d["message"].as_str().unwrap_or("error")
            )),
            "handoff" => s.handoffs += 1,
            "budget_warning" => s.budget_warnings += 1,
            _ => {}
        }
    }
    s.agents.retain(|_, a| a.model_calls + a.tool_calls > 0);
    s.repeated_queries = queries
        .into_iter()
        .filter(|(_, n)| *n > 1)
        .map(|(q, n)| format!("{q} (×{n})"))
        .collect();
    s
}

impl TraceSummary {
    pub fn to_markdown(&self) -> String {
        let mut out = String::from("| agent | model calls | tool calls | errors | tokens in/out | cost | tools |\n|---|---|---|---|---|---|---|\n");
        for (name, a) in &self.agents {
            let tools: Vec<String> = a.tools.iter().map(|(t, n)| format!("{t}×{n}")).collect();
            out.push_str(&format!(
                "| {name} | {} | {} | {} | {}/{} | ${:.3} | {} |\n",
                a.model_calls,
                a.tool_calls,
                a.tool_errors,
                a.input_tokens,
                a.output_tokens,
                a.usd,
                tools.join(" ")
            ));
        }
        out.push_str(&format!(
            "\nContext handoffs: {} · budget warnings: {} · events: {}\n",
            self.handoffs, self.budget_warnings, self.events
        ));
        if !self.repeated_queries.is_empty() {
            out.push_str("\nRepeated queries:\n");
            for q in &self.repeated_queries {
                out.push_str(&format!("- {q}\n"));
            }
        }
        if !self.errors.is_empty() {
            out.push_str("\nErrors:\n");
            for e in self.errors.iter().take(40) {
                out.push_str(&format!("- {e}\n"));
            }
        }
        out
    }
}

/// Prints a readable progress line per notable event to stderr.
pub struct ConsoleSink {
    pub verbose: bool,
}

impl Sink for ConsoleSink {
    fn on_event(&self, e: &Event) {
        let d = &e.data;
        let s = |k: &str| d[k].as_str().unwrap_or("").to_string();
        let line = match e.kind.as_str() {
            "run_started" => format!("▶ run {} · {}", e.run_id, s("query")),
            "run_resumed" => format!("▶ resuming run {} (phase: {})", e.run_id, s("phase")),
            "plan" if self.verbose => format!("  {} plan: {}", e.agent, clip(&s("text"), 300)),
            "subagent_started" => format!(
                "  ⇢ {} · {} (≤{} tool calls)",
                e.agent,
                s("title"),
                d["tool_budget"]
            ),
            "subagent_done" => format!(
                "  ✓ {} {} · {} tool calls · ${:.3}",
                e.agent,
                s("status"),
                d["tool_calls"],
                d["usd"].as_f64().unwrap_or(0.0)
            ),
            "tool_call" if self.verbose || e.agent == "lead" => {
                let detail = d["input"]["query"]
                    .as_str()
                    .or(d["input"]["url"].as_str())
                    .map(|q| format!(" “{}”", clip(q, 80)))
                    .unwrap_or_default();
                let err = if d["ok"] == false { " ✗" } else { "" };
                format!(
                    "    {} → {}{}{} ({} ms)",
                    e.agent,
                    s("tool"),
                    detail,
                    err,
                    d["ms"]
                )
            }
            "thinking" if self.verbose => {
                format!("    {} thinks: {}", e.agent, clip(&s("text"), 240))
            }
            "model_call" if self.verbose => format!(
                "    {} · {} · {} in / {} out · ${:.4}",
                e.agent,
                s("model"),
                d["prompt_tokens"],
                d["output_tokens"],
                d["usd"].as_f64().unwrap_or(0.0)
            ),
            "handoff" => format!(
                "  ↻ {}: context reset at {} tokens; continuing from memory",
                e.agent, d["prompt_tokens"]
            ),
            "budget_warning" => format!("  ! {}", s("message")),
            "agent_error" | "error" => format!("  ✗ {}: {}", e.agent, s("message")),
            "report_written" => format!("  ✎ report drafted ({} chars)", d["chars"]),
            "citations" => format!(
                "  ❝ {} claims cited from {} sources ({} dropped)",
                d["claims"], d["sources"], d["dropped"]
            ),
            "run_finished" => format!("■ {} · {}", s("status"), s("summary")),
            _ => return,
        };
        eprintln!("{line}");
    }
}
