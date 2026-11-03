"""Running Python that the agent wrote or the operator uploaded.

Two backends, chosen by AGENTLAB_EXECUTION:

local   A subprocess on this host in its own working directory, with a
        scrubbed environment (no API keys, no database URL), POSIX resource
        limits on memory, CPU time and file size, a wall-clock timeout, and its
        own process group so a timeout kills everything it spawned. This is
        resource isolation, not a security boundary: the code can still read
        files the server user can read and open network connections. Run the
        backend in a container or as an unprivileged user if that matters, or
        use RunPod.
runpod  A RunPod Serverless endpoint (endpoint id stored as the RunPod key's
        "model name"). The worker receives {"input": {"code", "entrypoint",
        "files": {name: base64}, "timeout"}} and must return {"stdout",
        "stderr", "exit_code", "files": {name: base64}}; see
        backend/runpod_worker.py for a reference handler.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import settings

MAX_CAPTURE = 200_000  # bytes of stdout/stderr kept per run
OUTPUT_SUFFIXES = {".png", ".svg", ".jpg", ".jpeg", ".json", ".csv", ".txt", ".md"}
MAX_OUTPUT_FILE = 5 * 1024 * 1024

# Rough on-demand RunPod Serverless prices, USD per GPU-hour. Used only to
# estimate spend; the RunPod bill is authoritative.
GPU_RATES = {"rtx 4090": 0.69, "rtx a5000": 0.44, "l4": 0.48, "a40": 0.79, "a100": 1.89, "h100": 3.35, "cpu": 0.10}


@dataclass
class ExecResult:
    ok: bool
    status: str  # ok | error | timeout | oom | unavailable
    exit_code: int | None
    stdout: str
    stderr: str
    seconds: float
    cost_usd: float = 0.0
    gpu_hours: float = 0.0
    outputs: dict[str, Path] = field(default_factory=dict)  # produced files, name → path


def backend_name() -> str:
    return settings.execution if settings.execution in {"local", "runpod"} else "none"


def _limits() -> None:  # runs in the child between fork and exec
    import resource

    mem = settings.exec_memory_mb * 1024 * 1024
    for res, value in (
        (resource.RLIMIT_AS, mem),
        (resource.RLIMIT_CPU, settings.exec_timeout_s + 10),
        (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def _clip(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(text) > MAX_CAPTURE:
        half = MAX_CAPTURE // 2
        text = text[:half] + "\n…[output truncated]…\n" + text[-half:]
    return text


def _collect(workdir: Path, before: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in sorted(workdir.rglob("*")):
        rel = p.relative_to(workdir).as_posix()
        if (
            p.is_file()
            and rel not in before
            and p.suffix.lower() in OUTPUT_SUFFIXES
            and p.stat().st_size <= MAX_OUTPUT_FILE
            and not rel.startswith(".")
        ):
            found[rel] = p
    return found


def prepare_workdir(workdir: Path, inputs: dict[str, Path], code: dict[str, str]) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    for name, src in inputs.items():
        dst = workdir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for name, text in code.items():
        dst = workdir / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text)


async def run_local(workdir: Path, entrypoint: str, timeout: int) -> ExecResult:
    before = {p.relative_to(workdir).as_posix() for p in workdir.rglob("*")}
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MPLBACKEND": "Agg",
        "OMP_NUM_THREADS": os.environ.get("AGENTLAB_EXEC_THREADS", "4"),
    }
    python = settings.exec_python or sys.executable
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        python, "-I", entrypoint,
        cwd=str(workdir), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        preexec_fn=_limits if os.name == "posix" else None,
        start_new_session=True,
    )

    def _kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill()
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        _kill()
        raise
    seconds = time.monotonic() - started
    stdout, stderr = _clip(out), _clip(err)
    code = proc.returncode
    if timed_out:
        status = "timeout"
        stderr += f"\n[sandbox] killed after {timeout}s wall-clock limit"
    elif code == 0:
        status = "ok"
    elif "MemoryError" in stderr or (code in (-signal.SIGKILL, -signal.SIGSEGV) and "Traceback" not in stderr):
        status = "oom"
    else:
        status = "error"
    return ExecResult(status == "ok", status, code, stdout, stderr, round(seconds, 3), outputs=_collect(workdir, before))


async def run_runpod(workdir: Path, entrypoint: str, timeout: int, api_key: str, endpoint_id: str,
                     gpu_type: str) -> ExecResult:
    files = {}
    for p in workdir.rglob("*"):
        if p.is_file() and p.stat().st_size <= 20 * 1024 * 1024:
            files[p.relative_to(workdir).as_posix()] = base64.b64encode(p.read_bytes()).decode()
    payload = {"input": {"code": (workdir / entrypoint).read_text(), "entrypoint": entrypoint,
                         "files": files, "timeout": timeout}}
    headers = {"Authorization": f"Bearer {api_key}"}
    base = f"https://api.runpod.ai/v2/{endpoint_id}"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout + 60) as http:
        resp = await http.post(f"{base}/runsync", json=payload, headers=headers)
        if resp.status_code >= 400:
            return ExecResult(False, "error", None, "", f"RunPod {resp.status_code}: {resp.text[:500]}", 0.0)
        data = resp.json()
        deadline = time.monotonic() + timeout + 120
        while data.get("status") in {"IN_QUEUE", "IN_PROGRESS"} and time.monotonic() < deadline:
            await asyncio.sleep(3)
            data = (await http.get(f"{base}/status/{data['id']}", headers=headers)).json()
        if data.get("status") in {"IN_QUEUE", "IN_PROGRESS"}:
            await http.post(f"{base}/cancel/{data['id']}", headers=headers)
            return ExecResult(False, "timeout", None, "", "RunPod job did not finish in time; cancelled.",
                              time.monotonic() - started)
    seconds = (data.get("executionTime") or (time.monotonic() - started) * 1000) / 1000
    rate = GPU_RATES.get(gpu_type.lower(), GPU_RATES["rtx 4090"])
    gpu_hours = seconds / 3600
    output = data.get("output") or {}
    if data.get("status") != "COMPLETED":
        return ExecResult(False, "error", None, "", f"RunPod job {data.get('status')}: {data.get('error', '')}",
                          seconds, rate * gpu_hours, gpu_hours)
    before = {p.relative_to(workdir).as_posix() for p in workdir.rglob("*")}
    for name, b64 in (output.get("files") or {}).items():
        dst = (workdir / name).resolve()
        if workdir.resolve() in dst.parents:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(base64.b64decode(b64))
    code = output.get("exit_code")
    status = "ok" if code == 0 else ("timeout" if output.get("timed_out") else "error")
    return ExecResult(code == 0, status, code, _clip(str(output.get("stdout", "")).encode()),
                      _clip(str(output.get("stderr", "")).encode()), round(seconds, 3), rate * gpu_hours, gpu_hours,
                      _collect(workdir, before))


async def execute(workdir: Path, entrypoint: str, *, timeout: int | None = None,
                  runpod: tuple[str, str] | None = None, gpu_type: str = "rtx 4090") -> ExecResult:
    """Run `entrypoint` inside an already-prepared `workdir`."""
    timeout = min(timeout or settings.exec_timeout_s, settings.exec_timeout_s)
    backend = backend_name()
    if backend == "local":
        return await run_local(workdir, entrypoint, timeout)
    if backend == "runpod":
        if not runpod:
            return ExecResult(False, "unavailable", None, "", "RunPod execution needs an enabled RunPod key and endpoint id.", 0.0)
        return await run_runpod(workdir, entrypoint, timeout, runpod[0], runpod[1], gpu_type)
    return ExecResult(False, "unavailable", None, "", "Code execution is disabled (AGENTLAB_EXECUTION=none).", 0.0)


def notebook_to_script(raw: str) -> str:
    """Concatenate a notebook's code cells into one script (magics dropped)."""
    import json

    nb = json.loads(raw)
    chunks = []
    for cell in nb.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith(("%", "!"))]
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks) + "\n"
