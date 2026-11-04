"""The research run: brief → plan → (code + execution) → figures → paper → reviewers.

A run executes as a background asyncio task. Every step logs to the database
and to the live-log socket, every model call is charged to the run's budget
and the user's monthly ledger, and the run stops — keeping what it produced —
the moment either limit is reached.

Two orchestration modes:

pipeline  fixed sequence: plan, write one experiment script, run it (one
          automatic repair on failure), draft, review.
agentic   the super agent (backend/superagent.py): a lead agent with plugin
          tools, on-demand skills and project memory that delegates to
          parallel sub-agents; the same draft and review stages then run on
          what it found.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

from . import llm, sandbox
from .config import settings
from .db import Artifact, Brief, Project, ReviewFinding, Run, RunLog, User, session_scope, utcnow
from .events import hub
from .ledger import BudgetExceeded, monthly_spend, record

REVIEWERS = {
    "methods": "Methods reviewer. Judge whether the design actually tests the stated hypothesis: controls, baselines, "
    "confounds, ablations, and whether the conclusions follow from what was run.",
    "statistics": "Statistics reviewer. Judge sample sizes, variance and seeds, significance or uncertainty "
    "reporting, metric choice, and whether any number in the paper is unsupported by the execution output.",
    "novelty": "Novelty and related-work reviewer. Judge whether the contribution is clearly positioned, whether "
    "cited work is real and relevant, and whether claims of novelty are overstated.",
    "safety": "Safety and integrity reviewer. Look for data leakage, test-set contamination, fabricated or "
    "unverifiable citations, placeholder text, overclaiming, and dual-use or ethical risks.",
}

# Token assumptions per stage (input, output) used for estimates and gating.
STAGE_TOKENS = {
    "plan": (3_000, 4_000),
    "code": (5_000, 8_000),
    "repair": (9_000, 8_000),
    "agent_step": (10_000, 3_000),
    "draft": (10_000, 12_000),
    "review": (14_000, 3_000),
    "brief": (2_000, 4_000),
}


def project_dir(project_id: int) -> Path:
    return settings.data_dir / "projects" / str(project_id)


def uploads_dir(project_id: int) -> Path:
    return project_dir(project_id) / "uploads"


def run_dir(run_id: int) -> Path:
    return settings.data_dir / "runs" / str(run_id)


def list_uploads(project_id: int) -> dict[str, Path]:
    d = uploads_dir(project_id)
    if not d.is_dir():
        return {}
    return {p.name: p for p in sorted(d.iterdir()) if p.is_file()}


def describe_uploads(project_id: int, preview_bytes: int = 600) -> str:
    files = list_uploads(project_id)
    if not files:
        return "No files were uploaded for this project."
    lines = []
    for name, p in files.items():
        lines.append(f"- {name} ({p.stat().st_size} bytes)")
        if p.suffix.lower() in {".csv", ".txt", ".json", ".py", ".md", ".tsv"}:
            head = p.read_text(errors="replace")[:preview_bytes]
            lines.append("  ```\n  " + head.replace("\n", "\n  ") + "\n  ```")
    return "\n".join(lines)


# ---- estimation -----------------------------------------------------------


def estimate(model: str, config: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    agentic = config.get("orchestration") == "agentic"
    execute = bool(config.get("execute"))
    figures = bool(config.get("figures"))
    calls: list[tuple[str, int]] = [("plan", 1)]
    if agentic:
        from .superagent import LEAD_STEPS, MAX_PARALLEL, SUB_STEPS

        # typical: the lead plus one round of parallel sub-agents
        calls.append(("agent_step", LEAD_STEPS + MAX_PARALLEL * SUB_STEPS))
    elif execute:
        calls += [("code", 1), ("repair", 1)]
    calls += [("draft", 1), ("review", len(REVIEWERS))]
    total = 0.0
    for stage, n in calls:
        tin, tout = STAGE_TOKENS[stage]
        total += n * llm.cost_of(model, tin, tout)
    if figures:
        total += llm.IMAGE_PRICE_USD
    compute = 0.0
    if execute and sandbox.backend_name() == "runpod":
        compute = sandbox.GPU_RATES["rtx 4090"] * (settings.exec_timeout_s / 3600) * (3 if agentic else 2)
    total += compute
    assumptions = (
        f"{model} · {'super agent (lead + 3 parallel sub-agents)' if agentic else 'pipeline'}"
        f" · {len(REVIEWERS)} reviewers"
        f"{' · code execution (' + sandbox.backend_name() + ')' if execute else ''}"
        f"{' · 1 figure' if figures else ''}. Upper-bound token assumptions; actual cost is metered per call."
    )
    return round(total, 4), {"text": assumptions, "model": model, "compute_usd": round(compute, 4)}


# ---- brief ------------------------------------------------------------------

BRIEF_SYSTEM = """You are a principal investigator writing a research brief that a small autonomous research \
agent will execute and a human will approve before any money is spent. Be concrete and scoped to what one \
run can actually do: a modest experiment on the supplied data (or a small synthetic/simulated study if no data \
is supplied) plus a written report. Never invent citations; if you mention prior work, name only work you are \
certain exists and say so plainly when you are unsure."""

BRIEF_TEMPLATE = """Write the research brief in Markdown with exactly these sections:

