//! Claude over the Messages API, streamed as server-sent events.
//!
//! There is no official Anthropic SDK for Rust, so this speaks the HTTP API
//! directly (`POST /v1/messages`, `anthropic-version: 2026-10-21`). It streams,
//! because research turns can think and write for minutes, and rebuilds the
//! final message from the events: text, thinking (with signatures), tool calls
//! whose JSON input arrives in fragments, citations and server-tool blocks.
//!
//! Per request it turns on adaptive thinking and effort where the model takes
//! them, top-level prompt caching (the agent loops are append-only, so each turn
//! reads the previous turn's prefix from cache), and the server-side refusal
//! fallback (`fallbacks: "default"`) for the models that accept it.

use async_trait::async_trait;
use futures::StreamExt;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use super::{
    pricing, status_error, transport_error, ChatRequest, ChatResponse, LlmError, Model, Usage,
};

pub const API_VERSION: &str = "2026-10-21";
pub const FALLBACK_BETA: &str = "server-side-fallback-2026-07-01";
const DEFAULT_BASE: &str = "https://api.anthropic.com";
const IDLE_TIMEOUT: Duration = Duration::from_secs(300);

/// Models that take `thinking: {type: "adaptive"}` and `output_config.effort`.
fn is_adaptive(model: &str) -> bool {
    [
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-5-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ]
    .iter()
    .any(|p| model.starts_with(p))
}

/// Models that accept the server-side refusal fallback in its `"default"` form.
fn takes_fallbacks(model: &str) -> bool {
    [
        "claude-fable-5-1",
        "claude-mythos-5-1",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
    ]
    .contains(&model)
}

/// Models with the `_20260209` web search / fetch tools (dynamic filtering).
fn has_dynamic_web_tools(model: &str) -> bool {
    [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ]
    .iter()
    .any(|p| model.starts_with(p))
}

pub struct AnthropicModel {
    http: reqwest::Client,
    base_url: String,
    api_key: String,
    model: String,
    official_endpoint: bool,
    use_fallbacks: AtomicBool,
}

impl AnthropicModel {
    /// Fallbacks and eager tool streaming are only sent to the official endpoint;
    /// proxies and gateways may reject them.
    pub fn new(
        http: reqwest::Client,
        base_url: &str,
        api_key: &str,
        model: &str,
    ) -> AnthropicModel {
        let official = base_url.trim_end_matches('/') == DEFAULT_BASE;
        AnthropicModel::with_options(http, base_url, api_key, model, official)
    }

    pub fn with_options(
        http: reqwest::Client,
        base_url: &str,
        api_key: &str,
        model: &str,
        official_features: bool,
    ) -> AnthropicModel {
        AnthropicModel {
            http,
            official_endpoint: official_features,
            use_fallbacks: AtomicBool::new(official_features && takes_fallbacks(model)),
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            model: model.to_string(),
        }
    }

    /// The server-side web tools this model should use.
    pub fn web_tool_types(model: &str) -> (&'static str, &'static str) {
        if has_dynamic_web_tools(model) {
            ("web_search_20260209", "web_fetch_20260209")
        } else {
            ("web_search_20250305", "web_fetch_20250910")
        }
    }

    fn body(&self, req: &ChatRequest<'_>, fallbacks: bool) -> Value {
        let mut body = json!({
            "model": self.model,
            "max_tokens": req.max_tokens,
            "system": [{"type": "text", "text": req.system}],
            "messages": req.messages,
            "stream": true,
            "cache_control": {"type": "ephemeral"},
        });
        if !req.tools.is_empty() {
            let tools: Vec<Value> = req
                .tools
                .iter()
                .map(|t| {
                    let mut t = t.clone();
                    // Client tools stream their input as it is generated; the agent
                    // loop validates every input against its schema before running it.
                    // Proxies may reject the field, so only send it to the real API.
                    if t.get("type").is_none() && self.official_endpoint {
                        t["eager_input_streaming"] = json!(true);
                    }
                    t
                })
                .collect();
            body["tools"] = Value::Array(tools);
        }
        if is_adaptive(&self.model) {
            let mut thinking = json!({"type": "adaptive"});
            if let Some(d) = req.thinking_display.filter(|d| !d.is_empty()) {
                thinking["display"] = json!(d);
            }
            body["thinking"] = thinking;
            if let Some(e) = req.effort {
                body["output_config"] = json!({"effort": e});
            }
        }
        if fallbacks {
            body["fallbacks"] = json!("default");
        }
        body
    }

