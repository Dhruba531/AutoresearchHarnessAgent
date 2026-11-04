"""Autoresearch search campaigns: an LLM proposer hill-climbs a program's score.

The contract with the operator's code is deliberately small:

* The program under search is one Python file — the upload named in the
  campaign's `entrypoint`, else `train.py`, `main.py`, or the first .py/.ipynb
  upload. With no runnable upload, iteration 0 asks the model to write a
  baseline from the approved brief.
* The program prints its result as a line `SCORE: <float>` (or writes
  {"score": <float>} to results.json). Higher is better — print a negated
  loss if you are minimising.
* Every other uploaded file (data, an eval harness) is FROZEN. Files are
  hashed before and after each evaluation; any change marks the candidate as
  a metric-integrity failure.

Each iteration: pick an edit family (weighted, with an exploration rate) and
a parent (the current best, or a recent runner-up when exploring), ask the
model for one focused change, statically audit it, run it in the sandbox,
classify any failure, and keep it only if it beats the best score with a
clean audit. The loop stops on any budget cap, the iteration limit, a target
score, stagnation, too many invalid candidates, or an operator stop.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from . import llm, sandbox
from .config import settings
from .db import Brief, Campaign, Candidate, Project, User, session_scope, utcnow
from .events import hub
from .ledger import monthly_spend, record
from .pipeline import list_uploads

DEFAULT_FAMILIES = ["architecture", "optimizer", "data", "loss", "regularization", "schedule"]
FAMILY_HINTS = {
    "architecture": "change the model's structure or capacity (layers, width, features, model class)",
    "optimizer": "change the optimisation algorithm or its hyperparameters (learning rate, momentum, solver)",
    "data": "change preprocessing, feature engineering, augmentation or sampling of the training data",
    "loss": "change the objective being optimised (loss function, class weighting, label smoothing)",
    "regularization": "change regularisation (weight decay, dropout, early stopping, ensembling)",
    "schedule": "change the training schedule (epochs, warmup, decay, batch size, iteration counts)",
    "tooling": "make the program faster or more efficient without changing what it computes",
}

SCORE_LINE = re.compile(r"^\s*score\s*[:=]\s*(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|nan|-?inf)\s*$", re.I | re.M)

PROPOSER_SYSTEM = """You are the proposer in an automated research search. A Python program prints \
`SCORE: <float>`; higher is better. Each turn you make ONE focused, testable change to the parent program in \
the requested edit family, aiming to raise the score.

Hard rules — violations are detected automatically and the candidate is discarded:
- The score must come from a genuine evaluation. Never hard-code, inflate, or special-case the printed score.
- Never modify, rewrite, or delete any other file in the working directory; they are frozen and hashed.
- Never train or tune on held-out/test data. No network access, no package installs.
- Keep the runtime under {timeout} seconds on a CPU. Keep printing `SCORE: <float>` exactly once at the end.