# <Title>
## Question
## Hypothesis
## Background
## Method
## Data
## Success criteria
## Risks and limitations
## Out of scope

Research idea from the operator:
<<<
{idea}
>>>

Project objective: {objective}

Files the operator uploaded:
{uploads}
{feedback}"""


async def generate_brief(project_id: int, user_id: int, feedback: str = "") -> Brief:
    with session_scope() as db:
        project = db.get(Project, project_id)
        user = db.get(User, user_id)
        if not (project.idea_text or project.objective):
            raise ValueError("Save a research idea before generating a brief.")
        client = llm.text_client(db, user)
        prev = db.scalar(select(Brief).where(Brief.project_id == project_id).order_by(Brief.version.desc()).limit(1))
        prompt = BRIEF_TEMPLATE.format(
            idea=project.idea_text or project.objective,
            objective=project.objective or "(none given)",
            uploads=describe_uploads(project_id),
            feedback=(
                f"\nThe previous draft (v{prev.version}) was sent back. Address this reviewer note:\n{feedback}\n"
                if prev and feedback
                else ""
            ),
        )
    res = await client.complete(BRIEF_SYSTEM, prompt, max_tokens=8000, effort="medium")
    with session_scope() as db:
        prev_version = db.scalar(select(func.max(Brief.version)).where(Brief.project_id == project_id)) or 0
        brief = Brief(project_id=project_id, version=prev_version + 1, content_markdown=res.text.strip(),
                      cost_usd=res.cost_usd)
        db.add(brief)
        project = db.get(Project, project_id)
        project.status = "brief_ready"
        project.updated_at = utcnow()
        record(db, user_id, res.cost_usd, "brief", project_id, f"brief v{prev_version + 1} · {res.model}")
        db.flush()
        db.refresh(brief)
        return brief


# ---- the run ----------------------------------------------------------------


@dataclass
class RunContext:
    run_id: int
    user_id: int
    project_id: int
    budget: float
    client: llm.TextClient
    spent: float = 0.0
    seq: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)

    def log(self, message: str, level: str = "info") -> None:
        self.seq += 1
        now = utcnow()
        with session_scope() as db:
            db.add(RunLog(run_id=self.run_id, sequence=self.seq, level=level, message=message, created_at=now))
        hub.publish(f"run:{self.run_id}", {"type": "log", "level": level, "message": message,
                                           "sequence": self.seq, "created_at": now.isoformat()})

    def status(self, status: str, **fields: Any) -> None:
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            run.status = status
            for k, v in fields.items():
                setattr(run, k, v)
        hub.publish(f"run:{self.run_id}", {"type": "status", "status": status})

    def guard(self, stage: str) -> None:
        """Refuse to start a stage that would likely push spend past a limit."""
        key = stage.split(":", 1)[0]
        key = {"agent": "agent_step", "review": "review"}.get(key, key)
        tin, tout = STAGE_TOKENS.get(key, (5_000, 5_000))
        next_cost = llm.cost_of(self.client.model, tin, tout) * 0.5  # typical, not worst case
        if self.spent + next_cost > self.budget:
            raise BudgetExceeded(
                f"Run budget ${self.budget:.2f} reached (spent ${self.spent:.2f}; next step '{stage}' ≈ ${next_cost:.2f})."
            )
        with session_scope() as db:
            if monthly_spend(db, self.user_id) + next_cost > settings.monthly_cap_usd:
                raise BudgetExceeded(f"Monthly cap ${settings.monthly_cap_usd:.2f} reached.")

    def charge(self, amount: float, detail: str) -> None:
        self.spent += amount
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            run.actual_cost = round(self.spent, 6)
            record(db, self.user_id, amount, "run", self.run_id, detail)

    async def ask(self, stage: str, system: str, prompt: str, *, max_tokens: int = 12_000,
                  effort: str = "medium") -> llm.LLMResult:
        self.guard(stage)
        res = await self.client.complete(system, prompt, max_tokens=max_tokens, effort=effort)
        self.charge(res.cost_usd, f"{stage} · {res.model}")
        self.log(f"{stage}: {res.model} · {res.input_tokens}→{res.output_tokens} tok · ${res.cost_usd:.4f}")
        return res

    def save_artifact(self, kind: str, name: str, data: bytes | str, mime: str | None = None) -> None:
        d = run_dir(self.run_id) / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data.encode() if isinstance(data, str) else data
        path.write_bytes(raw)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        with session_scope() as db:
            existing = db.scalar(select(Artifact).where(Artifact.run_id == self.run_id, Artifact.name == name))
            if existing:
                existing.size_bytes, existing.mime_type = len(raw), mime
            else:
                db.add(Artifact(run_id=self.run_id, project_id=self.project_id, kind=kind, name=name,
                                path=str(path), size_bytes=len(raw), mime_type=mime))

    def save_analysis(self, **fields: Any) -> None:
        with session_scope() as db:
            run = db.get(Run, self.run_id)
            data = dict(run.analysis_json or {})
            data.update(fields)
            run.analysis_json = data


PLAN_SYSTEM = """You are the planning stage of an autonomous research agent. Turn an approved research brief \
into an executable plan for this single run. Be specific about what will be computed and how results will be \
judged. Do not promise anything the run cannot do."""

CODE_SYSTEM = """You write a single, self-contained Python 3 experiment script for an autonomous research run.