    async fn send(&self, req: &ChatRequest<'_>, fallbacks: bool) -> Result<ChatResponse, LlmError> {
        let started = Instant::now();
        let mut rb = self
            .http
            .post(format!("{}/v1/messages", self.base_url))
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", API_VERSION)
            .header("content-type", "application/json")
            .header("accept", "text/event-stream")
            .json(&self.body(req, fallbacks));
        if fallbacks {
            rb = rb.header("anthropic-beta", FALLBACK_BETA);
        }
        let resp = rb
            .send()
            .await
            .map_err(|e| transport_error("Anthropic", e))?;
        let status = resp.status().as_u16();
        if status != 200 {
            let retry_after = resp
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.parse().ok());
            let text = resp.text().await.unwrap_or_default();
            return Err(status_error("Anthropic", status, &text, retry_after));
        }

        let mut decoder = SseDecoder::default();
        let mut acc = Accumulator::default();
        let mut stream = resp.bytes_stream();
        loop {
            let next = tokio::time::timeout(IDLE_TIMEOUT, stream.next())
                .await
                .map_err(|_| LlmError::Network("Anthropic stream idle for 5 minutes".into()))?;
            let Some(chunk) = next else { break };
            let chunk = chunk.map_err(|e| transport_error("Anthropic stream", e))?;
            for (event, data) in decoder.push(&chunk) {
                if acc.apply(&event, &data)? {
                    break;
                }
            }
            if acc.done {
                break;
            }
        }
        let mut out = acc.finish(&self.model)?;
        out.cost_usd = pricing::cost(&out.model, "anthropic", &out.usage);
        out.duration_ms = started.elapsed().as_millis() as u64;
        Ok(out)
    }
}

#[async_trait]
impl Model for AnthropicModel {
    fn id(&self) -> &str {
        &self.model
    }

    fn provider(&self) -> &str {
        "anthropic"
    }

    fn supports_server_search(&self) -> bool {
        true
    }

    async fn chat(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError> {
        let fallbacks = self.use_fallbacks.load(Ordering::Relaxed);
        match self.send(req, fallbacks).await {
            // Accounts or models without the fallback beta: retry once without it.
            Err(LlmError::BadRequest(msg))
                if fallbacks && msg.to_lowercase().contains("fallback") =>
            {
                self.use_fallbacks.store(false, Ordering::Relaxed);
                self.send(req, false).await
            }
            other => other,
        }
    }
}

// ---- SSE decoding ----------------------------------------------------------------

/// Incremental server-sent-events decoder. Bytes may split anywhere, including
/// inside a UTF-8 sequence, so lines are only decoded once complete.
#[derive(Default)]
pub struct SseDecoder {
    buf: Vec<u8>,
    event: String,
    data: String,
}

impl SseDecoder {
    pub fn push(&mut self, bytes: &[u8]) -> Vec<(String, String)> {
        self.buf.extend_from_slice(bytes);
        let mut out = Vec::new();
        while let Some(pos) = self.buf.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buf.drain(..=pos).collect();
            let line = String::from_utf8_lossy(&line);
            let line = line.trim_end_matches(['\n', '\r']);
            if line.is_empty() {
                if !self.data.is_empty() || !self.event.is_empty() {
                    let ev = if self.event.is_empty() {
                        "message".to_string()
                    } else {
                        std::mem::take(&mut self.event)
                    };
                    out.push((ev, std::mem::take(&mut self.data)));
                    self.event.clear();
                }
            } else if let Some(v) = line.strip_prefix("event:") {
                self.event = v.trim().to_string();
            } else if let Some(v) = line.strip_prefix("data:") {
                if !self.data.is_empty() {
                    self.data.push('\n');
                }
                self.data.push_str(v.strip_prefix(' ').unwrap_or(v));
            }
        }
        out
    }
}