Reply in exactly this format:
HYPOTHESIS: <one or two sentences: what you change and why it should help>
```python
<the complete new program>
```"""

BASELINE_SYSTEM = """You write the baseline program for an automated research search. It must be a single \
self-contained Python 3 file that implements a simple, honest first attempt at the task, evaluates it on a \
held-out split (or a fixed synthetic benchmark if no data is supplied), and prints exactly one final line \
`SCORE: <float>` where higher is better. Use only the standard library plus numpy/scikit-learn if installed \
(guard the imports and fall back to pure Python), fix all random seeds, and finish in under {timeout} seconds \
on a CPU. Reply with the program in one ```python fence."""


def campaign_dir(campaign_id: int) -> Path:
    return settings.data_dir / "campaigns" / str(campaign_id)


def _hash_files(files: dict[str, Path]) -> dict[str, str]:
    return {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in files.items() if p.is_file()}


def parse_score(stdout: str, workdir: Path) -> float | None:
    matches = SCORE_LINE.findall(stdout)
    if matches:
        try:
            return float(matches[-1])
        except ValueError:
            return None
    rj = workdir / "results.json"
    if rj.is_file():
        try:
            data = json.loads(rj.read_text())
            if isinstance(data, dict) and isinstance(data.get("score"), (int, float)):
                return float(data["score"])
        except (json.JSONDecodeError, OSError):
            pass
    return None


def parse_metrics(workdir: Path) -> dict[str, float | str]:
    rj = workdir / "results.json"
    out: dict[str, float | str] = {}
    if rj.is_file():
        try:
            data = json.loads(rj.read_text())
        except (json.JSONDecodeError, OSError):
            return out
        if isinstance(data, dict):
            for k, v in list(data.items())[:30]:
                if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                    out[str(k)] = v if not isinstance(v, str) else v[:80]
    return out


def leakage_scan(program: str, parent: str, frozen: list[str]) -> tuple[str, list[str]]:
    """Static checks on the lines this candidate added."""
    added = "\n".join(
        line[1:] for line in difflib.unified_diff(parent.splitlines(), program.splitlines(), lineterm="", n=0)
        if line.startswith("+") and not line.startswith("+++")
    )
    reasons = []
    if re.search(r"print\s*\(\s*f?[\"']\s*score\s*[:=]\s*-?\d", added, re.I):
        reasons.append("prints a hard-coded score literal")
    if re.search(r"score\s*=\s*(?:1(?:\.0+)?|0\.9{2,}\d*)\s*(?:#.*)?$", added, re.I | re.M):
        reasons.append("assigns a near-perfect constant to the score")
    for name in frozen:
        if re.search(rf"open\(\s*[rf]?[\"'][^\"']*{re.escape(name)}[\"']\s*,\s*[\"'][wa]", added):
            reasons.append(f"opens frozen file {name} for writing")
        if re.search(rf"(?:remove|unlink|rmtree|rename|replace)\([^)]*{re.escape(name)}", added):
            reasons.append(f"deletes or moves frozen file {name}")
    if re.search(r"\.fit\([^)]*\b(?:X_test|y_test|x_test|test_x|test_y)\b", added):
        reasons.append("fits on test-split variables")
    if re.search(r"^\s*(?:import|from)\s+(?:requests|urllib|socket|http\.client)\b", added, re.M):
        reasons.append("imports a network library")
    return ("flagged" if reasons else "clean"), reasons


def classify_failure(result: sandbox.ExecResult, score: float | None, hashes_ok: bool) -> str | None:
    err = result.stderr
    if result.status == "timeout":
        return "timeout"
    if result.status == "oom" or re.search(r"MemoryError|out of memory|Cannot allocate memory", err):
        return "oom"
    if not result.ok:
        if re.search(r"ModuleNotFoundError|ImportError", err):
            return "import"
        if re.search(r"SyntaxError|IndentationError", err):
            return "syntax"
        if re.search(r"shape|size mismatch|dimension|broadcast|must have the same length|matmul", err, re.I):
            return "shape"
        if re.search(r"\bnan\b", err, re.I):
            return "nan"
        return "runtime"
    if not hashes_ok:
        return "metric_integrity"
    if score is None:
        return "metric_integrity"
    if math.isnan(score) or math.isinf(score):
        return "nan"
    return None


class Search:
    def __init__(self, campaign_id: int):
        self.cid = campaign_id
        self.topic = f"campaign:{campaign_id}"
        self.started = time.monotonic()

    # -- plumbing --------------------------------------------------------
    def emit(self, **event: Any) -> None:
        event.setdefault("type", "search")
        event.setdefault("campaign_id", self.cid)
        event.setdefault("ts", utcnow().isoformat())
        hub.publish(self.topic, event)

    def update(self, **fields: Any) -> None:
        with session_scope() as db:
            c = db.get(Campaign, self.cid)
            for k, v in fields.items():
                setattr(c, k, v)

    def spend_add(self, usd: float = 0.0, calls: int = 0, gpu_hours: float = 0.0, detail: str = "") -> None:
        with session_scope() as db:
            c = db.get(Campaign, self.cid)
            s = dict(c.spend or {})
            s["usd"] = round(s.get("usd", 0.0) + usd, 6)
            s["model_calls"] = s.get("model_calls", 0) + calls
            s["gpu_hours"] = round(s.get("gpu_hours", 0.0) + gpu_hours, 6)
            s["wall_clock_minutes"] = round((time.monotonic() - self.started) / 60, 3)
            c.spend = s
            record(db, c.owner_id, usd, "campaign", self.cid, detail)

    # -- main loop -------------------------------------------------------
    async def run(self) -> None:
        with session_scope() as db:
            c = db.get(Campaign, self.cid)
            user = db.get(User, c.owner_id)
            project = db.get(Project, c.project_id)
            self.client = llm.text_client(db, user)
            self.runpod = llm.provider_credentials(db, user, "runpod") if sandbox.backend_name() == "runpod" else None
            self.config = dict(c.config or {})
            self.user_id = user.id
            brief = db.scalar(select(Brief).where(Brief.project_id == project.id, Brief.is_approved.is_(True))
                              .order_by(Brief.version.desc()).limit(1))
            self.task = (brief.content_markdown if brief else "") or project.idea_text or project.objective or project.title
            self.project_id = project.id

        self.update(status="running", started_at=utcnow())
        self.emit(type="status", status="running", message="campaign started")
        try:
            uploads = list_uploads(self.project_id)
            entry, baseline = self._find_entrypoint(uploads)
            self.entry = entry
            self.frozen = {n: p for n, p in uploads.items() if n != entry}
            self.frozen_hashes = _hash_files(self.frozen)
            if baseline is None:
                self.emit(phase="hypothesize", iteration=0, message="no runnable upload — writing a baseline program")
                baseline = await self._write_baseline()
            await self._evaluate_baseline(baseline)
            await self._loop()
        except asyncio.CancelledError:
            self.update(status="stopped", ended_at=utcnow())
            self.emit(type="status", status="stopped", message="campaign cancelled")
            raise
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.update(status="failed", ended_at=utcnow(), error=msg)
            self.emit(type="status", status="failed", level="error", message=msg)

    def _find_entrypoint(self, uploads: dict[str, Path]) -> tuple[str, str | None]:
        wanted = self.config.get("entrypoint")
        order = [wanted] if wanted else []
        order += ["train.py", "main.py"]
        order += [n for n in uploads if n.endswith(".py") and n not in {"eval.py", "prepare.py", "evaluate.py"}]
        order += [n for n in uploads if n.endswith(".ipynb")]
        for name in order:
            if name and name in uploads:
                raw = uploads[name].read_text(errors="replace")
                if name.endswith(".ipynb"):
                    return name[:-6] + ".py", sandbox.notebook_to_script(raw)
                return name, raw
        return "program.py", None

    async def _write_baseline(self) -> str:
        res = await self.client.complete(
            BASELINE_SYSTEM.format(timeout=settings.exec_timeout_s),
            f"Task:\n{self.task}\n\nFiles available in the working directory: "
            f"{', '.join(self.frozen) or 'none'}",
            max_tokens=12_000, effort="high",
        )
        self.spend_add(res.cost_usd, 1, detail=f"baseline · {res.model}")
        code = llm.extract_code(res.text)
        if not code:
            raise RuntimeError("The model did not return a baseline program.")
        return code

    async def _evaluate_baseline(self, program: str) -> None:
        cand = self._new_candidate(0, "baseline", None, program, "")
        self.emit(phase="evaluate", iteration=0, candidate_id=cand, edit_family="baseline", message="evaluating baseline")
        steps = [{"step": 1, "phase": "inspect", "tool": "baseline", "model_output": f"entrypoint {self.entry}",
                  "parser_status": "ok", "ts": utcnow().isoformat()}]
        await self._run_candidate(cand, program, program, steps, {"hypothesis": "Baseline program as supplied."}, 0, 0.0)

    def _new_candidate(self, iteration: int, family: str, parent: int | None, program: str, diff: str) -> int:
        with session_scope() as db:
            c = Candidate(campaign_id=self.cid, parent_id=parent, iteration=iteration, edit_family=family,
                          status="running", program=program, diff=diff)
            db.add(c)
            db.flush()
            c.trace_dir = str(campaign_dir(self.cid) / f"c{c.id}")
            return c.id

    async def _loop(self) -> None:
        cfg = self.config
        budget = cfg.get("budget") or {}
        max_iters = int(cfg.get("max_iters") or budget.get("max_iters") or 20)
        stopping = cfg.get("stopping") or {}
        stagnation_limit = int(stopping.get("stagnation_iters") or 0)
        target = stopping.get("target_score")
        max_invalid = stopping.get("max_invalid_rate")
        iteration = 0
        reason = "iteration limit reached"
        while True:
            with session_scope() as db:
                c = db.get(Campaign, self.cid)
                spend, metrics, best = dict(c.spend or {}), dict(c.metrics or {}), c.best_score
                stop = c.stop_requested
                monthly = monthly_spend(db, self.user_id)
            if stop:
                reason = "stopped by operator"
                break
            if iteration >= max_iters:
                break
            if budget.get("usd") and spend.get("usd", 0) >= budget["usd"]:
                reason = f"USD budget ${budget['usd']:.2f} reached"
                break
            if monthly >= settings.monthly_cap_usd:
                reason = "monthly spend cap reached"
                break
            if budget.get("model_calls") and spend.get("model_calls", 0) >= budget["model_calls"]:
                reason = "model-call budget reached"
                break
            if budget.get("wall_clock_minutes") and (time.monotonic() - self.started) / 60 >= budget["wall_clock_minutes"]:
                reason = "wall-clock budget reached"
                break
            if budget.get("gpu_hours") and sandbox.backend_name() == "runpod" and spend.get("gpu_hours", 0) >= budget["gpu_hours"]:
                reason = "GPU-hour budget reached"
                break
            if target is not None and best is not None and best >= float(target):
                reason = f"target score {target} reached"
                break
            if stagnation_limit and metrics.get("stagnation_counter", 0) >= stagnation_limit:
                reason = f"no improvement in {stagnation_limit} iterations"
                break
            if max_invalid and metrics.get("evaluated", 0) >= 5 and metrics.get("invalid_rate", 0) > float(max_invalid):
                reason = f"invalid rate above {float(max_invalid):.0%}"
                break
            iteration += 1
            self.update(iteration=iteration)
            await self._iterate(iteration)

        with session_scope() as db:
            c = db.get(Campaign, self.cid)
            final = "stopped" if c.stop_requested else ("completed" if c.best_score is not None else "failed")
            c.status, c.ended_at = final, utcnow()
            if final == "failed":
                c.error = "No candidate produced a valid score."
        self.emit(type="status", status=final, message=f"campaign finished: {reason}")

    def _pick(self, iteration: int) -> tuple[str, Candidate]:
        rng = random.Random(self.cid * 100_003 + iteration)
        weights = {k: float(v) for k, v in (self.config.get("edit_families") or {}).items() if float(v) > 0}
        if not weights:
            weights = {f: 1.0 for f in DEFAULT_FAMILIES}
        explore = float(self.config.get("explore", 0.3))
        with session_scope() as db:
            cands = list(db.scalars(select(Candidate).where(Candidate.campaign_id == self.cid)))
            c = db.get(Campaign, self.cid)
            best = db.get(Candidate, c.best_candidate_id)
        stats = {f: [0, 0] for f in weights}
        for cand in cands:
            if cand.edit_family in stats and cand.status in {"kept", "discarded", "invalid"}:
                stats[cand.edit_family][0] += 1
                stats[cand.edit_family][1] += cand.status == "kept"
        exploring = rng.random() < explore
        if exploring:
            family = rng.choice(sorted(weights))
            runners = sorted((x for x in cands if x.score is not None and x.status in {"kept", "discarded"}
                              and x.id != best.id), key=lambda x: -(x.score or 0))[:3]
            parent = rng.choice(runners) if runners and rng.random() < 0.3 else best
        else:
            # Thompson-flavoured: prior weight × (hits+1)/(tries+2)
            scores = {f: w * (stats[f][1] + 1) / (stats[f][0] + 2) for f, w in weights.items()}
            total = sum(scores.values())
            r, acc, family = rng.random() * total, 0.0, sorted(scores)[0]
            for f in sorted(scores):
                acc += scores[f]
                if r <= acc:
                    family = f
                    break
            parent = best
        return family, parent

    async def _iterate(self, iteration: int) -> None:
        family, parent = self._pick(iteration)
        t0 = time.monotonic()
        self.emit(phase="inspect", iteration=iteration, edit_family=family,
                  message=f"family {family} · parent c{parent.id} (score {parent.score})")
        steps: list[dict[str, Any]] = [{
            "step": 1, "phase": "inspect", "tool": "select_family",
            "model_output": f"edit family={family}, parent=c{parent.id}, parent score={parent.score}",
            "parser_status": "ok", "duration_ms": int((time.monotonic() - t0) * 1000), "ts": utcnow().isoformat()}]

        history = self._history()
        prompt = (
            f"Task:\n{self.task[:6000]}\n\nEdit family for this turn: {family} — {FAMILY_HINTS.get(family, family)}.\n\n"
            f"Frozen files in the working directory: {', '.join(self.frozen) or 'none'}\n\n"
            f"Recent attempts (newest last):\n{history}\n\n"
            f"Parent program c{parent.id} (score {parent.score}), file {self.entry}:\n```python\n{parent.program[:40_000]}\n```"
        )
        self.emit(phase="hypothesize", iteration=iteration, edit_family=family, message="asking the proposer")
        t1 = time.monotonic()
        try:
            res = await self.client.complete(PROPOSER_SYSTEM.format(timeout=settings.exec_timeout_s), prompt,
                                             max_tokens=16_000, effort="high")
        except llm.LLMError as exc:
            self.emit(phase="hypothesize", iteration=iteration, level="error", message=str(exc))
            raise
        self.spend_add(res.cost_usd, 1, detail=f"proposal it{iteration} · {res.model}")
        program = llm.extract_code(res.text)
        hyp = re.search(r"HYPOTHESIS:\s*(.+?)(?:\n```|\Z)", res.text, re.S)
        hypothesis = hyp.group(1).strip()[:800] if hyp else "(no hypothesis given)"
        parser_status = "ok" if program and hyp else ("repaired" if program else "failed")
        steps.append({"step": 2, "phase": "hypothesize", "tool": "proposer", "prompt": prompt[:6000],
                      "model_output": res.text[:12_000], "parser_status": parser_status,
                      "duration_ms": int((time.monotonic() - t1) * 1000), "tokens": res.input_tokens + res.output_tokens,
                      "ts": utcnow().isoformat()})
        program = program or parent.program
        diff = "".join(difflib.unified_diff(parent.program.splitlines(True), program.splitlines(True),
                                            fromfile=f"c{parent.id}/{self.entry}", tofile=f"new/{self.entry}"))
        cand = self._new_candidate(iteration, family, parent.id, program, diff)
        steps.append({"step": 3, "phase": "edit", "tool": "apply_edit",
                      "model_output": f"{diff.count(chr(10))} diff lines" if diff else "no change",
                      "parser_status": parser_status, "ts": utcnow().isoformat()})
        self.emit(phase="edit", iteration=iteration, candidate_id=cand, edit_family=family, message=hypothesis[:200])
        await self._run_candidate(cand, program, parent.program, steps, {"hypothesis": hypothesis}, iteration,
                                  res.cost_usd, res.input_tokens, parser_failed=not diff)

    def _history(self) -> str:
        with session_scope() as db:
            cands = list(db.scalars(select(Candidate).where(Candidate.campaign_id == self.cid,
                                                            Candidate.status != "running")
                                    .order_by(Candidate.id.desc()).limit(12)))
        lines = []
        for c in reversed(cands):
            what = (c.notes or {}).get("hypothesis", "")[:160]
            lines.append(f"- c{c.id} [{c.edit_family}] {c.status} score={c.score}"
                         f"{' failure=' + c.failure_kind if c.failure_kind else ''} — {what}")
        return "\n".join(lines) or "(none yet)"

    async def _run_candidate(self, cand_id: int, program: str, parent_program: str, steps: list[dict],
                             notes: dict, iteration: int, llm_cost: float, ctx_tokens: int = 0,
                             parser_failed: bool = False) -> None:
        t0 = time.monotonic()
        tdir = campaign_dir(self.cid) / f"c{cand_id}"
        sandbox.prepare_workdir(tdir, self.frozen, {self.entry: program})
        leak_status, leak_reasons = leakage_scan(program, parent_program, list(self.frozen)) if iteration else ("clean", [])

        try:
            compile(program, self.entry, "exec")
            syntax_ok = True
        except SyntaxError as exc:
            syntax_ok = False
            syntax_err = f"SyntaxError: {exc}"

        if parser_failed and iteration:
            result = sandbox.ExecResult(False, "error", None, "", "Proposer returned no usable program change.", 0.0)
            failure: str | None = "syntax"
            score = None
            hashes_ok = True
        elif not syntax_ok:
            result = sandbox.ExecResult(False, "error", 1, "", syntax_err, 0.0)
            failure, score, hashes_ok = "syntax", None, True
        else:
            self.emit(phase="evaluate", iteration=iteration, candidate_id=cand_id, message="running in sandbox")
            result = await sandbox.execute(tdir, self.entry, runpod=self.runpod)
            hashes_ok = _hash_files({n: tdir / n for n in self.frozen}) == self.frozen_hashes
            score = parse_score(result.stdout, tdir) if result.ok else None
            failure = classify_failure(result, score, hashes_ok)
            if failure is None and leak_status == "flagged":
                failure = "leakage"
        if result.cost_usd or result.gpu_hours:
            self.spend_add(result.cost_usd, 0, result.gpu_hours, detail=f"execution c{cand_id}")

        (tdir / "stdout.txt").write_text(result.stdout)
        (tdir / "stderr.txt").write_text(result.stderr)
        steps.append({"step": len(steps) + 1, "phase": "evaluate", "tool": "sandbox",
                      "tool_calls": [{"name": f"python {self.entry}", "input": {"backend": sandbox.backend_name()},
                                      "output": {"status": result.status, "exit_code": result.exit_code,
                                                 "seconds": result.seconds, "score": score,
                                                 "stdout_tail": result.stdout[-1500:], "stderr_tail": result.stderr[-1500:]}}],
                      "parser_status": "ok" if score is not None else "failed",
                      "duration_ms": int(result.seconds * 1000), "ts": utcnow().isoformat()})

        with session_scope() as db:
            camp = db.get(Campaign, self.cid)
            spend = dict(camp.spend or {})
            budget = (camp.config or {}).get("budget") or {}
            budget_ok = not (budget.get("usd") and spend.get("usd", 0) > budget["usd"] * 1.05) and result.status != "timeout"
            prev_best = camp.best_score
            improved = failure is None and score is not None and (prev_best is None or score > prev_best)
            status = "invalid" if failure else ("kept" if improved else "discarded")
            delta = None if score is None or prev_best is None else score - prev_best
            if failure:
                outcome = f"Invalid ({failure}). " + (leak_reasons[0] if failure == "leakage" and leak_reasons else
                                                      (result.stderr.strip().splitlines() or [''])[-1][:300])
            elif improved:
                outcome = f"Score {score:.6g}" + (f", +{delta:.4g} over the previous best." if delta is not None else " (baseline).")
            else:
                outcome = f"Score {score:.6g}, {delta:+.4g} versus best {prev_best:.6g}; discarded."
            notes = {**notes, "result": outcome,
                     "interpretation": ("Kept as the new best; later proposals build on it." if improved else
                                        "Reverted to the previous best." if status == "discarded" else
                                        "Excluded from the search; see stderr and audit.")}
            cand = db.get(Candidate, cand_id)
            # An invalid candidate's self-reported score is not a score: keep it
            # for the audit trail only, so it can never rank on the leaderboard.
            cand.status, cand.score = status, (None if failure else score)
            cand.failure_kind = failure
            cand.stdout, cand.stderr = result.stdout[-50_000:], result.stderr[-20_000:]
            cand.metrics = {**parse_metrics(tdir), "runtime_s": result.seconds,
                            **({"reported_score": score} if failure and score is not None else {})}
            cand.notes = notes
            cand.trace = steps
            cand.cost_vector = {"usd": round(llm_cost + result.cost_usd, 6), "model_calls": 1 if iteration else 0,
                                "gpu_hours": round(result.gpu_hours, 6), "context_tokens": ctx_tokens,
                                "wall_clock_minutes": round((time.monotonic() - t0) / 60 + result.seconds / 60, 4)}
            cand.audit = {"eval_hash_match": hashes_ok, "leakage_scan": leak_status, "leakage_reasons": leak_reasons,
                          "budget_compliant": budget_ok, "decision": "pending", "reviewed_by": None,
                          "reviewed_at": None, "reason": None}
            cand.ended_at = utcnow()
            if improved:
                camp.best_score, camp.best_candidate_id = score, cand_id
            fc = dict(camp.failure_counts or {})
            if failure:
                fc[failure] = fc.get(failure, 0) + 1
            camp.failure_counts = fc
            spend["iters"] = iteration
            camp.spend = spend
            camp.metrics = self._metrics(db, improved)
        self.emit(phase="evaluate", iteration=iteration, candidate_id=cand_id, score=score, status=status,
                  message=outcome, level="warning" if status == "invalid" else "info")

    def _metrics(self, db, improved: bool) -> dict[str, Any]:
        cands = list(db.scalars(select(Candidate).where(Candidate.campaign_id == self.cid, Candidate.iteration > 0,
                                                        Candidate.status.in_(["kept", "discarded", "invalid"]))))
        camp = db.get(Campaign, self.cid)
        prev = dict(camp.metrics or {})
        n = len(cands)
        kept = [c for c in cands if c.status == "kept"]
        invalid = [c for c in cands if c.status == "invalid"]
        discarded = [c for c in cands if c.status == "discarded"]
        kept_families = {c.edit_family for c in kept}
        useful = [d for d in discarded if any(k.edit_family == d.edit_family and k.id > d.id for k in kept)]
        stagnation = 0 if improved else prev.get("stagnation_counter", 0) + 1
        best = db.get(Candidate, camp.best_candidate_id) if camp.best_candidate_id else None
        ttb = 0.0
        if best and camp.started_at:
            start = camp.started_at if camp.started_at.tzinfo else camp.started_at.replace(tzinfo=utcnow().tzinfo)
            end = best.ended_at if best.ended_at and best.ended_at.tzinfo else (best.ended_at or utcnow()).replace(tzinfo=utcnow().tzinfo)
            ttb = round((end - start).total_seconds() / 60, 3)
        return {
            "evaluated": n,
            "hit_rate": round(len(kept) / n, 4) if n else 0.0,
            "invalid_rate": round(len(invalid) / n, 4) if n else 0.0,
            "time_to_best_minutes": ttb,
            "useful_regression_rate": round(len(useful) / len(discarded), 4) if discarded else 0.0,
            "stagnation_counter": stagnation,
            "improving_families": sorted(kept_families),
        }


