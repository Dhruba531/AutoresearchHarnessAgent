//! The research system: an orchestrator-worker architecture.
//!
//! ```text
//! user query ─▶ LeadResearcher ──plan──▶ memory
//!                 │  run_subagents (parallel, one round at a time)
//!                 ├─▶ subagent ─ web_search / web_fetch / corpus ─▶ findings ─▶ subagents/<id>.md
//!                 ├─▶ subagent ─ ...
//!                 │  synthesise · more research needed? ─▶ another round
//!                 └─ complete_task(report)
//!                       └─▶ CitationAgent ─▶ report.md with [n] markers and sources
//! ```
//!
//! Durable execution: the run is checkpointed after every lead turn and after
//! every subagent, so a crash, a Ctrl-C or a provider outage costs at most the
//! work in flight. `resume` continues from the checkpoint; subagents that already
//! finished are not run again.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Semaphore;

use crate::agent::{self, AgentEnd, AgentSpec, AgentState, Env, Hooks};
use crate::budget::{Budget, Spend};
use crate::citations;
use crate::config::{Config, Provider};
use crate::llm::anthropic::{harvest_sources, AnthropicModel};
use crate::llm::{build_model, Model};
use crate::prompts::{render, Prompts};
use crate::store::{RunMeta, RunStore};
use crate::tools::corpus::{Corpus, CorpusReadTool, CorpusSearchTool};
use crate::tools::fetch::{Fetcher, WebFetchTool};
use crate::tools::memory::{MemoryReadTool, MemorySaveTool, MemoryStore};
use crate::tools::search::{SearchBackend, WebSearchTool};
use crate::tools::sources::SourceRegistry;
use crate::tools::{int_arg, str_arg, FinishTool, Tool, ToolOutput};
use crate::trace::{Sink, Tracer};
use crate::util::{clip, fnv1a, new_id, now_iso, today};

