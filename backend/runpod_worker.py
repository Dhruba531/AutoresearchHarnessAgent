"""Reference RunPod Serverless handler for AGENTLAB_EXECUTION=runpod.

Deploy this as a RunPod Serverless worker (e.g. a Python image with numpy,
pandas, scikit-learn, torch as you need, plus `pip install runpod`), then save
your RunPod API key in the console with the endpoint id as its model name.

Input:  {"code": str, "entrypoint": str, "files": {name: base64}, "timeout": int}
Output: {"stdout": str, "stderr": str, "exit_code": int, "timed_out": bool,
         "files": {name: base64}}   # new .png/.json/.csv/.txt files only
"""

import base64
import subprocess
import sys
import tempfile
from pathlib import Path

KEEP = {".png", ".svg", ".jpg", ".jpeg", ".json", ".csv", ".txt", ".md"}


def handler(job):
    inp = job["input"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, b64 in (inp.get("files") or {}).items():
            dst = (root / name).resolve()
            if root.resolve() in dst.parents:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(base64.b64decode(b64))
        entry = inp.get("entrypoint", "main.py")
        (root / entry).parent.mkdir(parents=True, exist_ok=True)
        (root / entry).write_text(inp["code"])
        before = {p for p in root.rglob("*") if p.is_file()}
        try:
            proc = subprocess.run([sys.executable, entry], cwd=root, capture_output=True, text=True,
                                  timeout=int(inp.get("timeout", 300)))
            out, err, code, timed_out = proc.stdout, proc.stderr, proc.returncode, False
        except subprocess.TimeoutExpired as exc:
            out, err, code, timed_out = exc.stdout or "", (exc.stderr or "") + "\ntimeout", -9, True
        files = {
            p.relative_to(root).as_posix(): base64.b64encode(p.read_bytes()).decode()
            for p in root.rglob("*")
            if p.is_file() and p not in before and p.suffix.lower() in KEEP and p.stat().st_size < 5_000_000
        }
        return {"stdout": out[-200_000:], "stderr": err[-100_000:], "exit_code": code, "timed_out": timed_out,
                "files": files}


if __name__ == "__main__":
    import runpod  # type: ignore[import-not-found]

    runpod.serverless.start({"handler": handler})
