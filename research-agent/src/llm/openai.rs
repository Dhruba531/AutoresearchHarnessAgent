//! OpenAI-compatible chat completions with function calling: OpenAI, DeepSeek,
//! Ollama, LM Studio, OpenRouter and anything else that speaks the same API.
//!
//! The history stays in the Anthropic block format; this adapter converts it on
//! every request (tool_use → `tool_calls`, tool_result → `role: tool`) and maps
//! the reply back. Thinking blocks and provider server tools are not sent.

use async_trait::async_trait;
use serde_json::{json, Value};
use std::time::{Duration, Instant};

use super::{
    pricing, status_error, transport_error, ChatRequest, ChatResponse, LlmError, Model, Usage,
};
use crate::config::Provider;

pub struct OpenAiModel {
    http: reqwest::Client,
    provider: Provider,
    base_url: String,
    api_key: String,
    model: String,
}

impl OpenAiModel {
    pub fn new(
        http: reqwest::Client,
        provider: Provider,
        base_url: &str,
        api_key: &str,
        model: &str,
    ) -> OpenAiModel {
        OpenAiModel {
            http,
            provider,
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            model: model.to_string(),
        }
    }

    fn body(&self, req: &ChatRequest<'_>) -> Value {
        let mut body = json!({
            "model": self.model,
            "messages": to_openai_messages(req.system, req.messages),
        });
        let tools = to_openai_tools(req.tools);
        if !tools.is_empty() {
            body["tools"] = Value::Array(tools);
        }
        // OpenAI's own API moved to max_completion_tokens; compatible servers still read max_tokens.
        let official_openai =
            self.provider == Provider::Openai && self.base_url.contains("api.openai.com");
        if official_openai {
            body["max_completion_tokens"] = json!(req.max_tokens);
        } else {
            let cap = if self.provider == Provider::Deepseek {
                8192
            } else {
                req.max_tokens
            };
            body["max_tokens"] = json!(req.max_tokens.min(cap));
        }
        body
    }
}

#[async_trait]
impl Model for OpenAiModel {
    fn id(&self) -> &str {
        &self.model
    }

    fn provider(&self) -> &str {
        self.provider.label()
    }

    async fn chat(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        let started = Instant::now();
        let label = self.provider.label();
        let resp = self
            .http
            .post(format!("{}/chat/completions", self.base_url))
            .bearer_auth(&self.api_key)
            .timeout(Duration::from_secs(900))
            .json(&self.body(req))
            .send()
            .await
            .map_err(|e| transport_error(label, e))?;
        let status = resp.status().as_u16();
        let retry_after = resp
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse().ok());
        let text = resp.text().await.map_err(|e| transport_error(label, e))?;
        if status != 200 {
            return Err(status_error(label, status, &text, retry_after));
        }
        let v: Value = serde_json::from_str(&text)
            .map_err(|e| LlmError::Other(format!("{label}: bad JSON ({e})")))?;
        let mut out = from_openai_response(&v, &self.model)?;
        out.cost_usd = pricing::cost(&out.model, label, &out.usage);
        out.duration_ms = started.elapsed().as_millis() as u64;
        Ok(out)
    }
}

