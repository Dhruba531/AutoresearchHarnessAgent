# AgentLab Research Hub

An autonomous research agent with human gates. You give it a question and
optionally data. It writes a brief, plans and runs experiments in a sandbox,
drafts a paper, and has four reviewers critique it. A groundedness check
runs before anything is exported. Every step that spends money waits for a
human to approve it. A second mode, **search campaigns**, hill-climbs a
program's score autoresearch-style.

> **Also in this repo:** [`mac-agent/`](mac-agent/README.md) — **Relay**, a Muse-style personal agent that
> runs on your Mac mini and takes tasks, approvals and live screen control from your phone.

Two parts in one repo:

| Part | Where | What |
| --- | --- | --- |
| Operator console + landing page | `src/` | TanStack Start (SSR React 19), deploys to Cloudflare Workers |
| Research backend | `backend/` | FastAPI + SQLite; models, sandbox, orchestration, live WebSockets |

## Quick start (local, no Supabase needed)

```bash
pip install -r backend/requirements.txt
```

```bash
npm install
```

```bash
cp .env.example .env    # VITE_API_BASE=http://localhost:8000 is already set
```

```bash
npm run backend          # API on :8000 (terminal 1)
```

```bash
npm run dev              # console on :8080 (terminal 2)
```

Open <http://localhost:8080/auth>, register a local account, and in the
console paste an API key under **Provider keys**:
**DeepSeek**, **Anthropic** or **OpenAI** for the agent itself, and optionally
**Gemini** for figures and **RunPod** for GPU execution. With several LLM keys
enabled, the most recently saved one drives runs.

Then: create a project → save an idea → (upload data) → **G1** generate and
approve the brief → **G2** estimate and approve the spend → watch the live
log → read the reviewers → **G3** targeted revisions → **G4** export.

## How a run works

```
brief ─G1─▶ plan ─▶ investigate ─▶ figures ─▶ paper ─▶ 4 reviewers ─▶ G3 revisions ─G4─▶ export
                     │
                     ├─ pipeline:    write experiment.py → run → auto-repair once
                     └─ super agent: lead agent + parallel sub-agents (below)
```

* **Budget.** The run stops (keeping its partial results) before any step
  that would push it past its own budget or the account's monthly cap.
  Every model call is metered from real token counts and recorded in a
  spend ledger.
* **Reviewers.** Methods, statistics, novelty and safety reviewers each
  see the paper, the brief and the actual execution record.
* **Groundedness gate (G4).** Export is blocked (HTTP 409) when the paper
  has placeholders (`TODO`, `XX%`), a citation `[n]` with no reference
  entry, Results numbers that do not appear in the execution output, a
  quantitative Results section when no code ran, or cited URLs that are
  dead. A reviewer can force-export, and that decision is recorded.
* **Audit trail.** Every gate decision is stored with the note the operator
  typed, and the trail is appended to the exported package.

### The super agent

The "Super agent" orchestration toggle (`backend/superagent.py`) is modelled
on open agent harnesses such as DeepSeek Harness, where models, tools and
skills are plugins, and DeerFlow, a lead agent with middleware, parallel
sub-agents, skills and memory:

* **Lead agent + sub-agents.** The lead plans and can `delegate` up to 3
  tasks at once to `coder`, `analyst` or `researcher` sub-agents. Each
  sub-agent works in its own sandbox folder with its own step budget and a
  15-minute timeout, and reports back.
* **Plugin tools.** `list_files`, `read_file`, `write_file`, `run_python`,
  `note`, `load_skill`, `remember`, `recall`, `delegate`, `finish`. Each is
  one registered class, so adding a tool takes one class.
* **Middleware.** Every tool call passes through permission, budget and
  output-trimming middleware.
* **Skills.** Markdown playbooks in `backend/skills/` (experiment design,
  tabular ML, statistics, debugging). Agents see only each skill's name and
  description, and load the full text when they need it.
* **Memory.** `remember` saves facts against the project, and later runs on
  that project start with them.

It uses a plain JSON action protocol, so it runs on DeepSeek
(`deepseek-reasoner` recommended), Claude or OpenAI.

### Search campaigns (`/campaigns`)

Upload a program (`train.py`, `main.py`, or any `.py`/`.ipynb`) that prints
`SCORE: <float>`, where higher is better. If you upload none, the agent
writes a baseline from the approved brief. Each iteration picks an edit
family (weighted, with an exploration rate), asks the model for one focused
change to the current best program, runs it, and keeps it only if the score
improves **and** the audit is clean:

