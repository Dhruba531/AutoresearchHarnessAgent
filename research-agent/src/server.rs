//! A small HTTP API and web page: start runs, watch their events live (SSE),
//! read reports, resume failed runs.
//!
//! ```text
//! GET  /                         the web page
//! GET  /api/runs                 list runs
//! POST /api/runs {"query": ...}  start a run → {"run_id": ...}
//! GET  /api/runs/{id}            metadata, phase, subagents, report
//! GET  /api/runs/{id}/events     server-sent events (replay, then live)
//! POST /api/runs/{id}/resume     continue a failed or interrupted run
//! ```
//!
//! Listens on 127.0.0.1 by default. Set `RESEARCH_AGENT_TOKEN` to require
//! `Authorization: Bearer <token>` (or `?token=`) on every API call.

use axum::extract::{Path, Query, Request, State};
use axum::http::{HeaderMap, StatusCode};
use axum::middleware::{self, Next};
use axum::response::sse::{Event as SseEvent, KeepAlive, Sse};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures::{Stream, StreamExt};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::convert::Infallible;
use std::sync::{Arc, Mutex};
use tokio::sync::broadcast;
use tokio_stream::wrappers::BroadcastStream;

use crate::research::{ResearchSystem, RunState};
use crate::store::{list_runs, valid_id, RunStore};
use crate::trace::{read_events, Event, Sink};

const INDEX: &str = include_str!("../web/index.html");

/// Forwards trace events to connected SSE clients.
pub struct BroadcastSink(pub broadcast::Sender<Event>);

impl Sink for BroadcastSink {
    fn on_event(&self, e: &Event) {
        let _ = self.0.send(e.clone());
    }
}

#[derive(Clone)]
struct App {
    sys: Arc<ResearchSystem>,
    tx: broadcast::Sender<Event>,
    token: Option<String>,
    running: Arc<Mutex<HashSet<String>>>,
}

type ApiResult = Result<Json<Value>, (StatusCode, String)>;

fn not_found(e: impl std::fmt::Display) -> (StatusCode, String) {
    (StatusCode::NOT_FOUND, e.to_string())
}

/// Build the router. `tx` must be the sender behind a `BroadcastSink` on `sys`.
pub fn router(
    sys: Arc<ResearchSystem>,
    tx: broadcast::Sender<Event>,
    token: Option<String>,
) -> Router {
    let app = App {
        sys,
        tx,
        token,
        running: Arc::new(Mutex::new(HashSet::new())),
    };
    let api = Router::new()
        .route("/api/runs", get(list).post(start))
        .route("/api/runs/{id}", get(show))
        .route("/api/runs/{id}/events", get(events))
        .route("/api/runs/{id}/resume", post(resume))
        .route_layer(middleware::from_fn_with_state(app.clone(), auth));
    Router::new()
        .route("/", get(|| async { Html(INDEX) }))
        .merge(api)
        .with_state(app)
}

async fn auth(
    State(app): State<App>,
    Query(q): Query<HashMap<String, String>>,
    headers: HeaderMap,
    req: Request,
    next: Next,
) -> Response {
    if let Some(token) = &app.token {
        let header = headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "));
        let ok = header
            .or(q.get("token").map(String::as_str))
            .is_some_and(|t| constant_eq(t.as_bytes(), token.as_bytes()));
        if !ok {
            return (StatusCode::UNAUTHORIZED, "missing or wrong token").into_response();
        }
    }
    next.run(req).await
}

fn constant_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

async fn list(State(app): State<App>) -> ApiResult {
    Ok(Json(json!(list_runs(&app.sys.config.runs_dir()))))
}

#[derive(Deserialize)]
struct StartBody {
    query: String,
}

fn spawn_run(app: &App, id: String) {
    if !app.running.lock().unwrap().insert(id.clone()) {
        return;
    }
    let app = app.clone();
    tokio::spawn(async move {
        if let Err(e) = app.sys.execute(&id).await {
            eprintln!("run {id}: {e:#}");
        }
        app.running.lock().unwrap().remove(&id);
    });
}

async fn start(State(app): State<App>, Json(body): Json<StartBody>) -> ApiResult {
    let id = app
        .sys
        .create_run(&body.query, vec!["web".into()])
        .map_err(|e| (StatusCode::BAD_REQUEST, e.to_string()))?;
    spawn_run(&app, id.clone());
    Ok(Json(json!({"run_id": id})))
}

async fn resume(State(app): State<App>, Path(id): Path<String>) -> ApiResult {
    RunStore::open(&app.sys.config.runs_dir(), &id).map_err(not_found)?;
    spawn_run(&app, id.clone());
    Ok(Json(json!({"run_id": id, "resumed": true})))
}

async fn show(State(app): State<App>, Path(id): Path<String>) -> ApiResult {
    let store = RunStore::open(&app.sys.config.runs_dir(), &id).map_err(not_found)?;
    let meta = store.meta().map_err(not_found)?;
    let state: RunState = store.read_json("state.json").unwrap_or_default();
    let subagents: Vec<Value> = state
        .subagents
        .iter()
        .map(|r| json!({"id": r.id, "title": r.task.title, "status": r.status, "tool_calls": r.tool_calls, "usd": r.usd}))
        .collect();
    Ok(Json(json!({
        "meta": meta,
        "phase": state.phase,
        "running": app.running.lock().unwrap().contains(&id),
        "spend": state.spend,
        "subagents": subagents,
        "report": store.report(),
    })))
}

async fn events(
    State(app): State<App>,
    Path(id): Path<String>,
) -> Result<Sse<impl Stream<Item = Result<SseEvent, Infallible>>>, (StatusCode, String)> {
    if !valid_id(&id) {
        return Err(not_found("invalid run id"));
    }
    let store = RunStore::open(&app.sys.config.runs_dir(), &id).map_err(not_found)?;
    let rx = app.tx.subscribe(); // subscribe before replaying, so nothing falls in between
    let replay = read_events(&store.path("trace.jsonl"));
    let to_sse = |e: Event| {
        SseEvent::default()
            .event(e.kind.clone())
            .data(serde_json::to_string(&e).unwrap_or_default())
    };
    let past = futures::stream::iter(replay.into_iter().map(move |e| Ok(to_sse(e))));
    let live = BroadcastStream::new(rx).filter_map(move |r| {
        let id = id.clone();
        async move {
            match r {
                Ok(e) if e.run_id == id => Some(Ok(to_sse(e))),
                _ => None,
            }
        }
    });
    Ok(Sse::new(past.chain(live)).keep_alive(KeepAlive::default()))
}

pub async fn serve(sys: ResearchSystem, addr: &str) -> anyhow::Result<()> {
    let token = std::env::var("RESEARCH_AGENT_TOKEN")
        .ok()
        .filter(|t| !t.is_empty());
    let (tx, _) = broadcast::channel(1024);
    let mut sys = sys;
    sys.add_sink(Arc::new(BroadcastSink(tx.clone())));
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let local = listener.local_addr()?;
    if !local.ip().is_loopback() && token.is_none() {
        eprintln!("warning: listening on {local} without RESEARCH_AGENT_TOKEN; anyone who can reach it can spend your API budget");
    }
    eprintln!("research-agent listening on http://{local}");
    axum::serve(listener, router(Arc::new(sys), tx, token)).await?;
    Ok(())
}
