"""The super agent: the "agentic" orchestration mode of a research run.

The design borrows from open agent harnesses such as DeepSeek Harness (where
models, tools and skills are all plugins) and DeerFlow (a "SuperAgent
harness" with a lead agent, a middleware chain, parallel sub-agents,
sandboxes, on-demand skills and persistent memory):

* Plugin tools. Every capability is a `Tool` registered in `TOOLS`; an agent
  is given a subset by name. Adding a tool is one decorated class.
* Middleware chain. Each tool call passes through budget, logging and
  output-trimming middleware before and after it runs (`MIDDLEWARE`).
* Lead agent + sub-agents. The lead plans and may `delegate` up to
  MAX_PARALLEL focused tasks. Each sub-agent runs concurrently in its own
  folder of the sandbox with its own step budget and a wall-clock timeout,
  and returns a structured report to the lead.
* Skills. Markdown workflow modules in backend/skills/. Agents see only each
  skill's name and one-line description, and `load_skill` pulls the full
  text when the task needs it, which keeps prompts small.
* Memory. `remember` stores facts against the project; later runs on the
  same project start with them. `recall` searches them.

The protocol is model-agnostic (one JSON action per turn), so it runs on
Claude, OpenAI or DeepSeek. Use `deepseek-reasoner` to have DeepSeek drive
it end to end.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from sqlalchemy import or_, select

from . import llm, sandbox
from .config import settings
from .db import Memory, session_scope, utcnow

if TYPE_CHECKING:  # pipeline imports this module; avoid the cycle at runtime
    from .pipeline import RunContext

LEAD_STEPS = 8
SUB_STEPS = 6
MAX_PARALLEL = 3
MAX_DELEGATIONS = 2
SUB_TIMEOUT_S = 15 * 60
SKILLS_DIR = Path(__file__).resolve().parent / "skills"
SUB_ROLES = {
    "coder": "writes and runs experiment code, debugging until it works",
    "analyst": "analyses data or results statistically and produces figures",
    "researcher": "reasons about method, prior knowledge, threats to validity and interpretation (no code)",
}


# ---- skills -------------------------------------------------------------------


def _skills() -> dict[str, dict[str, str]]:
    out = {}
    for p in sorted(SKILLS_DIR.glob("*.md")):
        text = p.read_text()
        meta = dict(re.findall(r"^(name|description):\s*(.+)$", text.split("---", 2)[1] if text.startswith("---") else "", re.M))
        name = meta.get("name", p.stem)
        out[name] = {"description": meta.get("description", ""), "body": text.split("---", 2)[-1].strip()}
    return out


SKILLS = _skills()


# ---- agents & tools -------------------------------------------------------------


@dataclass
class Agent:
    name: str
    role: str
    goal: str
    tools: list[str]
    max_steps: int
    home: str = ""  # sub-folder of the sandbox this agent may write in ("" = anywhere)
    notes: list[str] = field(default_factory=list)
    report: str = ""
    delegations: int = 0
    finished: bool = False


@dataclass
class Session:
    """Shared state for one run's agents."""

    ctx: "RunContext"
    workdir: Path
    can_exec: bool
    runpod: tuple[str, str] | None
    last_exec: sandbox.ExecResult | None = None
    step: int = 0
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_PARALLEL))


class Tool:
    name = ""
    description = ""
    params = "{}"

    async def run(self, s: Session, agent: Agent, inp: dict) -> Any:  # pragma: no cover - interface
        raise NotImplementedError


TOOLS: dict[str, Tool] = {}


def tool(cls: type[Tool]) -> type[Tool]:
    TOOLS[cls.name] = cls()
    return cls


def _resolve(s: Session, path: str) -> Path | None:
    p = (s.workdir / str(path or "")).resolve()
    root = s.workdir.resolve()
    return p if p == root or root in p.parents else None


@tool
class ListFiles(Tool):
    name, description, params = "list_files", "list files in the sandbox", "{}"

    async def run(self, s, agent, inp):
        return [f"{p.relative_to(s.workdir)} ({p.stat().st_size} B)"
                for p in sorted(s.workdir.rglob("*")) if p.is_file()][:200]


@tool
class ReadFile(Tool):
    name, description, params = "read_file", "read up to 8 KB of a file", '{"path": str}'

    async def run(self, s, agent, inp):
        p = _resolve(s, inp.get("path", ""))
        if not p or not p.is_file():
            return "error: no such file"
        return p.read_text(errors="replace")[:8000]