Rules:
- Standard library plus whatever of numpy, pandas, scipy, scikit-learn and matplotlib is installed — import \
optional libraries inside try/except and degrade gracefully if missing. No network access, no pip installs.
- Read only the uploaded files listed (they sit in the working directory) or generate data in-script with a \
fixed random seed.
- Finish well within {timeout} seconds on a CPU.
- Print a short human-readable summary of results to stdout.
- Write every number the paper may cite to results.json (a flat or shallow JSON object).
- Save any figures as PNG files in the working directory (use matplotlib with the Agg backend).
- Never fabricate results: every reported number must be computed by the script.

Reply with the complete script in one ```python fenced block and nothing else."""

DRAFT_SYSTEM = """You write the research paper for an autonomous research run, in Markdown.

Integrity rules, which reviewers and an automated groundedness gate will check:
- Every quantitative claim must come from the execution output or results.json provided. If no code ran, \
say so and present the work as a proposal or analysis, with no empirical numbers.
- Cite only work you are certain exists. Use numbered citations [1], [2] that map to a "## References" \
section; include a URL (DOI or arXiv link) only when you are sure it is correct. Fewer real references beat \
many doubtful ones. Leave out References entirely if you cite nothing.
- No placeholders (TODO, TBD, XX, [citation needed], lorem ipsum).
- Refer to figures by their file names when they exist.

Sections: Title (# heading), Abstract, Introduction, Method, Results, Discussion, Limitations, Conclusion, \
References (if any)."""

REVIEW_SYSTEM = """You are the {role}

Review the paper below against the brief and the actual execution record. Be specific and terse; quote the \
passage you are criticising. Reply with one JSON object in a ```json fence:

{{"severity": "low" | "medium" | "high",
  "score": <integer 1-10>,
  "review_markdown": "<your review in Markdown: a '## Summary' section, a '## Strengths' bullet list, a \
'## Weaknesses' bullet list>",
  "suggested_fix": "<the single most important change, one or two sentences>"}}