const SUBAGENT_TIMEOUT: Duration = Duration::from_secs(20 * 60);
const SUMMARY_CHARS: usize = 4000;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Phase {
    #[default]
    Lead,
    Citations,
    Done,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct SubTask {
    pub title: String,
    pub objective: String,
    pub output_format: String,
    pub guidance: String,
    pub boundaries: String,
    pub tool_budget: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SubagentRecord {
    pub id: String,
    pub key: String,
    pub round: u32,
    pub task: SubTask,
    /// done | partial | failed
    pub status: String,
    pub findings: String,
    pub sources: Vec<String>,
    pub confidence: String,
    pub gaps: String,
    pub tool_calls: u32,
    pub usd: f64,
    pub end: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct RunState {
    pub phase: Phase,
    pub lead: AgentState,
    pub rounds: u32,
    pub handoffs: u32,
    pub subagents: Vec<SubagentRecord>,
    pub report: Option<String>,
    pub lead_end: Option<String>,
    pub spend: Spend,
}

#[derive(Clone, Debug, Serialize)]
pub struct RunOutcome {
    pub run_id: String,
    pub status: String,
    pub report: String,
    pub spend: Spend,
    pub sources_cited: usize,
    pub subagents: usize,
    pub dir: PathBuf,
}

/// Everything one run shares across its agents.
pub struct RunCtx {
    pub run_id: String,
    pub query: String,
    pub config: Config,
    pub store: RunStore,
    pub sources: Arc<SourceRegistry>,
    pub memory: Arc<MemoryStore>,
    pub tracer: Arc<Tracer>,
    pub budget: Arc<Budget>,
    pub prompts: Prompts,
    pub sub_model: Arc<dyn Model>,
    pub search: Arc<SearchBackend>,
    pub corpus: Option<Arc<Corpus>>,
    pub state: Mutex<RunState>,
    pub sub_slots: Semaphore,
    pub search_tool: Option<Arc<dyn Tool>>,
    pub fetch_tool: Option<Arc<dyn Tool>>,
    pub corpus_tools: Vec<Arc<dyn Tool>>,
}

impl RunCtx {
    pub fn env(&self) -> Env {
        Env {
            budget: self.budget.clone(),
            tracer: self.tracer.clone(),
            max_retries: self.config.max_retries,
        }
    }

    /// Persist the run state (atomically). Called after every lead turn and subagent.
    pub fn checkpoint(&self) {
        let mut st = self.state.lock().unwrap();
        st.spend = self.budget.snapshot();
        let _ = self.store.write_json("state.json", &*st);
        drop(st);
        let _ = self.sources.save();
    }

    fn date_vars(&self) -> Vec<(&'static str, String)> {
        let l = &self.config.limits;
        vec![
            ("date", today()),
            ("max_per_round", l.max_subagents_per_round.to_string()),
            ("max_total", l.max_subagents_total.to_string()),
        ]
    }

    /// Search/fetch tools for an agent on `model`: client tools, or Claude's
    /// server-side web tools when the run searches server-side.
    fn web_tools(&self, model: &dyn Model, max_uses: u32) -> (Vec<Arc<dyn Tool>>, Vec<Value>) {
        if matches!(*self.search, SearchBackend::Anthropic) {
            if model.supports_server_search() {
                let (s, f) = AnthropicModel::web_tool_types(model.id());
                return (
                    vec![],
                    vec![
                        json!({"type": s, "name": "web_search", "max_uses": max_uses}),
                        json!({"type": f, "name": "web_fetch", "max_uses": max_uses, "citations": {"enabled": true}, "max_content_tokens": 16000}),
                    ],
                );
            }
            return (vec![], vec![]);
        }
        let mut tools = Vec::new();
        if let Some(t) = &self.search_tool {
            tools.push(t.clone());
        }
        if let Some(t) = &self.fetch_tool {
            tools.push(t.clone());
        }
        (tools, vec![])
    }
}

pub struct ResearchSystem {
    pub config: Config,
    pub lead_model: Arc<dyn Model>,
    pub sub_model: Arc<dyn Model>,
    pub citation_model: Arc<dyn Model>,
    pub search: Arc<SearchBackend>,
    pub fetcher: Arc<Fetcher>,
    pub corpus: Option<Arc<Corpus>>,
    pub http: reqwest::Client,
    pub prompts: Prompts,
    sinks: Vec<Arc<dyn Sink>>,
}

pub fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(30))
        .pool_idle_timeout(Duration::from_secs(90))
        .build()
        .expect("HTTP client")
}

impl ResearchSystem {
    /// Build models, search and corpus from configuration.
    pub fn new(config: Config) -> anyhow::Result<ResearchSystem> {
        let http = http_client();
        let lead = build_model(&config, &config.lead_model, &http)?;
        let sub = build_model(&config, &config.subagent_model, &http)?;
        let cite = build_model(&config, &config.citation_model, &http)?;
        let search = SearchBackend::from_config(
            &config,
            config.subagent_model.provider == Provider::Anthropic,
        )?;
        let corpus = match &config.corpus_dir {
            Some(d) => Some(Arc::new(Corpus::load(d)?)),
            None => None,
        };
        let fetcher = Arc::new(Fetcher::Http(http.clone()));
        Ok(ResearchSystem::with_parts(
            config, lead, sub, cite, search, fetcher, corpus, http,
        ))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn with_parts(
        config: Config,
        lead: Arc<dyn Model>,
        sub: Arc<dyn Model>,
        cite: Arc<dyn Model>,
        search: SearchBackend,
        fetcher: Arc<Fetcher>,
        corpus: Option<Arc<Corpus>>,
        http: reqwest::Client,
    ) -> ResearchSystem {
        let prompts = Prompts::load(config.prompts_dir.as_deref());
        ResearchSystem {
            config,
            lead_model: lead,
            sub_model: sub,
            citation_model: cite,
            search: Arc::new(search),
            fetcher,
            corpus,
            http,
            prompts,
            sinks: Vec::new(),
        }
    }

    pub fn add_sink(&mut self, sink: Arc<dyn Sink>) {
        self.sinks.push(sink);
    }

    /// Register a new run and return its id; `execute` runs it.
    pub fn create_run(&self, query: &str, tags: Vec<String>) -> anyhow::Result<String> {
        let query = query.trim();
        anyhow::ensure!(!query.is_empty(), "the research question is empty");
        let id = new_id();
        let store = RunStore::create(&self.config.runs_dir(), &id)?;
        let mut meta = RunMeta {
            id: id.clone(),
            query: query.to_string(),
            created_at: now_iso(),
            status: "queued".into(),
            lead_model: format!("{}", self.config.lead_model),
            subagent_model: format!("{}", self.config.subagent_model),
            search_backend: self.search.label().into(),
            tags,
            ..RunMeta::default()
        };
        store.save_meta(&mut meta)?;
        store.write_json("state.json", &RunState::default())?;
        Ok(id)
    }

    /// Research a question from scratch.
    pub async fn research(&self, query: &str) -> anyhow::Result<RunOutcome> {
        let id = self.create_run(query, vec![])?;
        self.execute(&id).await
    }

    /// Run (or resume) a run from its last checkpoint.
    pub async fn execute(&self, run_id: &str) -> anyhow::Result<RunOutcome> {
        let store = RunStore::open(&self.config.runs_dir(), run_id)?;
        let mut meta = store.meta()?;
        let state: RunState = store.read_json("state.json").unwrap_or_default();
        let resumed = state.lead.turns > 0 || state.phase != Phase::Lead;
        if state.phase == Phase::Done {
            return Ok(self.outcome(&store, &meta, &state));
        }
        let ctx = Arc::new(self.context(store.clone(), &meta.query, state));
        meta.status = "running".into();
        meta.error = None;
        store.save_meta(&mut meta)?;
        if resumed {
            let phase = format!("{:?}", ctx.state.lock().unwrap().phase).to_lowercase();
            ctx.tracer
                .emit("system", "run_resumed", json!({"phase": phase}));
        } else {
            ctx.tracer.emit("system", "run_started", json!({
                "query": ctx.tracer.text(&meta.query, 2000), "lead_model": meta.lead_model, "subagent_model": meta.subagent_model,
                "search": meta.search_backend, "corpus_docs": self.corpus.as_ref().map(|c| c.docs.len()),
            }));
        }

        let result = self.drive(&ctx).await;
        let state = ctx.state.lock().unwrap().clone();
        let spend = ctx.budget.snapshot();
        meta.cost_usd = spend.usd;
        meta.subagents = state.subagents.len() as u64;
        match result {
            Ok(()) => {
                meta.status = "done".into();
                meta.sources_cited = store
                    .read_json::<citations::CitedReport>("citations.json")
                    .map(|c| c.cited.len())
                    .unwrap_or(0);
                store.save_meta(&mut meta)?;
                ctx.tracer.emit(
                    "system",
                    "run_finished",
                    json!({"status": "done", "summary": ctx.budget.summary()}),
                );
                Ok(self.outcome(&store, &meta, &state))
            }
            Err(e) => {
                meta.status = "failed".into();
                meta.error = Some(e.to_string());
                store.save_meta(&mut meta)?;
                ctx.tracer.emit(
                    "system",
                    "run_finished",
                    json!({"status": "failed", "summary": e.to_string()}),
                );
                Err(e.context(format!(
                    "run {run_id} stopped; continue it with `research-agent resume {run_id}`"
                )))
            }
        }
    }

    fn outcome(&self, store: &RunStore, meta: &RunMeta, state: &RunState) -> RunOutcome {
        RunOutcome {
            run_id: meta.id.clone(),
            status: meta.status.clone(),
            report: store.report().unwrap_or_default(),
            spend: state.spend.clone(),
            sources_cited: meta.sources_cited,
            subagents: state.subagents.len(),
            dir: store.dir.clone(),
        }
    }

    fn context(&self, store: RunStore, query: &str, state: RunState) -> RunCtx {
        let tracer = Arc::new(Tracer::open(
            &store.path("trace.jsonl"),
            &store.dir.file_name().unwrap_or_default().to_string_lossy(),
            self.config.trace_content,
            self.sinks.clone(),
        ));
        let sources = Arc::new(SourceRegistry::open(store.dir.clone()));
        let memory = Arc::new(MemoryStore::open(store.path("memory.json")));
        let budget = Arc::new(Budget::resume(
            self.config.limits.clone(),
            state.spend.clone(),
        ));
        let search_tool: Option<Arc<dyn Tool>> = self.search.is_client_side().then(|| {
            Arc::new(WebSearchTool::new(
                self.search.clone(),
                self.http.clone(),
                sources.clone(),
            )) as Arc<dyn Tool>
        });
        let fetch_tool: Option<Arc<dyn Tool>> = self.search.is_client_side().then(|| {
            Arc::new(WebFetchTool {
                fetcher: self.fetcher.clone(),
                sources: sources.clone(),
            }) as Arc<dyn Tool>
        });
        let corpus_tools: Vec<Arc<dyn Tool>> = match &self.corpus {
            Some(c) => vec![
                Arc::new(CorpusSearchTool {
                    corpus: c.clone(),
                    sources: sources.clone(),
                }),
                Arc::new(CorpusReadTool {
                    corpus: c.clone(),
                    sources: sources.clone(),
                }),
            ],
            None => vec![],
        };
        RunCtx {
            run_id: tracer.run_id.clone(),
            query: query.to_string(),
            config: self.config.clone(),
            store,
            sources,
            memory,
            tracer,
            budget,
            prompts: self.prompts.clone(),
            sub_model: self.sub_model.clone(),
            search: self.search.clone(),
            corpus: self.corpus.clone(),
            state: Mutex::new(state),
            sub_slots: Semaphore::new(self.config.limits.parallel_subagents.max(1)),
            search_tool,
            fetch_tool,
            corpus_tools,
        }
    }

    async fn drive(&self, ctx: &Arc<RunCtx>) -> anyhow::Result<()> {
        if ctx.state.lock().unwrap().phase == Phase::Lead {
            let report = self.lead_phase(ctx).await?;
            ctx.store.write_text("report.raw.md", &report)?;
            ctx.tracer
                .emit("lead", "report_written", json!({"chars": report.len()}));
            {
                let mut st = ctx.state.lock().unwrap();
                st.report = Some(report);
                st.phase = Phase::Citations;
            }
            ctx.checkpoint();
        }
        if ctx.state.lock().unwrap().phase == Phase::Citations {
            let report = ctx.state.lock().unwrap().report.clone().unwrap_or_default();
            let candidates = self.citation_candidates(ctx, &report);
            let cited = citations::cite(
                self.citation_model.clone(),
                &ctx.env(),
                &ctx.prompts.citations,
                &report,
                &ctx.sources,
                &candidates,
            )
            .await;
            ctx.tracer
                .emit("citations", "citations", citations::citation_stats(&cited));
            ctx.store.write_json("citations.json", &cited)?;
            ctx.store.write_text("report.md", &cited.markdown)?;
            ctx.state.lock().unwrap().phase = Phase::Done;
            ctx.checkpoint();
        }
        Ok(())
    }

    /// Sources the subagents relied on first, then any source whose URL the
    /// report mentions, then the rest by how many agents saw them.
    fn citation_candidates(&self, ctx: &RunCtx, report: &str) -> Vec<String> {
        let st = ctx.state.lock().unwrap();
        let mut ids: Vec<String> = Vec::new();
        let mut push = |id: String| {
            if !ids.contains(&id) {
                ids.push(id);
            }
        };
        for r in &st.subagents {
            r.sources.iter().cloned().for_each(&mut push);
        }
        let mut all = ctx.sources.all();
        for s in &all {
            if report.contains(&s.url) {
                push(s.id.clone());
            }
        }
        all.sort_by(|a, b| {
            (b.content_chars > 0)
                .cmp(&(a.content_chars > 0))
                .then(b.seen_by.len().cmp(&a.seen_by.len()))
        });
        for s in all {
            push(s.id);
        }
        ids
    }

    async fn lead_phase(&self, ctx: &Arc<RunCtx>) -> anyhow::Result<String> {
        let l = &ctx.config.limits;
        let (mut tools, server_tools) = ctx.web_tools(self.lead_model.as_ref(), 10);
        tools.extend(ctx.corpus_tools.iter().cloned());
        tools.push(Arc::new(RunSubagentsTool { ctx: ctx.clone() }));
        tools.push(Arc::new(ReadArtifactTool { ctx: ctx.clone() }));
        tools.push(Arc::new(MemorySaveTool(ctx.memory.clone())));
        tools.push(Arc::new(MemoryReadTool(ctx.memory.clone())));
        tools.push(Arc::new(FinishTool {
            name: "complete_task".into(),
            description: "Finish the research by submitting the final report (Markdown). Call this exactly once, when the research is complete.".into(),
            schema: json!({"type": "object", "additionalProperties": false, "required": ["report"],
                           "properties": {"report": {"type": "string", "description": "The full final report in Markdown, without citation markers."}}}),
        }));
        let spec = AgentSpec {
            name: "lead".into(),
            model: self.lead_model.clone(),
            system: render(&ctx.prompts.lead, &ctx.date_vars()),
            tools,
            server_tools,
            finish_tool: "complete_task".into(),
            max_turns: l.lead_max_turns,
            max_tool_calls: l.lead_max_tool_calls,
            max_tokens: 32_000,
            effort: Some(ctx.config.lead_effort.clone()),
            thinking_display: Some(ctx.config.thinking_display.clone()),
        };
        let state = {
            let st = ctx.state.lock().unwrap();
            if st.lead.messages.is_empty() {
                AgentState::new(&self.lead_brief(ctx))
            } else {
                st.lead.clone()
            }
        };
        let hooks = LeadHooks {
            ctx: ctx.clone(),
            brief: self.lead_brief(ctx),
        };
        let (end, state) = agent::run(&spec, &ctx.env(), state, &hooks).await;
        ctx.state.lock().unwrap().lead = state.clone();
        ctx.checkpoint();
        let end_label = match &end {
            AgentEnd::Finished(_) => "finished",
            AgentEnd::Text(_) => "text",
            AgentEnd::OutOfTurns => "out_of_turns",
            AgentEnd::Budget(_) => "budget",
            AgentEnd::Error(_) => "error",
        };
        ctx.state.lock().unwrap().lead_end = Some(end_label.into());
        let report = match end {
            AgentEnd::Finished(v) => str_arg(&v, "report"),
            AgentEnd::Text(t) if t.trim().len() > 200 => t,
            AgentEnd::Error(e) => anyhow::bail!("the lead agent failed: {e}"),
            other => {
                let reason = match other {
                    AgentEnd::OutOfTurns => "it ran out of turns".to_string(),
                    AgentEnd::Budget(r) => r,
                    _ => "it stopped without writing a report".to_string(),
                };
                ctx.tracer.emit("lead", "budget_warning", json!({"message": format!("lead stopped early: {reason}; compiling the subagents' findings")}));
                self.fallback_report(ctx, &reason, &state)
            }
        };
        if report.trim().is_empty() {
            anyhow::bail!("the lead agent submitted an empty report");
        }
        Ok(report)
    }

    fn lead_brief(&self, ctx: &RunCtx) -> String {
        let l = &ctx.config.limits;
        let corpus = match &ctx.corpus {
            Some(c) => format!(
                "{} local documents (corpus_search / corpus_read)",
                c.docs.len()
            ),
            None => "none".into(),
        };
        format!(
            "Research question:\n{}\n\nToday's date: {}.\nWeb search: {}. Local corpus: {}.\nLimits for this run: at most {} subagents per \
             run_subagents call and {} in total; budget ${:.2}; {} minutes.",
            ctx.query,
            today(),
            ctx.search.label(),
            corpus,
            l.max_subagents_per_round,
            l.max_subagents_total,
            l.max_usd,
            l.max_minutes
        )
    }

    /// When the lead cannot finish, the subagents' findings are still worth having.
    fn fallback_report(&self, ctx: &RunCtx, reason: &str, lead: &AgentState) -> String {
        let st = ctx.state.lock().unwrap();
        let mut out = format!("# {}\n\n> This report is incomplete: the lead researcher stopped before writing its synthesis ({reason}). \
                               Below are the findings its subagents returned.\n", ctx.query);
        let draft = lead.last_text();
        if !draft.trim().is_empty() {
            out.push_str(&format!(
                "\n## Lead researcher's last notes\n\n{}\n",
                draft.trim()
            ));
        }
        for r in &st.subagents {
            out.push_str(&format!("\n## {}\n\n{}\n", r.task.title, r.findings.trim()));
        }
        out
    }
}

// ---- the lead's hooks: checkpoints, reminders, context handoff ------------------------

struct LeadHooks {
    ctx: Arc<RunCtx>,
    brief: String,
}

#[async_trait]
impl Hooks for LeadHooks {
    async fn after_turn(&self, state: &mut AgentState) {
        let limit = self.ctx.config.limits.lead_context_tokens;
        let at_user_turn = state.messages.last().is_some_and(|m| m["role"] == "user");
        if limit > 0 && state.last_prompt_tokens > limit && at_user_turn {
            let tokens = state.last_prompt_tokens;
            state.messages = vec![json!({"role": "user", "content": self.handoff_message()})];
            state.last_prompt_tokens = 0;
            self.ctx.state.lock().unwrap().handoffs += 1;
            self.ctx
                .tracer
                .emit("lead", "handoff", json!({"prompt_tokens": tokens}));
        }
        self.ctx.state.lock().unwrap().lead = state.clone();
        self.ctx.checkpoint();
    }

    async fn after_reply(&self, state: &AgentState) {
        self.ctx.state.lock().unwrap().lead = state.clone();
        self.ctx.checkpoint();
    }

    fn reminder(&self, state: &AgentState, spec: &AgentSpec) -> Option<String> {
        let used = self.ctx.budget.fraction_used();
        let turns_left = spec.max_turns.saturating_sub(state.turns);
        if used >= 0.8 {
            let msg = format!(
                "[budget] {:.0}% of this run's budget is used. Do not start new subagents unless essential; write the report and call complete_task.",
                used * 100.0
            );
            self.ctx
                .tracer
                .emit("lead", "budget_warning", json!({"message": msg}));
            return Some(msg);
        }
        if turns_left <= 3 {
            return Some(format!(
                "[budget] {turns_left} turns left. Finish with complete_task soon."
            ));
        }
        None
    }
}

impl LeadHooks {
    /// A fresh starting message that carries the run's state forward: the
    /// original brief, the lead's memory, and every subagent's result.
    fn handoff_message(&self) -> String {
        let mut out = format!(
            "{}\n\nYou are continuing this research run in a fresh context: the previous one grew too large. Nothing is lost. \
             Your memory and every subagent result so far are below; read full outputs with read_artifact.\n",
            self.brief
        );
        let mem = self.ctx.memory.all();
        out.push_str("\n## Your memory\n");
        if mem.is_empty() {
            out.push_str("(empty)\n");
        }
        for (k, e) in mem {
            out.push_str(&format!("\n### {k}\n{}\n", e.content));
        }
        let st = self.ctx.state.lock().unwrap();
        out.push_str(&format!(
            "\n## Subagent results so far ({})\n",
            st.subagents.len()
        ));
        for r in &st.subagents {
            out.push_str(&format!(
                "\n### {} · {} — {}\n{}\nSources: {}\n",
                r.id,
                r.task.title,
                r.status,
                clip(&r.findings, 1500),
                r.sources.join(", ")
            ));
        }
        out.push_str(&format!("\n{} sources registered so far.\n\nContinue: decide whether more research is needed, then write the final report and call complete_task.", self.ctx.sources.len()));
        out
    }
}

// ---- subagents ---------------------------------------------------------------------

struct SubHooks {
    ctx: Arc<RunCtx>,
    agent: String,
}

#[async_trait]
impl Hooks for SubHooks {
    fn on_response(&self, content: &[Value]) {
        for (url, title, text) in harvest_sources(content) {
            let id = self.ctx.sources.upsert(
                &url,
                &title,
                &clip(&text, 400),
                "server_search",
                &self.agent,
            );
            if text.chars().count() > 600
                && self
                    .ctx
                    .sources
                    .get(&id)
                    .is_some_and(|s| s.content_chars < text.len())
            {
                self.ctx.sources.attach_content(&id, &text);
            }
        }
    }

    fn reminder(&self, state: &AgentState, spec: &AgentSpec) -> Option<String> {
        let left = spec.max_tool_calls.saturating_sub(state.tool_calls);
        if self.ctx.budget.fraction_used() >= 0.9 {
            return Some("[budget] The run's budget is nearly spent. Call complete_task now with what you have.".into());
        }
        if left == 0 {
            Some("[budget] Tool budget used up. Call complete_task now.".into())
        } else if state.tool_calls * 2 >= spec.max_tool_calls {
            Some(format!(
                "[budget] {} of {} tool calls used.",
                state.tool_calls, spec.max_tool_calls
            ))
        } else {
            None
        }
    }
}

fn task_key(t: &SubTask) -> String {
    let norm = |s: &str| {
        s.split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase()
    };
    format!(
        "{:016x}",
        fnv1a(format!("{}\u{1}{}", norm(&t.objective), norm(&t.boundaries)).as_bytes())
    )
}

fn subagent_finish_tool() -> FinishTool {
    FinishTool {
        name: "complete_task".into(),
        description: "Report your findings to the lead researcher and finish. Call this exactly once, when your task is done or your budget is spent.".into(),
        schema: json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["findings", "sources"],
            "properties": {
                "findings": {"type": "string", "description": "Your findings, dense and specific, in the requested output format."},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Source ids (S4) or URLs the findings rely on."},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "gaps": {"type": "string", "description": "What you could not establish."}
            }
        }),
    }
}