@tool
class WriteFile(Tool):
    name, description = "write_file", "create or overwrite a file (sub-agents: inside your own folder)"
    params = '{"path": str, "content": str}'

    async def run(self, s, agent, inp):
        path = str(inp.get("path", ""))
        if agent.home and not path.startswith(agent.home + "/"):
            path = f"{agent.home}/{Path(path).name}"
        p = _resolve(s, path)
        if not p:
            return "error: path must stay inside the sandbox"
        p.parent.mkdir(parents=True, exist_ok=True)
        content = str(inp.get("content", ""))
        p.write_text(content)
        if p.suffix == ".py":
            s.ctx.save_artifact("code", p.relative_to(s.workdir).as_posix().replace("/", "__"), content)
        return f"wrote {len(content)} chars to {p.relative_to(s.workdir)}"


@tool
class RunPython(Tool):
    name, description = "run_python", f"execute a Python script in the sandbox (timeout {settings.exec_timeout_s}s)"
    params = '{"path": str}'

    async def run(self, s, agent, inp):
        if not s.can_exec:
            return "error: code execution is disabled for this run"
        p = _resolve(s, inp.get("path", ""))
        if not p or not p.is_file():
            return "error: write the script first"
        # Scripts run from the sandbox root, so uploads are at ./<name> for every agent.
        result = await sandbox.execute(s.workdir, p.relative_to(s.workdir).as_posix(), runpod=s.runpod)
        if result.cost_usd:
            s.ctx.charge(result.cost_usd, f"execution · {result.seconds:.1f}s")
        if result.ok or s.last_exec is None:
            s.last_exec = result
        s.ctx.log(f"    {agent.name} ran {p.name}: {result.status} · exit {result.exit_code} · {result.seconds:.1f}s",
                  "info" if result.ok else "warning")
        return {"status": result.status, "exit_code": result.exit_code, "stdout": result.stdout[-5000:],
                "stderr": result.stderr[-3000:], "new_files": sorted(result.outputs)[:30]}


@tool
class Note(Tool):
    name, description, params = "note", "record an observation for the paper", '{"text": str}'

    async def run(self, s, agent, inp):
        agent.notes.append(str(inp.get("text", ""))[:2000])
        return "noted"


@tool
class LoadSkill(Tool):
    name, description, params = "load_skill", "load the full text of a skill playbook", '{"name": str}'

    async def run(self, s, agent, inp):
        sk = SKILLS.get(str(inp.get("name", "")))
        return sk["body"] if sk else f"error: unknown skill; available: {', '.join(SKILLS)}"


@tool
class Remember(Tool):
    name, description = "remember", "persist a fact for future runs on this project (kind: fact|pitfall|result|preference)"
    params = '{"text": str, "kind": str}'

    async def run(self, s, agent, inp):
        text = str(inp.get("text", "")).strip()[:1000]
        if not text:
            return "error: empty"
        kind = str(inp.get("kind", "fact"))
        with session_scope() as db:
            db.add(Memory(user_id=s.ctx.user_id, project_id=s.ctx.project_id, run_id=s.ctx.run_id,
                          kind=kind if kind in {"fact", "pitfall", "result", "preference"} else "fact", text=text))
        return "remembered"


@tool
class Recall(Tool):
    name, description, params = "recall", "search memories from earlier runs", '{"query": str}'

    async def run(self, s, agent, inp):
        words = [w for w in re.findall(r"\w{3,}", str(inp.get("query", "")).lower())][:6]
        with session_scope() as db:
            q = select(Memory).where(Memory.user_id == s.ctx.user_id,
                                     or_(Memory.project_id == s.ctx.project_id, Memory.project_id.is_(None)))
            if words:
                q = q.where(or_(*[Memory.text.ilike(f"%{w}%") for w in words]))
            rows = db.scalars(q.order_by(Memory.id.desc()).limit(15)).all()
            return [f"[{m.kind}] {m.text}" for m in rows] or "no matching memories"


