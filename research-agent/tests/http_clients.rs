//! The real HTTP clients against local mock servers: Claude's streaming
//! Messages API, an OpenAI-compatible endpoint, and page fetching.

use axum::body::Body;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use research_agent::config::Provider;
use research_agent::llm::anthropic::AnthropicModel;
use research_agent::llm::openai::OpenAiModel;
use research_agent::llm::{ChatRequest, LlmError, Model};
use research_agent::tools::fetch::{Fetcher, WebFetchTool};
use research_agent::tools::sources::SourceRegistry;
use research_agent::tools::Tool;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};

const SSE: &str = concat!(
    "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"model\":\"claude-opus-5-5\",\"usage\":{\"input_tokens\":10,\"output_tokens\":1}}}\n\n",
    "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"Searching.\"}}\n\n",
    "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
    "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"toolu_9\",\"name\":\"web_search\",\"input\":{}}}\n\n",
    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"query\\\":\\\"rust\\\"}\"}}\n\n",
    "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":1}\n\n",
    "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"tool_use\"},\"usage\":{\"output_tokens\":30}}\n\n",
    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
);

#[derive(Default)]
struct Seen {
    headers: Vec<HeaderMap>,
    bodies: Vec<Value>,
}

async fn spawn(app: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    base
}

fn req<'a>(tools: &'a [Value], messages: &'a [Value]) -> ChatRequest<'a> {
    ChatRequest {
        system: "sys",
        tools,
        messages,
        max_tokens: 2000,
        effort: Some("high"),
        thinking_display: Some("summarized"),
    }
}

#[tokio::test]
async fn anthropic_streams_and_downgrades_fallbacks() {
    let seen = Arc::new(Mutex::new(Seen::default()));
    let s = seen.clone();
    let app = Router::new().route(
        "/v1/messages",
        post(move |headers: HeaderMap, Json(body): Json<Value>| {
            let s = s.clone();
            async move {
                let first = {
                    let mut g = s.lock().unwrap();
                    g.headers.push(headers);
                    g.bodies.push(body.clone());
                    g.bodies.len() == 1
                };
                if first && body.get("fallbacks").is_some() {
                    return (
                        StatusCode::BAD_REQUEST,
                        "{\"error\":{\"message\":\"fallbacks: not enabled for this account\"}}",
                    )
                        .into_response();
                }
                Response::builder()
                    .header("content-type", "text/event-stream")
                    .body(Body::from(SSE))
                    .unwrap()
            }
        }),
    );
    let base = spawn(app).await;
    let model = AnthropicModel::with_options(
        reqwest::Client::new(),
        &base,
        "sk-test",
        "claude-opus-5-5",
        true,
    );
    let tools =
        vec![json!({"name": "web_search", "description": "d", "input_schema": {"type": "object"}})];
    let msgs = vec![json!({"role": "user", "content": "hi"})];
    let r = model.chat(&req(&tools, &msgs)).await.unwrap();
    assert_eq!(r.stop_reason, "tool_use");
    assert_eq!(r.text(), "Searching.");
    assert_eq!(r.content[1]["input"]["query"], "rust");
    assert_eq!(r.usage.output_tokens, 30);
    assert!(r.cost_usd > 0.0);

    {
        let g = seen.lock().unwrap();
        assert_eq!(
            g.bodies.len(),
            2,
            "rejected fallbacks are retried once without them"
        );
        let h = &g.headers[0];
        assert_eq!(h["x-api-key"], "sk-test");
        assert_eq!(h["anthropic-version"], "2026-10-21");
        assert_eq!(h["anthropic-beta"], "server-side-fallback-2026-07-01");
        assert_eq!(g.bodies[0]["fallbacks"], "default");
        assert!(
            g.bodies[1].get("fallbacks").is_none() && g.headers[1].get("anthropic-beta").is_none()
        );
        let b = &g.bodies[1];
        assert_eq!(b["stream"], true);
        assert_eq!(
            b["thinking"],
            json!({"type": "adaptive", "display": "summarized"})
        );
        assert_eq!(b["output_config"]["effort"], "high");
        assert_eq!(b["cache_control"]["type"], "ephemeral");
        assert_eq!(b["tools"][0]["eager_input_streaming"], true);
    }

    // Later calls remember that fallbacks are off.
    model.chat(&req(&tools, &msgs)).await.unwrap();
    assert_eq!(seen.lock().unwrap().bodies.len(), 3);
}

