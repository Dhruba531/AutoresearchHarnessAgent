//! `research-agent`: the command line.

use clap::{Args, Parser, Subcommand};
use research_agent::config::{Config, Provider};
use research_agent::llm::build_model;
use research_agent::prompts::Prompts;
use research_agent::research::{http_client, ResearchSystem, RunOutcome};
use research_agent::store::{list_runs, RunStore};
use research_agent::trace::{read_events, summarize, ConsoleSink};
use research_agent::{doctor, evals};
use std::path::PathBuf;
use std::sync::Arc;

#[derive(Parser)]
#[command(
    name = "research-agent",
    version,
    about = "A multi-agent research system: a lead researcher, parallel search subagents and a citation agent."
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
    #[command(flatten)]
    opts: Opts,
}

#[derive(Args)]
struct Opts {
    /// anthropic | openai | deepseek | ollama (default: from RESEARCH_PROVIDER or the keys present)
    #[arg(long, global = true)]
    provider: Option<String>,
    /// Lead model, e.g. claude-opus-5-5 or deepseek:deepseek-chat
    #[arg(long, global = true)]
    lead_model: Option<String>,
    /// Subagent model
    #[arg(long, global = true)]
    subagent_model: Option<String>,
    /// Citation-agent model
    #[arg(long, global = true)]
    citation_model: Option<String>,
    /// Judge model (eval, judge)
    #[arg(long, global = true)]
    judge_model: Option<String>,
    /// auto | anthropic | tavily | brave | searxng | duckduckgo | none
    #[arg(long, global = true)]
    search: Option<String>,
    /// Folder of local documents the agents can search
    #[arg(long, global = true)]
    corpus: Option<PathBuf>,
    /// Where runs are stored (default ./research-data)
    #[arg(long, global = true)]
    data_dir: Option<PathBuf>,
    /// Folder of prompt overrides (see `prompts --dump`)
    #[arg(long, global = true)]
    prompts: Option<PathBuf>,
    /// Spend cap per run in USD (default 5)
    #[arg(long, global = true)]
    max_usd: Option<f64>,
    /// Subagents per run (default 18)
    #[arg(long, global = true)]
    max_subagents: Option<usize>,
    /// Wall-clock cap per run in minutes (default 45)
    #[arg(long, global = true)]
    max_minutes: Option<u64>,
    /// Lead effort: low | medium | high | xhigh | max (Claude)
    #[arg(long, global = true)]
    effort: Option<String>,
    /// Keep only structure in traces (no queries, outputs or thinking)
    #[arg(long, global = true)]
    no_trace_content: bool,
    /// Print every model and tool call
    #[arg(short, long, global = true)]
    verbose: bool,
    /// Print nothing but the result
    #[arg(short, long, global = true)]
    quiet: bool,
}