// ---- rebuilding the message ------------------------------------------------------

#[derive(Default)]
pub struct Accumulator {
    content: Vec<Value>,
    json_bufs: HashMap<usize, String>,
    stop_reason: Option<String>,
    stop_details: Value,
    usage: Usage,
    model: Option<String>,
    pub done: bool,
}

fn set_usage(u: &mut Usage, v: &Value) {
    let get = |k: &str| v.get(k).and_then(Value::as_u64);
    if let Some(x) = get("input_tokens") {
        u.input_tokens = x;
    }
    if let Some(x) = get("output_tokens") {
        u.output_tokens = x;
    }
    if let Some(x) = get("cache_read_input_tokens") {
        u.cache_read_input_tokens = x;
    }
    if let Some(x) = get("cache_creation_input_tokens") {
        u.cache_creation_input_tokens = x;
    }
    if let Some(x) = v
        .pointer("/server_tool_use/web_search_requests")
        .and_then(Value::as_u64)
    {
        u.web_search_requests = x;
    }
}

/// Drop null-valued keys so echoed blocks carry only what the API sent.
fn clean(mut block: Value) -> Value {
    if let Value::Object(map) = &mut block {
        map.retain(|_, v| !v.is_null());
    }
    block
}

impl Accumulator {
    /// Apply one event. Returns `Ok(true)` once the message is complete.
    pub fn apply(&mut self, event: &str, data: &str) -> Result<bool, LlmError> {
        if data.is_empty() || event == "ping" {
            return Ok(false);
        }
        let v: Value = serde_json::from_str(data)
            .map_err(|e| LlmError::Other(format!("bad SSE payload ({e}): {data}")))?;
        let kind = v["type"].as_str().unwrap_or(event);
        match kind {
            "message_start" => {
                self.model = v
                    .pointer("/message/model")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                if let Some(u) = v.pointer("/message/usage") {
                    set_usage(&mut self.usage, u);
                }
            }
            "content_block_start" => {
                let idx = v["index"].as_u64().unwrap_or(self.content.len() as u64) as usize;
                let block = clean(v["content_block"].clone());
                if matches!(
                    block["type"].as_str(),
                    Some("tool_use" | "server_tool_use" | "mcp_tool_use")
                ) {
                    self.json_bufs.insert(idx, String::new());
                }
                while self.content.len() < idx {
                    self.content.push(Value::Null);
                }
                if idx < self.content.len() {
                    self.content[idx] = block;
                } else {
                    self.content.push(block);
                }
            }
            "content_block_delta" => {
                let idx = v["index"].as_u64().unwrap_or(0) as usize;
                let d = &v["delta"];
                let Some(block) = self.content.get_mut(idx) else {
                    return Ok(false);
                };
                match d["type"].as_str().unwrap_or("") {
                    "text_delta" => append(block, "text", d["text"].as_str().unwrap_or("")),
                    "thinking_delta" => {
                        append(block, "thinking", d["thinking"].as_str().unwrap_or(""))
                    }
                    "signature_delta" => block["signature"] = d["signature"].clone(),
                    "input_json_delta" => {
                        self.json_bufs
                            .entry(idx)
                            .or_default()
                            .push_str(d["partial_json"].as_str().unwrap_or(""));
                    }
                    "citations_delta" => {
                        let c = d["citation"].clone();
                        match block.get_mut("citations").and_then(Value::as_array_mut) {
                            Some(arr) => arr.push(c),
                            None => block["citations"] = json!([c]),
                        }
                    }
                    _ => {}
                }
            }
            "content_block_stop" => {
                let idx = v["index"].as_u64().unwrap_or(0) as usize;
                if let (Some(raw), Some(block)) =
                    (self.json_bufs.get(&idx), self.content.get_mut(idx))
                {
                    let input = if raw.trim().is_empty() {
                        Some(json!({}))
                    } else {
                        serde_json::from_str::<Value>(raw)
                            .ok()
                            .filter(Value::is_object)
                    };
                    match input {
                        Some(i) => {
                            block["input"] = i;
                            self.json_bufs.remove(&idx);
                        }
                        None => block["input"] = json!({}), // keep raw text in json_bufs for the error
                    }
                }
            }
            "message_delta" => {
                if let Some(r) = v.pointer("/delta/stop_reason").and_then(Value::as_str) {
                    self.stop_reason = Some(r.to_string());
                }
                if let Some(d) = v.pointer("/delta/stop_details") {
                    self.stop_details = d.clone();
                }
                if let Some(u) = v.get("usage") {
                    set_usage(&mut self.usage, u);
                }
            }
            "message_stop" => {
                self.done = true;
                return Ok(true);
            }
            "error" => {
                let msg = v
                    .pointer("/error/message")
                    .and_then(Value::as_str)
                    .unwrap_or("stream error")
                    .to_string();
                return Err(match v.pointer("/error/type").and_then(Value::as_str) {
                    Some("overloaded_error" | "api_error") => LlmError::Unavailable(msg),
                    Some("rate_limit_error") => LlmError::RateLimited(msg, None),
                    Some("authentication_error" | "permission_error") => LlmError::Auth(msg),
                    Some("invalid_request_error") => LlmError::BadRequest(msg),
                    _ => LlmError::Other(msg),
                });
            }
            _ => {}
        }
        Ok(false)
    }