async def run_campaign(campaign_id: int) -> None:
    await Search(campaign_id).run()


# ---- read models --------------------------------------------------------------


def leaderboard(cands: list[Candidate]) -> list[dict]:
    rows = sorted(cands, key=lambda c: (c.score is None, -(c.score or 0), c.id))
    out = []
    for c in rows:
        runtime = (c.metrics or {}).get("runtime_s")
        out.append({
            "candidate_id": c.id, "run_id": None, "parent_id": c.parent_id, "edit_family": c.edit_family,
            "status": c.status, "score": c.score, "cost_usd": (c.cost_vector or {}).get("usd"),
            "throughput": round(3600 / runtime, 2) if isinstance(runtime, (int, float)) and runtime > 0 else None,
            "context_tokens": (c.cost_vector or {}).get("context_tokens"),
            "note": (c.notes or {}).get("hypothesis"),
        })
    return out


def frontier(cands: list[Candidate]) -> list[dict]:
    pts = []
    for c in cands:
        runtime = (c.metrics or {}).get("runtime_s")
        if c.score is None or c.status == "invalid" or not isinstance(runtime, (int, float)):
            continue
        pts.append({"candidate_id": c.id, "quality": c.score, "cost": round(float(runtime), 3),
                    "quality_label": "score", "cost_label": "runtime (s)", "edit_family": c.edit_family})
    for p in pts:
        p["dominated"] = any(
            q is not p and q["quality"] >= p["quality"] and q["cost"] <= p["cost"]
            and (q["quality"] > p["quality"] or q["cost"] < p["cost"]) for q in pts
        )
    return pts