#[derive(Subcommand)]
enum Cmd {
    /// Research a question and print the cited report
    Run {
        /// The research question
        #[arg(required = true)]
        query: Vec<String>,
        /// Also write the report to this file
        #[arg(long, short)]
        output: Option<PathBuf>,
        /// Print the outcome as JSON instead of the report
        #[arg(long)]
        json: bool,
    },
    /// Continue a failed or interrupted run from its last checkpoint ("latest" works)
    Resume {
        run_id: String,
        #[arg(long, short)]
        output: Option<PathBuf>,
        #[arg(long)]
        json: bool,
    },
    /// List runs, newest first
    List {
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Print a run's report ("latest" works)
    Show {
        run_id: String,
        /// The lead's report before citations
        #[arg(long)]
        raw: bool,
    },
    /// Summarise a run's trace: tools, errors, tokens and cost per agent
    Trace {
        run_id: String,
        #[arg(long)]
        json: bool,
    },
    /// Grade a run with the LLM judge
    Judge {
        run_id: String,
        /// The expected answer, if there is one
        #[arg(long)]
        expected: Option<String>,
    },
    /// Run an eval set (default: the built-in starter set) and grade every case
    Eval {
        /// JSONL file of {"id", "query", "expected"?}
        #[arg(long)]
        set: Option<PathBuf>,
        /// Only the first N cases
        #[arg(long)]
        limit: Option<usize>,
        /// Only these case ids (repeatable)
        #[arg(long = "case")]
        cases: Vec<String>,
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// Diagnose a run and suggest prompt and tool-description fixes
    Doctor { run_id: String },
    /// Serve the web UI and HTTP API
    #[cfg(feature = "server")]
    Serve {
        #[arg(long, default_value = "127.0.0.1:8787")]
        addr: String,
    },
    /// Write the prompts to a folder for editing
    Prompts {
        #[arg(long)]
        dump: PathBuf,
    },
    /// Show the effective configuration (keys are never printed)
    Config,
}

fn config(o: &Opts) -> anyhow::Result<Config> {
    let mut c = Config::from_env();
    if let Some(p) = &o.provider {
        c.set_provider(Provider::parse(p).ok_or_else(|| anyhow::anyhow!("unknown provider {p}"))?);
    }
    c.apply_models(
        o.lead_model.clone(),
        o.subagent_model.clone(),
        o.citation_model.clone(),
        o.judge_model.clone(),
    );
    if let Some(s) = &o.search {
        c.search.backend = s.clone();
    }
    if let Some(d) = &o.corpus {
        c.corpus_dir = Some(d.clone());
    }
    if let Some(d) = &o.data_dir {
        c.data_dir = d.clone();
    }
    if let Some(d) = &o.prompts {
        c.prompts_dir = Some(d.clone());
    }
    if let Some(v) = o.max_usd {
        c.limits.max_usd = v;
    }
    if let Some(v) = o.max_subagents {
        c.limits.max_subagents_total = v;
    }
    if let Some(v) = o.max_minutes {
        c.limits.max_minutes = v;
    }
    if let Some(e) = &o.effort {
        c.lead_effort = e.clone();
    }
    if o.no_trace_content {
        c.trace_content = false;
    }
    Ok(c)
}

fn system(cfg: &Config, o: &Opts) -> anyhow::Result<ResearchSystem> {
    let mut sys = ResearchSystem::new(cfg.clone())?;
    if !o.quiet {
        sys.add_sink(Arc::new(ConsoleSink { verbose: o.verbose }));
    }
    Ok(sys)
}

/// "latest" / "last" → the newest run.
fn resolve_id(cfg: &Config, id: &str) -> anyhow::Result<String> {
    if id == "latest" || id == "last" {
        return list_runs(&cfg.runs_dir())
            .first()
            .map(|m| m.id.clone())
            .ok_or_else(|| anyhow::anyhow!("no runs yet"));
    }
    Ok(id.to_string())
}

fn print_outcome(out: &RunOutcome, output: &Option<PathBuf>, json: bool) -> anyhow::Result<()> {
    if let Some(path) = output {
        std::fs::write(path, &out.report)?;
        eprintln!("report written to {}", path.display());
    }
    if json {
        println!("{}", serde_json::to_string_pretty(out)?);
    } else {
        println!("{}", out.report);
        eprintln!(
            "\nrun {} · {} · ${:.3} · {} subagents · {} sources cited · {}",
            out.run_id,
            out.status,
            out.spend.usd,
            out.subagents,
            out.sources_cited,
            out.dir.display()
        );
    }
    Ok(())
}

/// Run to completion, or checkpoint and exit cleanly on Ctrl-C.
async fn execute(sys: &ResearchSystem, cfg: &Config, id: &str) -> anyhow::Result<RunOutcome> {
    tokio::select! {
        r = sys.execute(id) => r,
        _ = tokio::signal::ctrl_c() => {
            if let Ok(store) = RunStore::open(&cfg.runs_dir(), id) {
                if let Ok(mut meta) = store.meta() {
                    meta.status = "interrupted".into();
                    let _ = store.save_meta(&mut meta);
                }
            }
            eprintln!("\ninterrupted; progress is checkpointed. Continue with: research-agent resume {id}");
            std::process::exit(130);
        }
    }
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    if let Err(e) = real_main(cli).await {
        eprintln!("error: {e:#}");
        std::process::exit(1);
    }
}

async fn real_main(cli: Cli) -> anyhow::Result<()> {
    let o = &cli.opts;
    let cfg = config(o)?;
    match cli.cmd {
        Cmd::Run {
            query,
            output,
            json,
        } => {
            let sys = system(&cfg, o)?;
            let id = sys.create_run(&query.join(" "), vec![])?;
            let out = execute(&sys, &cfg, &id).await?;
            print_outcome(&out, &output, json)
        }
        Cmd::Resume {
            run_id,
            output,
            json,
        } => {
            let sys = system(&cfg, o)?;
            let id = resolve_id(&cfg, &run_id)?;
            let out = execute(&sys, &cfg, &id).await?;
            print_outcome(&out, &output, json)
        }
        Cmd::List { limit } => {
            let runs = list_runs(&cfg.runs_dir());
            if runs.is_empty() {
                eprintln!("no runs in {}", cfg.runs_dir().display());
            }
            for m in runs.into_iter().take(limit) {
                println!(
                    "{}  {:<11} ${:<7.3} {}",
                    m.id,
                    m.status,
                    m.cost_usd,
                    research_agent::util::clip(&m.query, 90)
                );
            }
            Ok(())
        }
        Cmd::Show { run_id, raw } => {
            let store = RunStore::open(&cfg.runs_dir(), &resolve_id(&cfg, &run_id)?)?;
            let text = if raw {
                store.read_text("report.raw.md")
            } else {
                store.report()
            };
            println!(
                "{}",
                text.ok_or_else(|| anyhow::anyhow!("this run has no report yet"))?
            );
            Ok(())
        }
        Cmd::Trace { run_id, json } => {
            let store = RunStore::open(&cfg.runs_dir(), &resolve_id(&cfg, &run_id)?)?;
            let summary = summarize(&read_events(&store.path("trace.jsonl")));
            if json {
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else {
                println!("{}", summary.to_markdown());
            }
            Ok(())
        }
        Cmd::Judge { run_id, expected } => {
            let sys = ResearchSystem::new(cfg.clone())?;
            let judge = build_model(&cfg, &cfg.judge_model, &http_client())?;
            let g = evals::judge_run(
                &sys,
                judge,
                &resolve_id(&cfg, &run_id)?,
                expected.as_deref(),
            )
            .await?;
            println!("{}", serde_json::to_string_pretty(&g)?);
            Ok(())
        }
        Cmd::Eval {
            set,
            limit,
            cases,
            out,
        } => {
            let mut all = evals::load_cases(set.as_deref())?;
            if !cases.is_empty() {
                all.retain(|c| cases.contains(&c.id));
            }
            if let Some(n) = limit {
                all.truncate(n);
            }
            anyhow::ensure!(!all.is_empty(), "no eval cases selected");
            let sys = system(&cfg, o)?;
            let judge = build_model(&cfg, &cfg.judge_model, &http_client())?;
            let (dir, results) = evals::run_eval(&sys, judge, &all, out).await?;
            println!("{}", evals::summary(&results));
            eprintln!("results in {}", dir.display());
            Ok(())
        }
        Cmd::Doctor { run_id } => {
            let store = RunStore::open(&cfg.runs_dir(), &resolve_id(&cfg, &run_id)?)?;
            let model = build_model(&cfg, &cfg.lead_model, &http_client())?;
            let prompts = Prompts::load(cfg.prompts_dir.as_deref());
            let text =
                doctor::diagnose(model, &prompts.doctor, &prompts, &store, cfg.max_retries).await?;
            println!("{text}");
            eprintln!("saved to {}", store.path("doctor.md").display());
            Ok(())
        }
        #[cfg(feature = "server")]
        Cmd::Serve { addr } => {
            let sys = system(&cfg, o)?;
            research_agent::server::serve(sys, &addr).await
        }
        Cmd::Prompts { dump } => {
            Prompts::load(cfg.prompts_dir.as_deref()).dump(&dump)?;
            eprintln!(
                "prompts written to {}; use them with --prompts {} (or RESEARCH_PROMPTS_DIR)",
                dump.display(),
                dump.display()
            );
            Ok(())
        }
        Cmd::Config => {
            let mut v = serde_json::to_value(&cfg)?;
            v["keys_present"] = serde_json::json!({
                "anthropic": cfg.keys.anthropic.is_some(), "openai": cfg.keys.openai.is_some(),
                "deepseek": cfg.keys.deepseek.is_some(), "tavily": cfg.search.tavily_key.is_some(),
                "brave": cfg.search.brave_key.is_some(),
            });
            println!("{}", serde_json::to_string_pretty(&v)?);
            Ok(())
        }
    }
}