#[tokio::test]
async fn anthropic_maps_http_errors() {
    let app = Router::new()
        .route(
            "/a/v1/messages",
            post(|| async {
                (
                    StatusCode::TOO_MANY_REQUESTS,
                    [("retry-after", "7")],
                    "slow down",
                )
            }),
        )
        .route(
            "/b/v1/messages",
            post(|| async { (StatusCode::UNAUTHORIZED, "bad key") }),
        )
        .route(
            "/c/v1/messages",
            post(|| async { (StatusCode::from_u16(529).unwrap(), "overloaded") }),
        );
    let base = spawn(app).await;
    let msgs = vec![json!({"role": "user", "content": "hi"})];
    let call = |path: &str| {
        AnthropicModel::new(
            reqwest::Client::new(),
            &format!("{base}/{path}"),
            "k",
            "claude-sonnet-5-5",
        )
    };
    assert!(matches!(
        call("a").chat(&req(&[], &msgs)).await,
        Err(LlmError::RateLimited(_, Some(7)))
    ));
    assert!(matches!(
        call("b").chat(&req(&[], &msgs)).await,
        Err(LlmError::Auth(_))
    ));
    let e = call("c").chat(&req(&[], &msgs)).await.unwrap_err();
    assert!(e.retryable(), "{e}");
}

#[tokio::test]
async fn openai_compatible_round_trip() {
    let seen = Arc::new(Mutex::new(Vec::<Value>::new()));
    let s = seen.clone();
    let app = Router::new().route(
        "/v1/chat/completions",
        post(move |Json(body): Json<Value>| {
            let s = s.clone();
            async move {
                s.lock().unwrap().push(body);
                Json(json!({"model": "deepseek-chat", "choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"q\"}"}}]}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5}}))
            }
        }),
    );
    let base = spawn(app).await;
    let model = OpenAiModel::new(
        reqwest::Client::new(),
        Provider::Deepseek,
        &format!("{base}/v1"),
        "k",
        "deepseek-chat",
    );
    let tools = vec![
        json!({"name": "web_search", "description": "d", "input_schema": {"type": "object"}}),
        json!({"type": "web_search_20260209", "name": "web_search"}),
    ];
    let msgs = vec![json!({"role": "user", "content": "hi"})];
    let r = model.chat(&req(&tools, &msgs)).await.unwrap();
    assert_eq!(r.stop_reason, "tool_use");
    assert_eq!(r.content[0]["input"]["query"], "q");
    let body = &seen.lock().unwrap()[0];
    assert_eq!(
        body["tools"].as_array().unwrap().len(),
        1,
        "server tools are not sent"
    );
    assert_eq!(body["messages"][0]["role"], "system");
    assert_eq!(body["max_tokens"], 2000);
}

#[tokio::test]
async fn fetches_and_cleans_real_pages() {
    let app = Router::new()
        .route("/page", get(|| async {
            ([("content-type", "text/html; charset=utf-8")],
             "<html><head><title>Port volumes</title></head><body><nav>menu</nav><h1>2025</h1><p>Shanghai handled 51.5M TEU.</p></body></html>")
        }))
        .route("/doc.pdf", get(|| async { ([("content-type", "application/pdf")], "%PDF-1.7") }))
        .route("/gone", get(|| async { StatusCode::NOT_FOUND }));
    let base = spawn(app).await;
    let dir = tempfile::tempdir().unwrap();
    let sources = Arc::new(SourceRegistry::open(dir.path().to_path_buf()));
    let tool = WebFetchTool {
        fetcher: Arc::new(Fetcher::Http(reqwest::Client::new())),
        sources: sources.clone(),
    };
    let out = tool
        .call("sub-1.1", json!({"url": format!("{base}/page")}))
        .await;
    assert!(!out.is_error, "{}", out.content);
    assert!(out.content.contains("[S1] Port volumes"));
    assert!(out.content.contains("Shanghai handled 51.5M TEU."));
    assert!(!out.content.contains("menu"));
    assert!(sources.content("S1").unwrap().contains("## 2025"));
    let pdf = tool
        .call("sub-1.1", json!({"url": format!("{base}/doc.pdf")}))
        .await;
    assert!(pdf.is_error && pdf.content.contains("PDF"));
    let gone = tool
        .call("sub-1.1", json!({"url": format!("{base}/gone")}))
        .await;
    assert!(gone.is_error && gone.content.contains("404"));
    let bad = tool
        .call("sub-1.1", json!({"url": "ftp://example.org/x"}))
        .await;
    assert!(bad.is_error);
}