    pub fn finish(mut self, requested_model: &str) -> Result<ChatResponse, LlmError> {
        let stop = self.stop_reason.clone().unwrap_or_else(|| {
            if self.done {
                "end_turn".into()
            } else {
                "incomplete".into()
            }
        });
        if !self.done && self.stop_reason.is_none() {
            return Err(LlmError::Network("stream ended before message_stop".into()));
        }
        if stop == "refusal" {
            let cat = self.stop_details["category"]
                .as_str()
                .map(|c| format!(": {c}"))
                .unwrap_or_default();
            return Err(LlmError::Refusal(cat));
        }
        let mut invalid = Vec::new();
        for (idx, raw) in &self.json_bufs {
            if let Some(id) = self.content.get(*idx).and_then(|b| b["id"].as_str()) {
                invalid.push((id.to_string(), raw.clone()));
            }
        }
        self.content.retain(|b| !b.is_null());
        let content = sanitize_fallback(self.content);
        Ok(ChatResponse {
            content,
            stop_reason: stop,
            usage: self.usage,
            model: self.model.unwrap_or_else(|| requested_model.to_string()),
            cost_usd: 0.0,
            invalid_tool_inputs: invalid,
            duration_ms: 0,
        })
    }
}

fn append(block: &mut Value, key: &str, s: &str) {
    let cur = block
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    block[key] = Value::String(cur + s);
}

/// After a mid-output fallback, the declined partial before the last `fallback`
/// block keeps only its text and paired server-tool blocks; its thinking and
/// unexecuted tool calls are dropped so they are neither run nor echoed back.
pub fn sanitize_fallback(content: Vec<Value>) -> Vec<Value> {
    let Some(boundary) = content.iter().rposition(|b| b["type"] == "fallback") else {
        return content;
    };
    let result_ids: Vec<String> = content[..boundary]
        .iter()
        .filter(|b| {
            b["type"]
                .as_str()
                .is_some_and(|t| t.ends_with("_tool_result"))
        })
        .filter_map(|b| b["tool_use_id"].as_str().map(str::to_string))
        .collect();
    let mut out = Vec::with_capacity(content.len());
    for (i, b) in content.into_iter().enumerate() {
        if i >= boundary {
            out.push(b);
            continue;
        }
        let t = b["type"].as_str().unwrap_or("");
        let keep = match t {
            "text" => true,
            "server_tool_use" => b["id"]
                .as_str()
                .is_some_and(|id| result_ids.iter().any(|r| r == id)),
            t if t.ends_with("_tool_result") => true,
            _ => false,
        };
        if keep {
            out.push(b);
        }
    }
    out
}