async fn run_subagent(ctx: Arc<RunCtx>, id: String, task: SubTask, round: u32) -> SubagentRecord {
    let l = &ctx.config.limits;
    let budget = task.tool_budget.clamp(2, l.subagent_max_tool_calls);
    let (mut tools, server_tools) = ctx.web_tools(ctx.sub_model.as_ref(), budget);
    tools.extend(ctx.corpus_tools.iter().cloned());
    tools.push(Arc::new(subagent_finish_tool()));
    let spec = AgentSpec {
        name: id.clone(),
        model: ctx.sub_model.clone(),
        system: render(&ctx.prompts.subagent, &ctx.date_vars()),
        tools,
        server_tools,
        finish_tool: "complete_task".into(),
        max_turns: l.subagent_max_turns,
        max_tool_calls: budget,
        max_tokens: 16_000,
        effort: Some(ctx.config.subagent_effort.clone()),
        thinking_display: Some(ctx.config.thinking_display.clone()),
    };
    let first = format!(
        "<task>\nTitle: {}\nObjective: {}\nExpected output: {}\nWhere to look / tools: {}\nBoundaries (do not cover): {}\nTool-call budget: {}\n</task>\n\n\
         Overall research question, for context only (other subagents cover other parts):\n{}\n\nToday's date: {}.",
        task.title,
        task.objective,
        or_default(&task.output_format, "concise findings with specific facts, figures and dates"),
        or_default(&task.guidance, "use your judgment"),
        or_default(&task.boundaries, "none given"),
        budget,
        ctx.query,
        today()
    );
    ctx.tracer.emit(&id, "subagent_started", json!({
        "title": task.title, "round": round, "tool_budget": budget, "objective": ctx.tracer.text(&task.objective, 600),
    }));
    let hooks = SubHooks {
        ctx: ctx.clone(),
        agent: id.clone(),
    };
    let env = ctx.env();
    let run = agent::run(&spec, &env, AgentState::new(&first), &hooks);
    let (end, state) = match tokio::time::timeout(SUBAGENT_TIMEOUT, run).await {
        Ok(r) => r,
        Err(_) => (
            AgentEnd::Error(format!(
                "timed out after {} minutes",
                SUBAGENT_TIMEOUT.as_secs() / 60
            )),
            AgentState::default(),
        ),
    };
    let seen: Vec<String> = ctx
        .sources
        .all()
        .into_iter()
        .filter(|s| s.seen_by.contains(&id))
        .map(|s| s.id)
        .take(12)
        .collect();
    let (status, findings, sources, confidence, gaps, end_label) = match end {
        AgentEnd::Finished(v) => {
            let refs: Vec<String> = v["sources"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter_map(|r| ctx.sources.resolve(r, &id))
                .collect();
            (
                "done",
                str_arg(&v, "findings"),
                refs,
                str_arg(&v, "confidence"),
                str_arg(&v, "gaps"),
                "finished".to_string(),
            )
        }
        AgentEnd::Text(t) => (
            "partial",
            t,
            seen,
            "low".into(),
            "Ended without a structured report.".into(),
            "text".into(),
        ),
        AgentEnd::OutOfTurns => (
            "partial",
            state.last_text(),
            seen,
            "low".into(),
            "Ran out of turns.".into(),
            "out_of_turns".into(),
        ),
        AgentEnd::Budget(r) => (
            "partial",
            state.last_text(),
            seen,
            "low".into(),
            format!("Stopped: {r}."),
            "budget".into(),
        ),
        AgentEnd::Error(e) => (
            "failed",
            format!("(subagent failed: {e})"),
            seen,
            "low".into(),
            e.clone(),
            "error".into(),
        ),
    };
    let record = SubagentRecord {
        id: id.clone(),
        key: task_key(&task),
        round,
        task,
        status: status.into(),
        findings: if findings.trim().is_empty() {
            "(no findings)".into()
        } else {
            findings
        },
        sources,
        confidence,
        gaps,
        tool_calls: state.tool_calls,
        usd: state.usd,
        end: end_label,
    };
    write_artifact(&ctx, &record, &state);
    ctx.tracer.emit(&id, "subagent_done", json!({
        "status": record.status, "tool_calls": record.tool_calls, "usd": record.usd, "sources": record.sources.len(), "end": record.end,
    }));
    record
}

fn or_default<'a>(s: &'a str, d: &'a str) -> &'a str {
    if s.trim().is_empty() {
        d
    } else {
        s
    }
}

/// Subagents write their full output to the filesystem; the lead gets a summary
/// and a reference, which avoids a game of telephone through the coordinator.
fn write_artifact(ctx: &RunCtx, r: &SubagentRecord, state: &AgentState) {
    let mut md = format!(
        "# {} · {}\n\nStatus: {} · {} tool calls · ${:.4} · confidence: {}\n\n## Task\n\n- Objective: {}\n- Output format: {}\n- Guidance: {}\n- Boundaries: {}\n\n## Findings\n\n{}\n",
        r.id, r.task.title, r.status, r.tool_calls, r.usd, or_default(&r.confidence, "n/a"),
        r.task.objective, r.task.output_format, r.task.guidance, r.task.boundaries, r.findings.trim()
    );
    if !r.gaps.trim().is_empty() {
        md.push_str(&format!("\n## Gaps\n\n{}\n", r.gaps.trim()));
    }
    md.push_str("\n## Sources\n\n");
    for id in &r.sources {
        if let Some(s) = ctx.sources.get(id) {
            md.push_str(&format!("- [{}] {} — {}\n", s.id, s.title, s.url));
        }
    }
    let _ = ctx.store.write_text(&format!("subagents/{}.md", r.id), &md);
    let _ = ctx.store.write_json(
        &format!("subagents/{}.messages.json", r.id),
        &state.messages,
    );
}

fn format_for_lead(r: &SubagentRecord, cached: bool) -> String {
    let mut out = format!(
        "### {} · {} — {}{}, {} tool calls, ${:.3}\n{}\n",
        r.id,
        r.task.title,
        r.status,
        if cached {
            " (from an earlier session)"
        } else {
            ""
        },
        r.tool_calls,
        r.usd,
        clip(r.findings.trim(), SUMMARY_CHARS)
    );
    if !r.sources.is_empty() {
        out.push_str(&format!("Sources: {}\n", r.sources.join(", ")));
    }
    if !r.confidence.is_empty() {
        out.push_str(&format!("Confidence: {}. ", r.confidence));
    }
    if !r.gaps.trim().is_empty() {
        out.push_str(&format!("Gaps: {}", r.gaps.trim()));
    }
    if r.findings.chars().count() > SUMMARY_CHARS {
        out.push_str(&format!(
            "\n(Findings truncated; read_artifact(\"{}\") has the full text.)",
            r.id
        ));
    }
    out
}

/// `run_subagents`: the lead's delegation tool.
pub struct RunSubagentsTool {
    ctx: Arc<RunCtx>,
}

#[async_trait]
impl Tool for RunSubagentsTool {
    fn name(&self) -> &str {
        "run_subagents"
    }

    fn description(&self) -> String {
        let l = &self.ctx.config.limits;
        format!(
            "Start research subagents that work in parallel, each with its own context and tools (web search and fetch, \
             and the local corpus if there is one), and wait for their findings. Each subagent knows only what you put \
             in its task, so give every task a specific objective, an output format, guidance on where to look, \
             boundaries against the other tasks, and a tool budget matched to its difficulty (3-10 for a simple \
             lookup, 10-20 for a hard one). Put all tasks of a round in one call. At most {} tasks per call, {} \
             subagents per run.",
            l.max_subagents_per_round, l.max_subagents_total
        )
    }

    fn input_schema(&self) -> Value {
        let l = &self.ctx.config.limits;
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["tasks"],
            "properties": {"tasks": {"type": "array", "minItems": 1, "maxItems": l.max_subagents_per_round.max(1) * 2, "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["title", "objective", "output_format", "tool_budget"],
                "properties": {
                    "title": {"type": "string", "description": "A few words naming the task."},
                    "objective": {"type": "string", "description": "The specific question this subagent must answer, and why it matters."},
                    "output_format": {"type": "string", "description": "Exactly what to return."},
                    "guidance": {"type": "string", "description": "Where to look and which tools or sources to prefer."},
                    "boundaries": {"type": "string", "description": "What not to cover (other subagents have it)."},
                    "tool_budget": {"type": "integer", "minimum": 2, "maximum": l.subagent_max_tool_calls}
                }}}}
        })
    }

    async fn call(&self, _agent: &str, input: Value) -> ToolOutput {
        let ctx = &self.ctx;
        let l = &ctx.config.limits;
        if ctx.budget.fraction_used() >= 0.9 {
            return ToolOutput::err("The run's budget is nearly spent, so no new subagents can start. Write the report with what you have.");
        }
        let mut tasks: Vec<SubTask> = input["tasks"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|t| SubTask {
                title: str_arg(t, "title"),
                objective: str_arg(t, "objective"),
                output_format: str_arg(t, "output_format"),
                guidance: str_arg(t, "guidance"),
                boundaries: str_arg(t, "boundaries"),
                tool_budget: int_arg(t, "tool_budget", 10, 2, l.subagent_max_tool_calls as i64)
                    as u32,
            })
            .filter(|t| !t.objective.is_empty())
            .collect();
        let mut notes = Vec::new();
        if tasks.len() > l.max_subagents_per_round {
            notes.push(format!("Only the first {} tasks ran (the per-call limit); send the rest in a later call if still needed.", l.max_subagents_per_round));
            tasks.truncate(l.max_subagents_per_round);
        }
        let round = {
            let mut st = ctx.state.lock().unwrap();
            st.rounds += 1;
            st.rounds
        };
        // Reuse finished results for identical tasks (after a resume, or a repeated request).
        let mut cached: Vec<(usize, SubagentRecord)> = Vec::new();
        let mut fresh: Vec<(usize, SubTask)> = Vec::new();
        {
            let st = ctx.state.lock().unwrap();
            for (i, t) in tasks.into_iter().enumerate() {
                let key = task_key(&t);
                match st
                    .subagents
                    .iter()
                    .rev()
                    .find(|r| r.key == key && r.status != "failed")
                {
                    Some(r) => cached.push((i, r.clone())),
                    None => fresh.push((i, t)),
                }
            }
        }
        let granted = ctx.budget.reserve_subagents(fresh.len());
        if granted < fresh.len() {
            notes.push(format!(
                "{} of {} new tasks did not run: the run's limit of {} subagents is reached.",
                fresh.len() - granted,
                fresh.len(),
                l.max_subagents_total
            ));
            fresh.truncate(granted);
        }
        let jobs = fresh.into_iter().map(|(i, task)| {
            let ctx = ctx.clone();
            let id = format!("sub-{round}.{}", i + 1);
            async move {
                let _slot = ctx.sub_slots.acquire().await;
                let rec = run_subagent(ctx.clone(), id, task, round).await;
                {
                    let mut st = ctx.state.lock().unwrap();
                    st.subagents.push(rec.clone());
                }
                ctx.checkpoint();
                (i, rec)
            }
        });
        let done: Vec<(usize, SubagentRecord)> = futures::future::join_all(jobs).await;
        let mut all: Vec<(usize, String)> = done
            .iter()
            .map(|(i, r)| (*i, format_for_lead(r, false)))
            .collect();
        all.extend(cached.iter().map(|(i, r)| (*i, format_for_lead(r, true))));
        all.sort_by_key(|(i, _)| *i);
        if all.is_empty() {
            return ToolOutput::err(notes.join(" "));
        }
        let mut out = format!("Round {round}: {} subagent result(s).\n\n", all.len());
        out.push_str(
            &all.into_iter()
                .map(|(_, s)| s)
                .collect::<Vec<_>>()
                .join("\n\n"),
        );
        if !notes.is_empty() {
            out.push_str(&format!("\n\nNote: {}", notes.join(" ")));
        }
        out.push_str(&format!("\n\n[{}]", ctx.budget.summary()));
        ToolOutput::ok(out)
    }
}

/// `read_artifact`: a subagent's full output.
pub struct ReadArtifactTool {
    ctx: Arc<RunCtx>,
}

#[async_trait]
impl Tool for ReadArtifactTool {
    fn name(&self) -> &str {
        "read_artifact"
    }

    fn description(&self) -> String {
        "Read a subagent's complete output (task, findings, gaps, sources) by its id, e.g. sub-1.2. Use it when the summary you received was truncated or you need the detail.".into()
    }

    fn input_schema(&self) -> Value {
        json!({"type": "object", "additionalProperties": false, "required": ["id"], "properties": {"id": {"type": "string"}}})
    }

    async fn call(&self, _agent: &str, input: Value) -> ToolOutput {
        let id = str_arg(&input, "id");
        if !id.starts_with("sub-") || !id[4..].chars().all(|c| c.is_ascii_digit() || c == '.') {
            return ToolOutput::err("ids look like sub-1.2");
        }
        match self.ctx.store.read_text(&format!("subagents/{id}.md")) {
            Some(t) => ToolOutput::ok(t),
            None => ToolOutput::err(format!("no artifact {id}")),
        }
    }
}