severity is the worst problem you found: high means a conclusion is unsupported or integrity is in doubt."""


async def execute_run(run_id: int) -> None:
    with session_scope() as db:
        run = db.get(Run, run_id)
        user = db.get(User, run.owner_id)
        brief = db.get(Brief, run.brief_id) if run.brief_id else None
        project = db.get(Project, run.project_id)
        config = dict(run.config_json or {})
        client = llm.text_client(db, user)
        gemini = llm.provider_credentials(db, user, "gemini") if config.get("figures") else None
        runpod = llm.provider_credentials(db, user, "runpod") if config.get("execute") else None
        ctx = RunContext(run_id, user.id, project.id, run.budget_threshold, client)
        ctx.seq = db.scalar(select(func.max(RunLog.sequence)).where(RunLog.run_id == run_id)) or 0
        brief_md = brief.content_markdown if brief else project.idea_text
        project_title = project.title

    ctx.status("running", started_at=utcnow())
    ctx.log(f"Run #{run_id} started · {client.provider}/{client.model} · budget ${ctx.budget:.2f}")
    workdir = run_dir(run_id) / "work"
    try:
        uploads = list_uploads(ctx.project_id)
        sandbox.prepare_workdir(workdir, uploads, {})
        if uploads:
            ctx.log(f"Staged {len(uploads)} uploaded file(s): {', '.join(uploads)}")

        # 1. plan
        ctx.log("Planning from the approved brief…")
        plan = (await ctx.ask("plan", PLAN_SYSTEM,
                              f"Approved brief:\n\n{brief_md}\n\nUploaded files:\n{describe_uploads(ctx.project_id)}\n\n"
                              f"Code execution is {'ENABLED' if config.get('execute') else 'DISABLED'} for this run.\n"
                              "Write the plan as Markdown: ## Approach, ## Steps (numbered), ## Metrics, ## Expected outputs.",
                              max_tokens=8000)).text
        ctx.save_artifact("report", "plan.md", plan)

        # 2. investigate
        exec_result: sandbox.ExecResult | None = None
        notes: list[str] = []
        if config.get("orchestration") == "agentic":
            from .superagent import run_super_agent

            exec_result, notes = await run_super_agent(ctx, brief_md, plan, describe_uploads(ctx.project_id), workdir,
                                                       bool(config.get("execute")), runpod)
        elif config.get("execute"):
            exec_result = await _pipeline_execute(ctx, brief_md, plan, workdir, runpod)
        else:
            ctx.log("Code execution disabled — the paper will be an analysis without new empirical results.")

        results_json: Any = None
        if exec_result is not None:
            results_json = _collect_outputs(ctx, workdir, exec_result)

        # 3. figures via Gemini
        if config.get("figures") and gemini:
            await _figure(ctx, gemini, brief_md, plan)

        # 4. draft
        ctx.log("Drafting the paper…")
        figures = _figure_names(ctx.run_id)
        evidence = _evidence(exec_result, results_json, notes, figures)
        draft = (await ctx.ask("draft", DRAFT_SYSTEM,
                               f"Project: {project_title}\n\nApproved brief:\n{brief_md}\n\nPlan:\n{plan}\n\n{evidence}",
                               max_tokens=16_000, effort="high")).text.strip()
        ctx.save_artifact("report", "paper.md", draft)
        ctx.status("review", draft_markdown=draft)

        # 5. reviewers
        ctx.log(f"Running {len(REVIEWERS)} reviewers…")
        await _review(ctx, brief_md, draft, evidence)

        ctx.status("completed", ended_at=utcnow())
        with session_scope() as db:
            project = db.get(Project, ctx.project_id)
            project.status = "review"
        ctx.log(f"Run complete · total ${ctx.spent:.4f}", "info")
    except asyncio.CancelledError:
        ctx.log("Run cancelled by operator.", "warning")
        ctx.status("cancelled", ended_at=utcnow())
        raise
    except BudgetExceeded as exc:
        ctx.log(str(exc), "error")
        ctx.status("failed", ended_at=utcnow(), error=str(exc))
    except Exception as exc:  # any provider or sandbox failure ends the run visibly
        msg = f"{type(exc).__name__}: {exc}"
        ctx.log(f"Run failed — {msg}", "error")
        ctx.status("failed", ended_at=utcnow(), error=msg)
    finally:
        ctx.save_analysis(actions=ctx.actions) if ctx.actions else None
        with session_scope() as db:
            project = db.get(Project, ctx.project_id)
            run = db.get(Run, run_id)
            if run.status in {"failed", "cancelled"}:
                project.status = "brief_approved"
            project.updated_at = utcnow()


async def _pipeline_execute(ctx: RunContext, brief_md: str, plan: str, workdir: Path,
                            runpod: tuple[str, str] | None) -> sandbox.ExecResult | None:
    ctx.log("Writing the experiment script…")
    system = CODE_SYSTEM.format(timeout=settings.exec_timeout_s)
    prompt = f"Brief:\n{brief_md}\n\nPlan:\n{plan}\n\nUploaded files:\n{describe_uploads(ctx.project_id)}"
    res = await ctx.ask("code", system, prompt, max_tokens=16_000, effort="high")
    code = llm.extract_code(res.text)
    if not code:
        ctx.log("Model did not return a code block; skipping execution.", "warning")
        return None
    result = None
    for attempt in range(2):
        (workdir / "experiment.py").write_text(code)
        ctx.save_artifact("code", "experiment.py", code)
        ctx.log(f"Executing experiment.py ({sandbox.backend_name()} sandbox, attempt {attempt + 1})…")
        result = await sandbox.execute(workdir, "experiment.py", runpod=runpod)
        _charge_exec(ctx, result)
        ctx.log(f"Execution {result.status} · exit {result.exit_code} · {result.seconds:.1f}s",
                "info" if result.ok else "warning")
        if result.ok or attempt == 1:
            break
        ctx.log("Repairing the script from the error output…", "warning")
        res = await ctx.ask("repair", system,
                            f"{prompt}\n\nYour previous script:\n```python\n{code}```\n\nIt failed with:\n```\n"
                            f"{result.stderr[-6000:]}\n```\nReturn a corrected complete script.",
                            max_tokens=16_000, effort="high")
        code = llm.extract_code(res.text) or code
    return result


def _charge_exec(ctx: RunContext, result: sandbox.ExecResult) -> None:
    if result.cost_usd:
        ctx.charge(result.cost_usd, f"execution · {result.seconds:.1f}s")


def _trim(value: Any, limit: int = 3000) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + "…"


def _collect_outputs(ctx: RunContext, workdir: Path, result: sandbox.ExecResult) -> Any:
    results_json: Any = None
    rj = workdir / "results.json"
    if rj.is_file():
        try:
            results_json = json.loads(rj.read_text())
        except json.JSONDecodeError:
            results_json = rj.read_text()[:20_000]
    for name, path in result.outputs.items():
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
            ctx.save_artifact("figure", Path(name).name, path.read_bytes())
        elif name == "results.json":
            ctx.save_artifact("data", "results.json", path.read_bytes())
    ctx.save_artifact("log", "stdout.txt", result.stdout or "(no output)")
    if result.stderr:
        ctx.save_artifact("log", "stderr.txt", result.stderr)
    ctx.save_analysis(execution={
        "status": result.status, "exit_code": result.exit_code, "stdout": result.stdout[-20_000:],
        "stderr": result.stderr[-10_000:], "cost": round(result.cost_usd, 4),
        "duration_ms": int(result.seconds * 1000),
        **({"results": results_json} if results_json is not None else {}),
    })
    return results_json


async def _figure(ctx: RunContext, gemini: tuple[str, str], brief_md: str, plan: str) -> None:
    if ctx.spent + llm.IMAGE_PRICE_USD > ctx.budget:
        ctx.log("Skipping figure: run budget reached.", "warning")
        return
    ctx.log("Generating a schematic figure with Gemini…")
    prompt = ("Create a clean, publication-style schematic diagram (white background, labelled boxes and arrows, "
              "no photorealism, no fake data plots) illustrating the method of this study:\n\n" + plan[:3000])
    try:
        data, mime = await llm.gemini_image(gemini[0], gemini[1], prompt)
    except llm.LLMError as exc:
        ctx.log(f"Figure generation failed: {exc}", "warning")
        return
    ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
    ctx.save_artifact("figure", f"method_schematic.{ext}", data, mime)
    ctx.charge(llm.IMAGE_PRICE_USD, "figure · gemini")


def _figure_names(run_id: int) -> list[str]:
    with session_scope() as db:
        return list(db.scalars(select(Artifact.name).where(Artifact.run_id == run_id, Artifact.kind == "figure")))


def _evidence(result: sandbox.ExecResult | None, results_json: Any, notes: list[str], figures: list[str]) -> str:
    parts = []
    if result is None:
        parts.append("EXECUTION RECORD: no code was executed in this run.")
    else:
        parts.append(f"EXECUTION RECORD: status={result.status}, exit_code={result.exit_code}, "
                     f"runtime={result.seconds:.1f}s\nstdout:\n```\n{result.stdout[-12_000:]}\n```")
        if result.stderr.strip():
            parts.append(f"stderr (tail):\n```\n{result.stderr[-3000:]}\n```")
        if results_json is not None:
            parts.append("results.json:\n```json\n" + json.dumps(results_json, indent=2, default=str)[:12_000] + "\n```")
    if notes:
        parts.append("Agent notes:\n" + "\n".join(f"- {n}" for n in notes if n))
    parts.append("Figures available: " + (", ".join(figures) if figures else "none"))
    return "\n\n".join(parts)


async def _review(ctx: RunContext, brief_md: str, draft: str, evidence: str) -> None:
    with session_scope() as db:
        db.query(ReviewFinding).filter(ReviewFinding.run_id == ctx.run_id).delete()
    prompt = f"Brief:\n{brief_md}\n\n{evidence}\n\nPAPER:\n{draft}"

    async def one(category: str, role: str) -> None:
        res = await ctx.ask(f"review:{category}", REVIEW_SYSTEM.format(role=role), prompt, max_tokens=8000,
                            effort="medium")
        data = llm.extract_json(res.text) or {}
        severity = str(data.get("severity", "medium")).lower()
        severity = severity if severity in {"low", "medium", "high"} else "medium"
        body = str(data.get("review_markdown") or res.text).strip()
        score = data.get("score")
        if isinstance(score, (int, float)) and not re.search(r"score[^0-9]{0,12}\d+\s*/\s*10", body, re.I):
            body = f"**Score: {int(score)}/10**\n\n{body}"
        with session_scope() as db:
            db.add(ReviewFinding(run_id=ctx.run_id, project_id=ctx.project_id, category=category, severity=severity,
                                 finding=body, suggested_fix=str(data.get("suggested_fix", ""))))
        ctx.log(f"  {category} reviewer: {severity}" + (f" · {score}/10" if score is not None else ""))

    # guard() runs per call; check once up front for the whole panel too.
    ctx.guard("review")
    results = await asyncio.gather(*(one(c, r) for c, r in REVIEWERS.items()), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    for err in errors:
        ctx.log(f"  reviewer failed: {err}", "warning")
    if errors and len(errors) == len(REVIEWERS):
        raise errors[0]


# ---- groundedness gate ---------------------------------------------------------

PLACEHOLDERS = re.compile(
    r"\b(TODO|TBD|FIXME|lorem ipsum)\b|\[citation needed\]|\[(?:insert|add)[^\]]*\]|<insert[^>]*>|\bXX+(?:\.X+)?%?\b|\?\?\?",
    re.I,
)
URL = re.compile(r"https?://[^\s)\]>\"'`]+")
CITE = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\]")


def _numbers_from_cites(marker: str) -> set[int]:
    out: set[int] = set()
    for part in re.split(r"\s*,\s*", marker):
        if re.search(r"[–-]", part):
            a, b = re.split(r"\s*[–-]\s*", part)[:2]
            if a.isdigit() and b.isdigit() and int(b) - int(a) < 50:
                out.update(range(int(a), int(b) + 1))
        elif part.strip().isdigit():
            out.add(int(part))
    return out


async def groundedness(draft: str, executed: bool, results_text: str) -> dict[str, Any]:
    placeholders = sorted({m.group(0) for m in PLACEHOLDERS.finditer(draft)})

    # References: numbered entries under a References heading.
    refs_match = re.search(r"^#+\s*References\s*$(.*)", draft, re.I | re.M | re.S)
    body = draft[: refs_match.start()] if refs_match else draft
    ref_numbers: set[int] = set()
    if refs_match:
        for line in refs_match.group(1).splitlines():
            m = re.match(r"\s*(?:\[(\d+)\]|(\d+)\.)\s+", line)
            if m:
                ref_numbers.add(int(m.group(1) or m.group(2)))
    cited: set[int] = set()
    for m in CITE.finditer(body):
        cited |= _numbers_from_cites(m.group(1))
    fabrications = [f"citation [{n}] has no matching reference entry" for n in sorted(cited - ref_numbers)]

    # Quantitative claims in Results when nothing ran, or numbers absent from the output.
    results_sec = re.search(r"^#+\s*Results\s*$(.*?)(?=^#+\s)", draft, re.I | re.M | re.S)
    if results_sec:
        nums = set(re.findall(r"(?<![\w.])\d+\.\d+%?|(?<![\w.])\d{2,}%", results_sec.group(1)))
        if not executed and nums:
            fabrications.append(f"Results section reports numbers ({', '.join(sorted(nums)[:6])}) but no code was executed")
        elif executed:
            haystack = results_text.replace(" ", "")
            missing = []
            for n in sorted(nums):
                core = n.rstrip("%")
                # accept rounding: compare with up to the given number of decimals
                if core not in haystack and not _rounded_in(core, haystack):
                    missing.append(n)
            if missing:
                fabrications.append(
                    f"numbers in Results not found in the execution output: {', '.join(missing[:8])}"
                )

    urls = sorted({u.rstrip(".,;:") for u in URL.findall(draft)})
    dead: list[str] = []
    if settings.check_urls and urls:
        dead = await _dead_urls(urls[:40])

    issues = len(placeholders) + len(fabrications) + len(dead)
    denominator = max(10, len(urls) + len(cited) + 5)
    score = max(0.0, 1.0 - issues / denominator)
    return {"score": round(score, 4), "dead_urls": dead, "placeholders": placeholders,
            "fabrications": fabrications, "checked_urls": len(urls), "citations": len(cited)}


def _rounded_in(core: str, haystack: str) -> bool:
    if "." not in core:
        return False
    decimals = len(core.split(".")[1])
    for m in re.finditer(r"-?\d+\.\d+(?:e-?\d+)?", haystack):
        try:
            if f"{float(m.group(0)):.{decimals}f}" == core or f"{float(m.group(0)) * 100:.{decimals}f}" == core:
                return True
        except ValueError:
            continue
    return False


async def _dead_urls(urls: list[str]) -> list[str]:
    sem = asyncio.Semaphore(8)

    async def probe(client: httpx.AsyncClient, url: str) -> str | None:
        async with sem:
            try:
                r = await client.head(url)
                if r.status_code in (403, 405, 429) or r.status_code >= 500:
                    r = await client.get(url)
                # 401/403/429 mean "exists but guarded", not "dead".
                return url if r.status_code in (404, 410) or r.status_code >= 500 else None
            except httpx.HTTPError:
                return url

    async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                 headers={"User-Agent": "AgentLab-groundedness/1.0"}) as client:
        found = await asyncio.gather(*(probe(client, u) for u in urls))
    return [u for u in found if u]


def results_text_for(run: Run) -> str:
    ex = (run.analysis_json or {}).get("execution") or {}
    return f"{ex.get('stdout', '')}\n{json.dumps(ex.get('results', ''), default=str)}"


# ---- revisions ----------------------------------------------------------------

REVISION_SYSTEM = """You revise one passage of a research paper. Apply the operator's instruction to the \
passage only, keep its Markdown formatting and voice, and do not introduce new numbers or citations that are not \
already in the paper. Reply with the replacement passage only — no preamble, no code fence."""


def estimate_revision(model: str, draft: str, excerpt: str) -> float:
    tin = (min(len(draft), 24_000) + len(excerpt) + 1_500) // 3
    tout = max(400, int(len(excerpt) / 3 * 1.5)) + 2_000  # + thinking headroom
    return round(llm.cost_of(model, tin, tout), 4)


def revision_context(draft: str, excerpt: str) -> str:
    i = draft.find(excerpt)
    if i == -1:
        return draft[:24_000]
    return draft[max(0, i - 6000): i + len(excerpt) + 6000]


async def draft_revision(client: llm.TextClient, draft: str, excerpt: str, instruction: str) -> llm.LLMResult:
    prompt = (f"Surrounding paper text for context:\n<<<\n{revision_context(draft, excerpt)}\n>>>\n\n"
              f"Passage to revise:\n<<<\n{excerpt}\n>>>\n\nInstruction: {instruction}")
    return await client.complete(REVISION_SYSTEM, prompt, max_tokens=8000, effort="medium")


def copy_uploads_to(workdir: Path, project_id: int) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    sandbox.prepare_workdir(workdir, list_uploads(project_id), {})