/// Every object with a `url` inside server-tool results and citations, as
/// (url, title, text) triples: search results, fetched documents, cited spans.
pub fn harvest_sources(content: &[Value]) -> Vec<(String, String, String)> {
    let mut out = Vec::new();
    fn walk(v: &Value, out: &mut Vec<(String, String, String)>) {
        match v {
            Value::Object(m) => {
                if let Some(url) = m.get("url").and_then(Value::as_str) {
                    let title = m
                        .get("title")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string();
                    let text = text_field(m);
                    out.push((url.to_string(), title, text));
                }
                for (k, child) in m {
                    if k != "encrypted_content" && k != "encrypted_index" {
                        walk(child, out);
                    }
                }
            }
            Value::Array(a) => a.iter().for_each(|c| walk(c, out)),
            _ => {}
        }
    }
    fn text_field(m: &Map<String, Value>) -> String {
        if let Some(t) = m.get("cited_text").and_then(Value::as_str) {
            return t.to_string();
        }
        // web_fetch_result: {url, content: {type: document, source: {data}}}
        m.get("content")
            .and_then(|c| c.pointer("/source/data"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string()
    }
    for b in content {
        let t = b["type"].as_str().unwrap_or("");
        if t.ends_with("_tool_result") || (t == "text" && b.get("citations").is_some()) {
            walk(b, &mut out);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn feed(acc: &mut Accumulator, raw: &str) {
        let mut d = SseDecoder::default();
        // Split mid-line to exercise buffering.
        let (a, b) = raw.split_at(raw.len() / 2);
        for (e, data) in d.push(a.as_bytes()).into_iter().chain(d.push(b.as_bytes())) {
            acc.apply(&e, &data).unwrap();
        }
    }

    #[test]
    fn rebuilds_text_thinking_and_tool_calls() {
        let raw = concat!(
            "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"model\":\"claude-opus-5-5\",\"usage\":{\"input_tokens\":120,\"cache_read_input_tokens\":30,\"output_tokens\":1}}}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"thinking\",\"thinking\":\"\",\"signature\":\"\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"Plan: search.\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"signature_delta\",\"signature\":\"sig\"}}\n\n",
            "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
            "event: ping\ndata: {\"type\":\"ping\"}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"text_delta\",\"text\":\"Héllo \"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"text_delta\",\"text\":\"world\"}}\n\n",
            "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":1}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":2,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_1\",\"name\":\"web_search\",\"input\":{}}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":2,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"query\\\": \\\"ru\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":2,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"st\\\"}\"}}\n\n",
            "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":2}\n\n",
            "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":42}}\n\n",
            "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        );
        let mut acc = Accumulator::default();
        feed(&mut acc, raw);
        let r = acc.finish("claude-opus-5-5").unwrap();
        assert_eq!(r.stop_reason, "tool_use");
        assert_eq!(r.content.len(), 3);
        assert_eq!(r.content[0]["thinking"], "Plan: search.");
        assert_eq!(r.content[0]["signature"], "sig");
        assert_eq!(r.text(), "Héllo world");
        assert_eq!(r.content[2]["input"]["query"], "rust");
        assert_eq!(r.usage.input_tokens, 120);
        assert_eq!(r.usage.cache_read_input_tokens, 30);
        assert_eq!(r.usage.output_tokens, 42);
        assert!(r.invalid_tool_inputs.is_empty());
    }

    #[test]
    fn invalid_tool_json_is_reported_not_run() {
        let raw = concat!(
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"t1\",\"name\":\"x\",\"input\":{}}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"a\\\": \"}}\n\n",
            "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
            "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"max_tokens\"}}\n\n",
            "data: {\"type\":\"message_stop\"}\n\n",
        );
        let mut acc = Accumulator::default();
        feed(&mut acc, raw);
        let r = acc.finish("m").unwrap();
        assert_eq!(r.content[0]["input"], json!({}));
        assert_eq!(
            r.invalid_tool_inputs,
            vec![("t1".to_string(), "{\"a\": ".to_string())]
        );
    }

    #[test]
    fn refusal_and_stream_errors() {
        let mut acc = Accumulator::default();
        feed(&mut acc, "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"refusal\",\"stop_details\":{\"category\":\"cyber\"}}}\n\ndata: {\"type\":\"message_stop\"}\n\n");
        assert!(matches!(acc.finish("m"), Err(LlmError::Refusal(c)) if c.contains("cyber")));
        let mut acc = Accumulator::default();
        let err = acc.apply("error", "{\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}");
        assert!(matches!(err, Err(LlmError::Unavailable(_))));
    }

    #[test]
    fn fallback_partial_is_sanitized() {
        let content = vec![
            json!({"type": "thinking", "thinking": "", "signature": "a"}),
            json!({"type": "text", "text": "partial "}),
            json!({"type": "tool_use", "id": "t0", "name": "x", "input": {}}),
            json!({"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}}),
            json!({"type": "text", "text": "rest"}),
            json!({"type": "tool_use", "id": "t1", "name": "x", "input": {}}),
        ];
        let out = sanitize_fallback(content);
        let kinds: Vec<&str> = out.iter().map(|b| b["type"].as_str().unwrap()).collect();
        assert_eq!(kinds, vec!["text", "fallback", "text", "tool_use"]);
        assert_eq!(out[3]["id"], "t1");
    }

    #[test]
    fn request_body_shape() {
        let m = AnthropicModel::new(reqwest::Client::new(), DEFAULT_BASE, "k", "claude-opus-5-5");
        let tools = vec![
            json!({"name": "t", "description": "d", "input_schema": {"type": "object"}}),
            json!({"type": "web_search_20260209", "name": "web_search"}),
        ];
        let msgs = vec![json!({"role": "user", "content": "hi"})];
        let req = ChatRequest {
            system: "s",
            tools: &tools,
            messages: &msgs,
            max_tokens: 1000,
            effort: Some("high"),
            thinking_display: Some("summarized"),
        };
        let b = m.body(&req, true);
        assert_eq!(b["thinking"]["type"], "adaptive");
        assert_eq!(b["output_config"]["effort"], "high");
        assert_eq!(b["fallbacks"], "default");
        assert_eq!(b["tools"][0]["eager_input_streaming"], true);
        assert!(b["tools"][1].get("eager_input_streaming").is_none());
        assert!(b.get("temperature").is_none());
        let old = AnthropicModel::new(
            reqwest::Client::new(),
            "http://proxy.local",
            "k",
            "claude-haiku-4-5",
        );
        let b = old.body(&req, false);
        assert!(b.get("thinking").is_none() && b.get("fallbacks").is_none());
        assert!(b["tools"][0].get("eager_input_streaming").is_none());
    }

    #[test]
    fn harvests_server_tool_sources() {
        let content = vec![
            json!({"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                {"type": "web_search_result", "url": "https://a.org/x", "title": "A", "encrypted_content": "zz"}]}),
            json!({"type": "text", "text": "claim", "citations": [
                {"type": "web_search_result_location", "url": "https://a.org/x", "title": "A", "cited_text": "the quote"}]}),
        ];
        let s = harvest_sources(&content);
        assert_eq!(s.len(), 2);
        assert_eq!(s[1].2, "the quote");
    }
}
