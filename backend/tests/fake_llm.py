"""A scripted stand-in for a model provider, keyed on each stage's system prompt.

It returns what a well-behaved model would, so the tests exercise the real
orchestration, sandbox, parsing, budget and gate logic end to end.
"""

from __future__ import annotations

import json
import re

from backend.llm import LLMResult

EXPERIMENT = '''import json, statistics
vals = [float(r.split(",")[1]) for r in open("data.csv").read().strip().splitlines()[1:]]
mean = statistics.fmean(vals)
print(f"n={len(vals)} mean={mean:.4f}")
json.dump({"n": len(vals), "mean": round(mean, 4)}, open("results.json", "w"))
'''

BASELINE = '''x = 1.0
score = -(x - 3.0) ** 2
print(f"SCORE: {score}")
'''


class FakeClient:
    provider = "fake"

    def __init__(self, model: str = "deepseek-reasoner", draft_extra: str = ""):
        self.model = model
        self.draft_extra = draft_extra
        self.calls: list[str] = []

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 8000, effort: str = "medium") -> LLMResult:
        text = self._reply(system, prompt)
        self.calls.append(system[:40])
        return LLMResult(text, 1000, 500, 0.001, self.model, self.provider, 5)

    def _reply(self, system: str, prompt: str) -> str:
        if "principal investigator" in system:
            return "# Mean estimation\n## Question\nWhat is the mean of column v?\n## Hypothesis\nAbout 2.5.\n" \
                   "## Background\nNone.\n## Method\nCompute it.\n## Data\ndata.csv\n## Success criteria\nA number.\n" \
                   "## Risks and limitations\nTiny sample.\n## Out of scope\nEverything else."
        if "planning stage" in system:
            return "## Approach\nRead data.csv.\n## Steps\n1. Compute mean.\n## Metrics\nmean\n## Expected outputs\nresults.json"
        if "experiment script" in system:
            return f"```python\n{EXPERIMENT}```"
        if "inside an autonomous research harness" in system:
            return self._agent(system, prompt)
        if "You write the research paper" in system:
            return ("# Mean of v\n\n## Abstract\nWe compute a mean.\n\n## Results\nThe mean was 2.5000 over 4 rows.\n\n"
                    f"## Discussion\nSmall sample.{self.draft_extra}\n\n## Conclusion\nDone.\n")
        if "Review the paper below" in system:
            return '```json\n{"severity": "low", "score": 7, "review_markdown": "## Summary\\nFine.\\n## Strengths\\n- clear\\n' \
                   '## Weaknesses\\n- tiny sample", "suggested_fix": "Use more data."}\n```'
        if "You revise one passage" in system:
            return "The sample is small (n=4), so the estimate is preliminary."
        if "You are the proposer" in system:
            m = re.search(r"^x = ([\d.]+)$", prompt, re.M)
            x = float(m.group(1)) if m else 1.0
            it = len([c for c in self.calls if c.startswith("You are the proposer")])
            if it == 2:  # one dishonest proposal, which the leakage scan must catch
                return "HYPOTHESIS: shortcut.\n```python\nprint(\"SCORE: 0.0\")\n```"
            if it == 3:  # one broken proposal
                return "HYPOTHESIS: typo.\n```python\nx = (\n```"
            return f"HYPOTHESIS: move x toward 3.\n```python\nx = {x + 0.5}\nscore = -(x - 3.0) ** 2\nprint(f\"SCORE: {{score}}\")\n```"
        if "baseline program" in system:
            return f"```python\n{BASELINE}```"
        raise AssertionError(f"unexpected prompt: {system[:80]}")

    def _agent(self, system: str, prompt: str) -> str:
        name = re.search(r"You are ([\w.-]+),", system).group(1)
        step = int(re.search(r"Step (\d+) of", prompt).group(1))
        act = lambda tool, inp, reason="": "```json\n" + json.dumps({"tool": tool, "input": inp, "reason": reason}) + "\n```"
        if name == "lead":
            script = [
                act("load_skill", {"name": "experiment-design"}, "follow the playbook"),
                act("delegate", {"tasks": [{"role": "coder", "goal": "compute the mean of v in data.csv"},
                                           {"role": "researcher", "goal": "list threats to validity"}]}, "parallelise"),
                act("remember", {"text": "data.csv has 4 rows; mean of v is 2.5", "kind": "result"}, "for next time"),
                act("finish", {"summary": "Computed the mean of v (2.5000) from data.csv."}, "done"),
            ]
        elif name.startswith("coder"):
            script = [
                act("write_file", {"path": "mean.py", "content": EXPERIMENT}, "write the script"),
                act("run_python", {"path": f"agents/{name}/mean.py"}, "run it"),
                act("finish", {"summary": "mean=2.5000 (n=4)"}, "report"),
            ]
        else:
            script = [act("note", {"text": "n=4 is too small for strong claims"}), act("finish", {"summary": "Main threat: n=4."})]
        return script[min(step, len(script)) - 1]
