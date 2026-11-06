# research-agent

A multi-agent research system in Rust, built after Anthropic's engineering post
[*How we built our multi-agent research system*](https://www.anthropic.com/engineering/multi-agent-research-system).
A **lead researcher** plans the work and delegates it to **search subagents** that
investigate in parallel, each in its own context. The lead judges what comes back,
runs more rounds if needed, and writes the report. A **citation agent** then
attributes each claim to the sources behind it.

```text
question ─▶ LeadResearcher ── plan ──▶ memory
              │  run_subagents (one round, in parallel)
              ├─▶ subagent ─ web_search · web_fetch · corpus ─▶ findings ─▶ subagents/<id>.md
              ├─▶ subagent ─ …
              │  synthesise · more research needed? ─▶ another round
              └─ complete_task(report)
                    └─▶ CitationAgent ─▶ report.md with [n] markers and a source list
```

The folder is self-contained: its own Cargo workspace, no code shared with the rest
of the repository. To detach it, copy the folder, or keep its history with
`git subtree split --prefix=research-agent -b research-agent`.

## Quick start

```bash
cd research-agent
```

```bash
cargo build --release
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
./target/release/research-agent run "What caused the 2020-2023 semiconductor shortage, and how did it hit the car industry?"
```

The report goes to stdout. Progress (subagents starting and finishing, the
lead's tool calls, citations, cost) goes to stderr; add `-v` to see every model
and tool call, or `-q` for none. Every run is stored under `research-data/runs/<id>/`.

With only a Claude key, search runs on Claude's server-side web search, so
nothing else is needed. For other models, add a search key (see below).

## How the article's lessons map to the code

| Lesson from the post | Where it lives |
| --- | --- |
| Orchestrator-worker: a lead agent spawns parallel subagents, each with its own context | `research.rs` (`RunSubagentsTool`, `run_subagent`) |
| Lead plans and saves the plan to memory, which survives context truncation | `memory_save` / `memory_read` (`tools/memory.rs`); the handoff in `LeadHooks` |
| **Teach the orchestrator to delegate**: every task has an objective, output format, guidance on tools and sources, boundaries | the required fields of `run_subagents`, and `prompts/lead.md` |
| **Scale effort to query complexity**: 1 agent / 3-10 calls for simple facts, 2-4 for comparisons, more for broad research | `prompts/lead.md`; per-task `tool_budget`; hard caps per round and per run (`config.rs`) |
| **Tool design matters**: distinct purpose, descriptions that say when to use the tool | every tool's `description()` in `tools/` |
| **Start wide, then narrow**; **judge source quality** (primary sources over SEO farms) | `prompts/subagent.md`, `prompts/lead.md` |
| **Guide the thinking**: extended thinking for planning, interleaved thinking after tool results | adaptive thinking on every Claude call (`llm/anthropic.rs`); summaries kept in the trace |
| **Parallel tool calling**: 3-5 subagents at once, several tools per turn | concurrent subagents (`parallel_subagents`) and concurrent tool calls in `agent.rs` |
| Subagents write to the filesystem to avoid a "game of telephone" | `subagents/<id>.md` + transcript; the lead gets a summary and `read_artifact` |
| CitationAgent attributes claims to sources | `citations.rs`: the model returns verbatim claims, code inserts the markers |
| **Let agents improve themselves** | `research-agent doctor <run>` diagnoses a run and proposes prompt and tool-description edits |
| **Start evaluating with small samples**; **LLM-as-judge** with one rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency) | `evals/starter.jsonl` (20 cases), `judge.rs`, `research-agent eval` |
| **Errors compound; resume, don't restart**; retries and checkpoints | checkpoint after every lead turn and subagent; `research-agent resume`; retries with backoff (`llm/mod.rs`) |
| Let the agent know when a tool fails and adapt | tool errors come back as `is_error` results with a hint, never a crash |
| **Full tracing**, monitoring decision patterns without reading conversation contents | `trace.jsonl`; `research-agent trace`; `--no-trace-content` keeps structure only |
| Long-horizon context management: summarise, store externally, continue in a fresh context | when the lead's prompt passes `lead_context_tokens`, it restarts from its memory and the subagents' saved outputs |

Two details beyond the post:

* **Citations can't change the report.** The citation model only returns
  `(claim, source ids)` pairs, each claim copied from the report. Code finds each
  claim and inserts `[n]`; a claim it cannot find is dropped, and only sources
  recorded in the run can be cited. The cited report is the lead's text plus markers.
* **Histories are append-only.** No agent edits an earlier message. Budget
  reminders are appended after tool results, and a context reset starts a new
  conversation rather than trimming the old one. That keeps Claude's thinking
  blocks valid and the prompt cache warm.

## Models and providers

| Provider | Key | Default lead → subagents |
| --- | --- | --- |
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `claude-opus-5-5` → `claude-sonnet-5-5` |
| `openai` (or any OpenAI-compatible API via `OPENAI_BASE_URL`) | `OPENAI_API_KEY` | `gpt-4.1` → `gpt-4.1-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` → `deepseek-chat` |
| `ollama` | none (`OLLAMA_BASE_URL`) | `qwen3:32b` → `qwen3:8b` |

The Claude defaults mirror the post: an Opus lead with Sonnet subagents. The
citation agent defaults to Sonnet and the judge to Opus. Set any of them with
`--lead-model`, `--subagent-model`, `--citation-model` and `--judge-model`, or
the matching `RESEARCH_*_MODEL` variables. A `provider:` prefix mixes providers,
for example `--subagent-model deepseek:deepseek-chat` under a Claude lead.

Claude is called over HTTPS (`/v1/messages`, API version `2026-10-21`), streamed,
because Rust has no official Anthropic SDK. Each request turns on adaptive
thinking with an explicit effort (lead `high`, subagents `medium`), automatic
prompt caching, and the server-side refusal fallback (`fallbacks: "default"`) on
the models that accept it. The fallback is dropped automatically if your account
rejects it.

## Search and local documents

`--search` / `RESEARCH_SEARCH`:

| Backend | Needs | Notes |
| --- | --- | --- |
| `auto` (default) | — | the first of Tavily, Brave or SearXNG that is configured; else Claude's server-side search if the subagents run on Claude; else DuckDuckGo |
| `anthropic` | Claude subagents | Claude's `web_search` / `web_fetch` server tools (dynamic filtering on current models), $10 per 1,000 searches; sources are collected from the results and citations |
| `tavily` | `TAVILY_API_KEY` | |
| `brave` | `BRAVE_API_KEY` | |
| `searxng` | `SEARXNG_URL` | self-hosted, no key |
| `duckduckgo` | — | the keyless HTML endpoint; best effort, and may block automated use |
| `none` | — | no web, e.g. corpus-only research |

`--corpus DIR` (or `RESEARCH_CORPUS`) gives every agent `corpus_search` (BM25)
and `corpus_read` over a folder of `.md`, `.txt`, `.html`, `.csv`, `.json` and
similar files. Use it for material the web cannot see: notes, internal docs,
exports. The prompts tell agents to prefer it when the question is about your
own material.

## Commands

```text
research-agent run <question…> [-o report.md] [--json]   research a question
research-agent resume <run|latest>                       continue a failed or interrupted run
research-agent list                                      runs, newest first
research-agent show <run|latest> [--raw]                 print the report (raw = before citations)
research-agent trace <run|latest> [--json]               tools, errors, tokens and cost per agent
research-agent judge <run> [--expected "…"]              grade a run with the LLM judge
research-agent eval [--set cases.jsonl] [--case id] [--limit n]   run and grade an eval set
research-agent doctor <run>                              diagnose a run, propose prompt fixes
research-agent serve [--addr 127.0.0.1:8787]             web UI + HTTP API with live events
research-agent prompts --dump DIR                        write the prompts out for editing
research-agent config                                    effective configuration (no keys)
```

Global flags: `--provider`, the `--*-model` flags, `--search`, `--corpus`,
`--data-dir`, `--prompts`, `--max-usd`, `--max-subagents`, `--max-minutes`,
`--effort`, `--no-trace-content`, `-v`, `-q`.

## Runs on disk, and resuming

```text
research-data/runs/<run-id>/
  run.json            query, status, models, cost
  state.json          checkpoint: phase, the lead's conversation, every subagent result
  memory.json         the lead's plan and progress notes
  sources.json        every source seen; sources/<id>.txt holds fetched text
  subagents/<id>.md   a subagent's full output (and <id>.messages.json, its transcript)
  report.raw.md       the lead's report
  report.md           the cited report
  citations.json      which claims cite which sources
  trace.jsonl         one line per model call, tool call and lifecycle event
  grade.json          (after `judge`)  ·  doctor.md (after `doctor`)
```

The run is checkpointed after each of the lead's replies and turns, and after
each finished subagent. If a provider fails after its retries, or you press Ctrl-C,
`research-agent resume <id>` continues from the checkpoint. Subagents that
finished are not run again; spend and elapsed time carry over.

## Limits and cost

Multi-agent research uses far more tokens than chat (the post measures roughly
15x), so limits are enforced in code. The defaults: $5 and 45 minutes per run,
6 subagents per round, 18 per run, 5 at a time, at most 25 tool calls per
subagent. At 80% of the budget the lead is told to stop delegating and write the
report. At the limit, agents stop, and if the lead never wrote a report, the run
still produces one compiled from the subagents' findings, marked incomplete.
Costs are computed from real token counts and list prices (`llm/pricing.rs`;
override any model with `RESEARCH_PRICE_<MODEL>="input,output"`).

## Improving it: evals, judge, doctor, prompts

The post's advice is to start evaluating at once with a small set. The loop:

1. `research-agent eval`: runs the 20-case starter set (simple facts with known
   answers, comparisons, depth and breadth questions) and grades each run.
   Results go to `research-data/evals/<id>/summary.md`. This costs real money:
   try `--limit 3` first.
2. `research-agent trace <run>` and `research-agent doctor <run>` on the weak
   cases: what went wrong, and which prompt lines to change.
3. `research-agent prompts --dump my-prompts`, edit, and re-run with
   `--prompts my-prompts`. No rebuild needed.

## Web UI and API

`research-agent serve` serves a page at <http://127.0.0.1:8787> to start runs,
watch them live and read the reports, plus a JSON API:

```text
GET  /api/runs                  POST /api/runs {"query": "…"}
GET  /api/runs/{id}             GET  /api/runs/{id}/events   (server-sent events)
POST /api/runs/{id}/resume
```

It binds to localhost. Set `RESEARCH_AGENT_TOKEN` before listening anywhere else:
requests then need `Authorization: Bearer <token>` (or `?token=`). Build without
the server with `cargo build --no-default-features`.

## Configuration

Every flag has an environment variable; see [`.env.example`](.env.example). The
binary reads the environment only; to use a file, `set -a; . ./.env; set +a`.
`research-agent config` prints the effective settings and which keys are present.

## Development

```bash
cargo test
```

```bash
cargo clippy --all-targets
```

The tests need no network or keys. A scripted fake model drives full runs:
delegation, parallel subagents, citations, resume after a failure, context
reset, limits, the judge, evals, doctor and the HTTP API. The real Claude and
OpenAI-compatible clients are tested over HTTP against local mock servers
(streaming, fallback downgrade, error mapping).

## Known limits

* Rounds are synchronous, as in the post: the lead waits for every subagent of
  a round before it continues, and can't steer them mid-flight.
* `web_fetch` reads HTML and text, not PDFs.
* No MCP client yet. Tools are one trait (`tools/mod.rs`), so adding one is a
  struct and an `impl Tool`.