* All other uploads are frozen and hashed before and after each run. Any
  change is a `metric_integrity` failure.
* A static leakage scan catches a hard-coded printed score, writes to frozen
  files, fitting on test variables, and network imports.
* Failures are classified as `syntax`, `import`, `shape`, `oom`, `nan`,
  `timeout`, `leakage` or `metric_integrity`.

The loop stops on any budget (USD, model calls, wall clock, GPU hours), the
iteration limit, the target score, stagnation, too high an invalid rate, or
Stop. The console shows the leaderboard, the Pareto frontier (score versus
runtime), the lineage graph, per-candidate traces and diffs, and lets you
promote or reject candidates.

## Code execution

`AGENTLAB_EXECUTION` selects the backend:

* `local` (default). A subprocess on the API host with its own directory, an
  environment stripped of keys, memory/CPU/file-size rlimits, a wall-clock
  timeout and process-group kill. **This is resource isolation, not a
  security boundary.** Run the backend in a container or as an unprivileged
  user, or use RunPod, if the code is untrusted.
* `runpod`. RunPod Serverless. Deploy `backend/runpod_worker.py` and save
  your RunPod key in the console, with the endpoint id as its model name.
* `none`. Execution off.

## Auth

* **Without Supabase** (no `VITE_SUPABASE_*` at build time), the console
  uses the backend's own email/password accounts (scrypt hashes, signed
  tokens).
* **With Supabase**, the console signs in against Supabase, Google included,
  and the backend verifies the access token: with `SUPABASE_JWT_SECRET`, via
  the project's JWKS, or by asking Supabase. `AGENTLAB_AUTH=supabase`
  disables local accounts.

WebSockets authenticate with `?token=`, because browsers cannot set headers
on a socket handshake. Provider keys are encrypted at rest (Fernet) and
never returned, only their last four characters.

## Configuration

Frontend: `.env` (see `.env.example`). Backend: `backend/.env` (see
`backend/.env.example`), covering the data dir, database URL, auth mode,
CORS origins, monthly cap, execution backend and limits, server-wide
fallback keys (off by default), and price overrides.

## Tests

```bash
npm run test:backend
```

The suite drives every gate through the real HTTP API with a scripted model
in place of the provider. Database, sandbox execution, WebSockets, budgets,
the groundedness gate and the campaign audit are all real. It covers a
pipeline run with execution, revisions and export, groundedness blocking a
draft, the super agent delegating to parallel sub-agents and writing
memory, and a campaign that improves its score while rejecting a cheating
and a broken candidate.

Other commands: `npm run build`, `npm run lint`, `npx tsc --noEmit`.

## Deploy

* **Console**: Cloudflare Workers. `VITE_API_BASE` must point at the deployed
  backend at build time.

  ```bash
  npm run build
  ```

  ```bash
  npx wrangler deploy
  ```

  Add the `*.workers.dev` origin to `AGENTLAB_CORS_ORIGINS`, and to
  Supabase's redirect allow-list if you use Supabase.
* **Backend**: any host that runs Python and keeps a disk (a VM, Fly.io,
  Railway, a container). Serve it over HTTPS:

  ```bash
  uvicorn backend.app:app --host 0.0.0.0 --port 8000
  ```

  Runs execute as in-process background tasks, so use one worker process.
  Runs that are in flight when the server restarts are marked failed.

## Layout

| Concern | File |
| --- | --- |
| API routes | `backend/app.py` |
| Run orchestration, brief, reviewers, groundedness, revisions | `backend/pipeline.py` |
| Super agent (lead, sub-agents, tools, middleware, skills, memory) | `backend/superagent.py`, `backend/skills/` |
| Search campaigns | `backend/campaign.py` |
| Model providers + pricing (Claude SDK, DeepSeek, OpenAI, Gemini) | `backend/llm.py` |
| Sandbox (local / RunPod) | `backend/sandbox.py` |
| Models / response shapes | `backend/db.py`, `backend/schemas.py` |
| Auth, crypto | `backend/auth.py`, `backend/security.py` |
| Console page | `src/routes/console.tsx`, `src/components/console/` |
| Campaign pages | `src/routes/campaigns.*.tsx`, `src/components/campaign/` |
| Typed API client (mirrors `backend/schemas.py`) | `src/lib/api.ts` |