@tool
class Delegate(Tool):
    name = "delegate"
    description = (f"run up to {MAX_PARALLEL} sub-agents in parallel; each gets one focused goal and returns a report. "
                   f"roles: {', '.join(f'{k} ({v})' for k, v in SUB_ROLES.items())}")
    params = '{"tasks": [{"role": str, "goal": str}]}'

    async def run(self, s, agent, inp):
        if agent.delegations >= MAX_DELEGATIONS:
            return f"error: delegation limit ({MAX_DELEGATIONS}) reached — finish with what you have"
        tasks = [t for t in (inp.get("tasks") or []) if isinstance(t, dict) and t.get("goal")][:MAX_PARALLEL]
        if not tasks:
            return "error: give 1-3 tasks, each {role, goal}"
        agent.delegations += 1
        subs = []
        for i, t in enumerate(tasks, 1):
            role = t.get("role") if t.get("role") in SUB_ROLES else "researcher"
            tools = ["list_files", "read_file", "write_file", "note", "load_skill", "recall", "finish"]
            if role != "researcher":
                tools.insert(3, "run_python")
            name = f"{role}-{agent.delegations}.{i}"
            subs.append(Agent(name=name, role=role, goal=str(t["goal"])[:2000], tools=tools,
                              max_steps=SUB_STEPS, home=f"agents/{name}"))
        s.ctx.log(f"  lead delegated {len(subs)} task(s): " + "; ".join(f"{a.name}: {a.goal[:80]}" for a in subs))

        async def bounded(sub: Agent) -> dict:
            async with s.sem:
                try:
                    await asyncio.wait_for(run_agent(s, sub, context=agent.goal), timeout=SUB_TIMEOUT_S)
                except asyncio.TimeoutError:
                    sub.report = sub.report or f"(timed out after {SUB_TIMEOUT_S // 60} min)"
                return {"agent": sub.name, "role": sub.role, "goal": sub.goal, "finished": sub.finished,
                        "report": sub.report, "notes": sub.notes}

        results = await asyncio.gather(*(bounded(a) for a in subs), return_exceptions=True)
        reports = []
        for sub, res in zip(subs, results):
            if isinstance(res, BaseException):
                if isinstance(res, (asyncio.CancelledError, llm.LLMError)) or type(res).__name__ == "BudgetExceeded":
                    raise res
                reports.append({"agent": sub.name, "error": f"{type(res).__name__}: {res}"})
            else:
                reports.append(res)
                agent.notes += [f"[{sub.name}] {n}" for n in res["notes"]]
                if res["report"]:
                    agent.notes.append(f"[{sub.name} report] {res['report'][:2000]}")
        return reports


@tool
class Finish(Tool):
    name, description = "finish", "end your work with a factual report of what you did and found"
    params = '{"summary": str}'

    async def run(self, s, agent, inp):
        agent.report = str(inp.get("summary", ""))[:6000]
        agent.finished = True
        return "finished"


# ---- middleware -----------------------------------------------------------------
# Each middleware is (before, after). `before` may return a value to short-circuit
# the tool; `after` may transform the result.

Before = Callable[[Session, Agent, str, dict], Awaitable[Any]]
After = Callable[[Session, Agent, str, dict, Any], Awaitable[Any]]


async def permission_mw(s: Session, agent: Agent, name: str, inp: dict) -> Any:
    if name not in agent.tools or name not in TOOLS:
        return f"error: tool '{name}' is not available to {agent.name}; you have: {', '.join(agent.tools)}"
    return None


async def budget_mw(s: Session, agent: Agent, name: str, inp: dict) -> Any:
    if name in {"run_python", "delegate"} and s.ctx.spent >= s.ctx.budget * 0.9:
        return "error: run budget nearly exhausted — finish now with what you have"
    return None


async def trim_mw(s: Session, agent: Agent, name: str, inp: dict, out: Any) -> Any:
    text = out if isinstance(out, str) else json.dumps(out, default=str)
    return out if len(text) <= 12_000 else text[:12_000] + "…[trimmed]"


MIDDLEWARE: list[tuple[Before | None, After | None]] = [(permission_mw, None), (budget_mw, None), (None, trim_mw)]


async def call_tool(s: Session, agent: Agent, name: str, inp: dict) -> Any:
    for before, _ in MIDDLEWARE:
        if before:
            short = await before(s, agent, name, inp)
            if short is not None:
                return short
    out = await TOOLS[name].run(s, agent, inp)
    for _, after in MIDDLEWARE:
        if after:
            out = await after(s, agent, name, inp, out)
    return out


# ---- the agent loop ----------------------------------------------------------------

SYSTEM = """You are {name}, a {role_desc} inside an autonomous research harness. You work in a sandboxed \
directory{home_note}. Act by replying with exactly ONE JSON object per turn in a ```json fence:

{{"tool": "<name>", "input": {{...}}, "reason": "<one sentence>"}}

Tools available to you:
{tools}

Skills (call load_skill to read one before relying on it):
{skills}

Python rules: standard library plus any installed numpy/pandas/scipy/scikit-learn/matplotlib (guard optional \
imports), no network, fixed seeds, uploaded files are in the sandbox root (open them as ./<name>), write numbers \
to results.json and figures as PNG. Never claim a result you did not compute. You have at most {steps} steps; \
call finish before you run out."""