fn block_text(content: &Value) -> String {
    match content {
        Value::String(s) => s.clone(),
        Value::Array(blocks) => blocks
            .iter()
            .filter_map(|b| {
                if b["type"] == "text" {
                    b["text"].as_str()
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// Anthropic-format history → chat-completions messages.
pub fn to_openai_messages(system: &str, messages: &[Value]) -> Vec<Value> {
    let mut out = vec![json!({"role": "system", "content": system})];
    for m in messages {
        let role = m["role"].as_str().unwrap_or("user");
        let content = &m["content"];
        match (role, content) {
            (_, Value::String(s)) => out.push(json!({"role": role, "content": s})),
            ("assistant", Value::Array(blocks)) => {
                let text = block_text(content);
                let calls: Vec<Value> = blocks
                    .iter()
                    .filter(|b| b["type"] == "tool_use")
                    .map(|b| {
                        json!({"id": b["id"], "type": "function",
                               "function": {"name": b["name"], "arguments": b["input"].to_string()}})
                    })
                    .collect();
                if text.is_empty() && calls.is_empty() {
                    continue; // e.g. a turn that held only thinking blocks
                }
                let mut msg = json!({"role": "assistant", "content": if text.is_empty() { Value::Null } else { json!(text) }});
                if !calls.is_empty() {
                    msg["tool_calls"] = Value::Array(calls);
                }
                out.push(msg);
            }
            (_, Value::Array(blocks)) => {
                // Tool results must directly follow the assistant message that called them.
                for b in blocks.iter().filter(|b| b["type"] == "tool_result") {
                    let mut text = block_text(&b["content"]);
                    if b["is_error"] == true {
                        text = format!("ERROR: {text}");
                    }
                    out.push(
                        json!({"role": "tool", "tool_call_id": b["tool_use_id"], "content": text}),
                    );
                }
                let text: Vec<&str> = blocks
                    .iter()
                    .filter(|b| b["type"] == "text")
                    .filter_map(|b| b["text"].as_str())
                    .collect();
                if !text.is_empty() {
                    out.push(json!({"role": "user", "content": text.join("\n")}));
                }
            }
            _ => {}
        }
    }
    out
}

/// Client tool definitions → `{"type": "function", ...}`. Server tools are skipped.
pub fn to_openai_tools(tools: &[Value]) -> Vec<Value> {
    tools
        .iter()
        .filter(|t| t.get("type").is_none())
        .map(|t| {
            json!({"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}})
        })
        .collect()
}

pub fn from_openai_response(v: &Value, requested: &str) -> Result<ChatResponse, LlmError> {
    let choice = v
        .pointer("/choices/0")
        .ok_or_else(|| LlmError::Other(format!("no choices in reply: {v}")))?;
    let msg = &choice["message"];
    let mut content = Vec::new();
    if let Some(t) = msg["content"].as_str().filter(|t| !t.trim().is_empty()) {
        content.push(json!({"type": "text", "text": t}));
    }
    let mut invalid = Vec::new();
    for (i, call) in msg["tool_calls"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
    {
        let id = call["id"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| format!("call_{i}_{}", crate::util::new_id()));
        let args = &call["function"]["arguments"];
        let input = match args {
            Value::String(s) if s.trim().is_empty() => Some(json!({})),
            Value::String(s) => serde_json::from_str::<Value>(s)
                .ok()
                .filter(Value::is_object),
            Value::Object(_) => Some(args.clone()),
            _ => None,
        };
        let input = input.unwrap_or_else(|| {
            invalid.push((id.clone(), args.as_str().unwrap_or("").to_string()));
            json!({})
        });
        content.push(
            json!({"type": "tool_use", "id": id, "name": call["function"]["name"], "input": input}),
        );
    }
    let has_calls = content.iter().any(|b| b["type"] == "tool_use");
    let stop = match choice["finish_reason"].as_str().unwrap_or("stop") {
        "tool_calls" | "function_call" => "tool_use",
        "length" => "max_tokens",
        "content_filter" => return Err(LlmError::Refusal(": content_filter".into())),
        _ if has_calls => "tool_use",
        _ => "end_turn",
    };
    let u = &v["usage"];
    let cached = u
        .pointer("/prompt_tokens_details/cached_tokens")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let prompt = u["prompt_tokens"].as_u64().unwrap_or(0);
    let usage = Usage {
        input_tokens: prompt.saturating_sub(cached),
        cache_read_input_tokens: cached,
        output_tokens: u["completion_tokens"].as_u64().unwrap_or(0),
        ..Usage::default()
    };
    Ok(ChatResponse {
        content,
        stop_reason: stop.into(),
        usage,
        model: v["model"].as_str().unwrap_or(requested).to_string(),
        cost_usd: 0.0,
        invalid_tool_inputs: invalid,
        duration_ms: 0,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn converts_tool_round_trip() {
        let history = vec![
            json!({"role": "user", "content": "find x"}),
            json!({"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hm", "signature": "s"},
                {"type": "text", "text": "Searching."},
                {"type": "tool_use", "id": "c1", "name": "web_search", "input": {"query": "x"}}]}),
            json!({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "result", "is_error": false},
                {"type": "text", "text": "[budget] 1 of 10 tool calls used"}]}),
        ];
        let m = to_openai_messages("sys", &history);
        assert_eq!(m.len(), 5);
        assert_eq!(
            m[2]["tool_calls"][0]["function"]["arguments"],
            "{\"query\":\"x\"}"
        );
        assert_eq!(m[2]["content"], "Searching.");
        assert_eq!(m[3]["role"], "tool");
        assert_eq!(m[3]["tool_call_id"], "c1");
        assert_eq!(m[4]["role"], "user");
    }

    #[test]
    fn parses_reply_with_tool_calls() {
        let v = json!({"model": "deepseek-chat", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": null, "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\":\"q\"}"}},
                {"id": "b", "type": "function", "function": {"name": "web_fetch", "arguments": "{oops"}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 40}}});
        let r = from_openai_response(&v, "x").unwrap();
        assert_eq!(r.stop_reason, "tool_use");
        assert_eq!(r.content.len(), 2);
        assert_eq!(r.content[0]["input"]["query"], "q");
        assert_eq!(r.invalid_tool_inputs.len(), 1);
        assert_eq!(r.usage.input_tokens, 60);
        assert_eq!(r.usage.cache_read_input_tokens, 40);
    }

    #[test]
    fn skips_server_tools() {
        let t = to_openai_tools(&[
            json!({"type": "web_search_20260209", "name": "web_search"}),
            json!({"name": "f", "description": "d", "input_schema": {"type": "object"}}),
        ]);
        assert_eq!(t.len(), 1);
        assert_eq!(t[0]["function"]["name"], "f");
    }
}
