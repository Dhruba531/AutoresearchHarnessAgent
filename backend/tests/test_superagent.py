"""The lead's `delegate` tool, driven directly with a scripted model.

test_e2e.py covers a whole agentic run through the HTTP API with the original
{role, goal} task form; this checks the richer task fields, step budgets and
report files without the database or the API.
"""

import asyncio
import json
import re
from types import SimpleNamespace

from backend import superagent as sa


def act(tool, inp):
    return "```json\n" + json.dumps({"tool": tool, "input": inp, "reason": ""}) + "\n```"


class ScriptedCtx:
    """Stands in for pipeline.RunContext: replies from a per-agent script and records each agent's prompts."""

    def __init__(self, scripts):
        self.scripts = scripts
        self.first_prompt: dict[str, str] = {}
        self.system: dict[str, str] = {}
        self.actions: list[dict] = []
        self.logs: list[str] = []
        self.spent, self.budget = 0.0, 10.0

    async def ask(self, stage, system, prompt, *, max_tokens=12_000, effort="medium"):
        name = stage.split(":", 1)[1]
        self.first_prompt.setdefault(name, prompt)
        self.system.setdefault(name, system)
        step = int(re.search(r"Step (\d+) of", prompt).group(1))
        script = self.scripts[name]
        return SimpleNamespace(text=script[min(step, len(script)) - 1])

    def log(self, message, level="info"):
        self.logs.append(message)

    def save_analysis(self, **fields):
        pass


def make_lead():
    return sa.Agent(name="lead", role="lead", goal="Carry out the brief.", tools=["delegate", "finish"],
                    max_steps=sa.LEAD_STEPS)


def test_delegate_passes_task_fields_and_saves_reports(tmp_path):
    long_report = "mean=2.5 over n=4. " + "More detail. " * 300  # longer than the summary the lead sees
    ctx = ScriptedCtx({
        "analyst-1.1": [act("finish", {"summary": long_report})],
        "researcher-1.2": [act("note", {"text": "n=4 is small"}), act("finish", {"summary": "Main threat: n=4."})],
        "researcher-1.3": [act("finish", {"summary": "Header is id,v."})],
    })
    s = sa.Session(ctx=ctx, workdir=tmp_path, can_exec=False, runpod=None)
    lead = make_lead()
    out = asyncio.run(sa.call_tool(s, lead, "delegate", {"tasks": [
        {"role": "analyst", "objective": "Compute the mean of v in data.csv.",
         "output_format": "results.json with key mean, and one line", "step_budget": 3,
         "boundaries": "Do not discuss validity; researcher-1.2 has it."},
        {"role": "researcher", "goal": "List threats to validity."},  # the original one-field form
        {"role": "bogus", "objective": "Check the CSV header.", "boundaries": ["no stats", "no plots"],
         "step_budget": 99},
    ]}))

    # every field reaches the sub-agent's first message, and the step budget its system prompt
    first = ctx.first_prompt["analyst-1.1"]
    assert "Objective: Compute the mean of v in data.csv." in first
    assert "Expected output: results.json with key mean, and one line" in first
    assert "Do not discuss validity; researcher-1.2 has it." in first
    assert "Step budget: 3 steps" in first and "at most 3 steps" in ctx.system["analyst-1.1"]
    # `goal` still works and means `objective`; missing fields get defaults
    first = ctx.first_prompt["researcher-1.2"]
    assert "Objective: List threats to validity." in first
    assert f"Expected output: {sa.DEFAULT_OUTPUT}" in first and "none given" in first
    assert f"Step budget: {sa.SUB_STEPS} steps" in first
    # unknown roles fall back to researcher, lists are joined, and budgets are capped at SUB_STEPS
    first = ctx.first_prompt["researcher-1.3"]
    assert "no stats; no plots" in first and f"Step budget: {sa.SUB_STEPS} steps" in first
    # only the lead is told how to delegate
    assert "How to delegate" in sa._system(lead, True)
    assert not any("How to delegate" in system for system in ctx.system.values())

    # each full report lands in agents/<name>/report.md
    analyst = (tmp_path / "agents" / "analyst-1.1" / "report.md").read_text()
    assert long_report.strip() in analyst and "Compute the mean of v" in analyst
    researcher = (tmp_path / "agents" / "researcher-1.2" / "report.md").read_text()
    assert "Status: finished · 2 of 6 steps" in researcher
    assert "Main threat: n=4." in researcher and "- n=4 is small" in researcher
    assert (tmp_path / "agents" / "researcher-1.3" / "report.md").is_file()

    # the lead gets a clipped summary and the path, small enough for its transcript to keep whole
    assert isinstance(out, str) and len(out) < 5000
    assert "[clipped; read_file agents/analyst-1.1/report.md for the rest]" in out
    assert long_report.strip() not in out
    assert "Full report: agents/researcher-1.2/report.md" in out and "Main threat: n=4." in out
    # the notes handed on to the paper are unchanged
    assert "[researcher-1.2] n=4 is small" in lead.notes
    assert any(n.startswith("[analyst-1.1 report] mean=2.5") for n in lead.notes)


def test_delegate_rejects_tasks_without_an_objective(tmp_path):
    s = sa.Session(ctx=ScriptedCtx({}), workdir=tmp_path, can_exec=False, runpod=None)
    lead = make_lead()
    out = asyncio.run(sa.call_tool(s, lead, "delegate", {"tasks": [{"role": "coder", "output_format": "a number"}]}))
    assert out.startswith("error:") and "objective" in out
    assert lead.delegations == 0  # a rejected call does not use up one of the MAX_DELEGATIONS


def test_step_budget_is_clamped():
    assert sa._step_budget(None) == sa.SUB_STEPS
    assert sa._step_budget("4") == 4
    assert sa._step_budget(1) == 2
    assert sa._step_budget(50) == sa.SUB_STEPS
    assert sa._step_budget("lots") == sa.SUB_STEPS
    assert sa._step_budget(True) == sa.SUB_STEPS