def _system(agent: Agent, can_exec: bool) -> str:
    tool_lines = []
    for n in agent.tools:
        t = TOOLS[n]
        note = " (DISABLED for this run)" if n == "run_python" and not can_exec else ""
        tool_lines.append(f"- {n} {t.params} — {t.description}{note}")
    role_desc = ("lead research agent who plans the investigation, delegates focused work to sub-agents, checks "
                 "their reports, and synthesises the findings" if agent.role == "lead" else
                 f"{agent.role} sub-agent who {SUB_ROLES[agent.role]}")
    return SYSTEM.format(
        name=agent.name, role_desc=role_desc, steps=agent.max_steps,
        home_note=f"; write only inside {agent.home}/" if agent.home else "",
        tools="\n".join(tool_lines),
        skills="\n".join(f"- {k}: {v['description']}" for k, v in SKILLS.items()) or "(none)",
    )


async def run_agent(s: Session, agent: Agent, context: str) -> None:
    system = _system(agent, s.can_exec)
    transcript = [f"Your goal:\n{agent.goal}\n\nContext:\n{context}"]
    for i in range(1, agent.max_steps + 1):
        prompt = "\n\n".join(transcript[:1] + transcript[1:][-12:]) + \
            f"\n\nStep {i} of {agent.max_steps}. Reply with one JSON action."
        res = await s.ctx.ask(f"agent:{agent.name}", system, prompt, max_tokens=12_000, effort="medium")
        action = llm.extract_json(res.text) or {}
        name = str(action.get("tool", "")).strip()
        inp = action.get("input") if isinstance(action.get("input"), dict) else {}
        reason = str(action.get("reason", ""))[:500]
        t0 = time.monotonic()
        if not name:
            out: Any = "error: could not parse a JSON action; reply with exactly one ```json object"
            s.ctx.log(f"  {agent.name} step {i}: unparseable action", "warning")
        else:
            s.ctx.log(f"  {agent.name} step {i}: {name} — {reason}")
            out = await call_tool(s, agent, name, inp)
        s.step += 1
        s.ctx.actions.append({"step": s.step, "tool": f"{agent.name}:{name or 'invalid'}", "action": name,
                              "input": _clip(inp), "output": _clip(out), "reason": reason,
                              "duration_ms": int((time.monotonic() - t0) * 1000), "ts": utcnow().isoformat()})
        s.ctx.save_analysis(actions=s.ctx.actions)
        transcript.append(f"Action {i}: {json.dumps({'tool': name, 'input': _clip(inp, 1500)}, default=str)}\n"
                          f"Result:\n{_clip(out, 5000) if isinstance(_clip(out, 5000), str) else json.dumps(_clip(out, 5000), default=str)}")
        if agent.finished:
            return
    agent.report = agent.report or "(step budget exhausted before finish)"
    s.ctx.log(f"  {agent.name} used its step budget without finishing", "warning")


def _clip(value: Any, limit: int = 3000) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return value if len(text) <= limit else text[:limit] + "…"


def memories_for(user_id: int, project_id: int) -> list[str]:
    with session_scope() as db:
        rows = db.scalars(select(Memory).where(Memory.user_id == user_id,
                                               or_(Memory.project_id == project_id, Memory.project_id.is_(None)))
                          .order_by(Memory.id.desc()).limit(20)).all()
        return [f"[{m.kind}] {m.text}" for m in reversed(rows)]


async def run_super_agent(ctx: "RunContext", brief_md: str, plan: str, uploads_desc: str, workdir: Path,
                          can_exec: bool, runpod: tuple[str, str] | None) -> tuple[sandbox.ExecResult | None, list[str]]:
    s = Session(ctx=ctx, workdir=workdir, can_exec=can_exec, runpod=runpod)
    memories = memories_for(ctx.user_id, ctx.project_id)
    lead = Agent(
        name="lead", role="lead", max_steps=LEAD_STEPS,
        goal=("Carry out the approved research brief as far as this run allows, then finish with a factual summary "
              "of methods, results (with numbers only from executed code) and limitations."),
        tools=["list_files", "read_file", "write_file", "run_python", "delegate", "note", "load_skill",
               "remember", "recall", "finish"],
    )
    context = (f"Approved brief:\n{brief_md}\n\nPlan:\n{plan}\n\nUploaded files:\n{uploads_desc}\n\n"
               f"Code execution: {'enabled' if can_exec else 'DISABLED'}.\n\n"
               "Memory from earlier runs on this project:\n" + ("\n".join(memories) if memories else "(none)") +
               "\n\nBefore finishing, use `remember` for anything a future run should know (pitfalls, results).")
    ctx.log(f"Super agent: lead + up to {MAX_PARALLEL} parallel sub-agents · {len(SKILLS)} skills · "
            f"{len(memories)} memories loaded")
    await run_agent(s, lead, context)
    notes = list(lead.notes)
    if lead.report:
        notes.append(lead.report)
    return s.last_exec, notes
