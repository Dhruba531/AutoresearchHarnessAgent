//! Model providers behind one small trait.
//!
//! Conversations are kept in the Anthropic Messages wire format (content blocks:
//! `text`, `thinking`, `tool_use`, `tool_result`, server-tool blocks). Claude
//! reads it natively; the OpenAI-compatible adapter converts on the way out and
//! back. Histories are append-only: an agent never edits an earlier message, so
//! thinking blocks stay valid and the prompt cache keeps hitting.

pub mod anthropic;
pub mod openai;
pub mod pricing;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;
use std::time::Duration;

use crate::config::{Config, ModelRef, Provider};

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_input_tokens: u64,
    pub cache_creation_input_tokens: u64,
    pub web_search_requests: u64,
}

impl Usage {
    /// Tokens the model read for this request (the size of its context).
    pub fn prompt_tokens(&self) -> u64 {
        self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens
    }
}

pub struct ChatRequest<'a> {
    pub system: &'a str,
    /// Tool definitions: client tools (`name`/`description`/`input_schema`) and
    /// provider server tools (objects with a `type`, e.g. `web_search_20260209`).
    pub tools: &'a [Value],
    pub messages: &'a [Value],
    pub max_tokens: u32,
    pub effort: Option<&'a str>,
    pub thinking_display: Option<&'a str>,
}

#[derive(Clone, Debug)]
pub struct ChatResponse {
    /// Assistant content blocks, ready to append to the history unchanged.
    pub content: Vec<Value>,
    /// end_turn | tool_use | max_tokens | pause_turn | stop_sequence | ...
    pub stop_reason: String,
    pub usage: Usage,
    /// The model that actually served the request (may differ after a fallback).
    pub model: String,
    pub cost_usd: f64,
    /// `tool_use` ids whose streamed input was not valid JSON, with the raw text.
    pub invalid_tool_inputs: Vec<(String, String)>,
    pub duration_ms: u64,
}

impl ChatResponse {
    pub fn text(&self) -> String {
        text_of(&self.content)
    }
}

/// Concatenated `text` blocks of a content array.
pub fn text_of(content: &[Value]) -> String {
    content
        .iter()
        .filter(|b| b["type"] == "text")
        .filter_map(|b| b["text"].as_str())
        .collect::<Vec<_>>()
        .join("")
}

#[derive(thiserror::Error, Debug, Clone)]
pub enum LlmError {
    #[error("authentication failed: {0}")]
    Auth(String),
    #[error("rate limited: {0}")]
    RateLimited(String, Option<u64>),
    #[error("provider unavailable: {0}")]
    Unavailable(String),
    #[error("network error: {0}")]
    Network(String),
    #[error("request rejected: {0}")]
    BadRequest(String),
    #[error("the model declined the request (refusal{0})")]
    Refusal(String),
    #[error("{0}")]
    Other(String),
}

impl LlmError {
    pub fn retryable(&self) -> bool {
        matches!(
            self,
            LlmError::RateLimited(..) | LlmError::Unavailable(_) | LlmError::Network(_)
        )
    }
}

#[async_trait]
pub trait Model: Send + Sync {
    /// The model id requests are sent with.
    fn id(&self) -> &str;
    fn provider(&self) -> &str;
    /// Whether this model can run the provider's server-side web search.
    fn supports_server_search(&self) -> bool {
        false
    }
    async fn chat(&self, req: &ChatRequest<'_>) -> Result<ChatResponse, LlmError>;
}

/// Build the model for a reference, using the keys and base URLs in `config`.
pub fn build_model(
    config: &Config,
    r: &ModelRef,
    http: &reqwest::Client,
) -> anyhow::Result<Arc<dyn Model>> {
    let key = config.key_for(r.provider);
    let base = config.base_url_for(r.provider);
    Ok(match r.provider {
        Provider::Anthropic => {
            let key = key
                .ok_or_else(|| anyhow::anyhow!("ANTHROPIC_API_KEY is not set (needed for {r})"))?;
            Arc::new(anthropic::AnthropicModel::new(
                http.clone(),
                &base,
                &key,
                &r.model,
            ))
        }
        p => {
            let key = key.ok_or_else(|| {
                anyhow::anyhow!(
                    "{}_API_KEY is not set (needed for {r})",
                    p.label().to_ascii_uppercase()
                )
            })?;
            Arc::new(openai::OpenAiModel::new(
                http.clone(),
                p,
                &base,
                &key,
                &r.model,
            ))
        }
    })
}

/// Call the model, retrying rate limits, overloads and network errors with
/// exponential backoff (2s, 4s, 8s, ... plus jitter, or the server's retry-after).
pub async fn chat_with_retry(
    model: &dyn Model,
    req: &ChatRequest<'_>,
    max_retries: u32,
) -> Result<ChatResponse, LlmError> {
    let mut attempt = 0;
    loop {
        match model.chat(req).await {
            Ok(r) => return Ok(r),
            Err(e) if e.retryable() && attempt < max_retries => {
                let base = 2u64.pow(attempt + 1) * 1000;
                let wait = match &e {
                    LlmError::RateLimited(_, Some(secs)) => (*secs * 1000).max(base),
                    _ => base,
                };
                attempt += 1;
                tokio::time::sleep(Duration::from_millis(wait + crate::util::jitter(1000))).await;
            }
            Err(e) => return Err(e),
        }
    }
}

/// Map an HTTP status and body to an error.
pub(crate) fn status_error(
    label: &str,
    status: u16,
    body: &str,
    retry_after: Option<u64>,
) -> LlmError {
    let detail = format!("{label} {status}: {}", crate::util::clip(body.trim(), 600));
    match status {
        401 | 403 => LlmError::Auth(detail),
        429 => LlmError::RateLimited(detail, retry_after),
        408 | 409 | 500 | 502 | 503 | 504 | 529 => LlmError::Unavailable(detail),
        _ => LlmError::BadRequest(detail),
    }
}

pub(crate) fn transport_error(label: &str, e: reqwest::Error) -> LlmError {
    LlmError::Network(format!("{label}: {e}"))
}