def trace_tree(root: Path) -> list[dict]:
    def walk(d: Path) -> list[dict]:
        nodes = []
        for p in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if p.name.startswith("."):
                continue
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                nodes.append({"name": p.name, "path": rel, "type": "dir", "children": walk(p)})
            else:
                nodes.append({"name": p.name, "path": rel, "type": "file", "size_bytes": p.stat().st_size})
        return nodes

    return walk(root) if root.is_dir() else []


def audit_report(c: Candidate) -> dict:
    a = c.audit or {}
    checks = [
        {"name": "frozen files unchanged (eval hash)", "ok": bool(a.get("eval_hash_match", False)),
         "detail": "all frozen inputs hash-identical after the run" if a.get("eval_hash_match") else "a frozen file changed"},
        {"name": "leakage scan", "ok": a.get("leakage_scan") == "clean",
         "detail": "; ".join(a.get("leakage_reasons") or []) or a.get("leakage_scan", "skipped")},
        {"name": "budget compliant", "ok": bool(a.get("budget_compliant", False)),
         "detail": f"cost ${(c.cost_vector or {}).get('usd', 0):.4f}"},
        {"name": "valid score", "ok": c.score is not None and c.failure_kind is None,
         "detail": f"score {c.score}" if c.score is not None else (c.failure_kind or "no score")},
    ]
    return {"candidate_id": c.id, "audit": a, "checks": checks, "generated_at": utcnow().isoformat()}
