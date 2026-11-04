"""AgentLab backend — the HTTP + WebSocket API the operator console talks to.

Run:  uvicorn backend.app:app --port 8000
Every route below is listed in src/lib/api.ts with the same path and shape.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import __version__, campaign as search, llm, pipeline, sandbox
from .auth import current_user, user_from_token
from .config import settings
from .db import (
    Artifact,
    AuditEvent,
    Brief,
    Campaign,
    Candidate,
    Project,
    ProviderKey,
    ReviewFinding,
    Revision,
    Run,
    RunLog,
    SessionLocal,
    SpendEvent,
    User,
    get_db,
    init_db,
    iso,
    utcnow,
)
from .events import hub, spawn, tasks
from .ledger import monthly_spend, record, usage
from .schemas import (
    BriefPatch,
    CampaignIn,
    Credentials,
    EstimateIn,
    FinalIn,
    IdeaIn,
    NotesIn,
    ProjectIn,
    ProviderKeyIn,
    RejectIn,
    RevisionIn,
    RunIn,
    UploadIn,
    UploadRunIn,
    brief_out,
    campaign_out,
    candidate_out,
    finding_out,
    project_out,
    provider_key_out,
    revision_out,
    run_out,
    user_out,
)
from .security import encrypt_secret, hash_password, issue_local_token, verify_artifact_sig, verify_password

ACTIVE_RUN = {"queued", "running", "review", "revising"}
TERMINAL_RUN = {"completed", "failed", "cancelled"}
STARTED_AT = time.time()


def _recover_interrupted() -> None:
    """Background tasks die with the process; say so instead of leaving rows 'running'."""
    with SessionLocal() as db:
        for run in db.scalars(select(Run).where(Run.status.in_(ACTIVE_RUN))):
            run.status, run.error, run.ended_at = "failed", "Interrupted by a server restart.", utcnow()
        for camp in db.scalars(select(Campaign).where(Campaign.status.in_(["queued", "running"]))):
            camp.status, camp.error, camp.ended_at = "stopped", "Interrupted by a server restart.", utcnow()
        for cand in db.scalars(select(Candidate).where(Candidate.status == "running")):
            cand.status, cand.failure_kind = "invalid", "timeout"
        db.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _recover_interrupted()
    yield
    for task in list(tasks.values()):
        task.cancel()


app = FastAPI(title="AgentLab research backend", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"https://.*\.workers\.dev|https://.*\.lovable\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- helpers -------------------------------------------------------------------


def owned_project(db: Session, user: User, project_id: int) -> Project:
    p = db.get(Project, project_id)
    if p is None or p.owner_id != user.id:
        raise HTTPException(404, "Project not found.")
    return p


def owned_run(db: Session, user: User, run_id: int) -> Run:
    r = db.get(Run, run_id)
    if r is None or r.owner_id != user.id:
        raise HTTPException(404, "Run not found.")
    return r


def owned_campaign(db: Session, user: User, campaign_id: int) -> Campaign:
    c = db.get(Campaign, campaign_id)
    if c is None or c.owner_id != user.id:
        raise HTTPException(404, "Campaign not found.")
    return c


def owned_candidate(db: Session, user: User, candidate_id: int) -> tuple[Candidate, Campaign]:
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found.")
    return cand, owned_campaign(db, user, cand.campaign_id)


def audit(db: Session, user: User, action: str, project_id: int | None, notes: str = "", **payload: Any) -> None:
    db.add(AuditEvent(user_id=user.id, project_id=project_id, action=action, notes=notes, payload=payload))


def latest_brief(db: Session, project_id: int) -> Brief | None:
    return db.scalar(select(Brief).where(Brief.project_id == project_id).order_by(Brief.version.desc()).limit(1))


def require_cap(db: Session, user: User, upcoming: float = 0.0) -> None:
    spent = monthly_spend(db, user.id)
    if spent + upcoming > settings.monthly_cap_usd:
        raise HTTPException(402, f"Monthly cap ${settings.monthly_cap_usd:.2f} reached (spent ${spent:.2f}).")


def text_client_or_400(db: Session, user: User) -> llm.TextClient:
    try:
        return llm.text_client(db, user)
    except llm.NoProviderError as exc:
        raise HTTPException(400, str(exc)) from exc


def execute_ready(db: Session, user: User) -> tuple[bool, str]:
    backend = sandbox.backend_name()
    if backend == "local":
        return True, ""
    if backend == "runpod":
        if llm.provider_credentials(db, user, "runpod"):
            return True, ""
        return False, "Enable a RunPod key (endpoint id as model name) to execute code."
    return False, "Code execution is disabled on this server (AGENTLAB_EXECUTION=none)."


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$")


# ---- health, auth, account ----------------------------------------------------------


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    with SessionLocal() as db:
        db.execute(select(1))
    return {"ok": True, "service": "agentlab-backend", "version": __version__,
            "execution": sandbox.backend_name(), "uptime_s": int(time.time() - STARTED_AT)}


@app.get("/api/auth/config")
def auth_config() -> dict:
    return {"local": settings.local_auth_enabled, "supabase": settings.supabase_enabled,
            "registration": settings.allow_registration and settings.local_auth_enabled}


@app.post("/api/auth/register")
def register(body: Credentials, db: Session = Depends(get_db)) -> dict:
    if not settings.local_auth_enabled:
        raise HTTPException(400, "Local accounts are disabled; sign in with Supabase.")
    if not settings.allow_registration:
        raise HTTPException(403, "Registration is closed on this server.")
    email = body.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Enter a valid email address.")
    if db.scalar(select(User).where(User.external_id == f"local:{email}")):
        raise HTTPException(409, "An account with this email already exists.")
    user = User(external_id=f"local:{email}", email=email, name=body.name.strip() or email.split("@")[0],
                role="admin", password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return {"token": issue_local_token(user.id, email), "user": user_out(user)}


@app.post("/api/auth/login")
def login(body: Credentials, db: Session = Depends(get_db)) -> dict:
    if not settings.local_auth_enabled:
        raise HTTPException(400, "Local accounts are disabled; sign in with Supabase.")
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.external_id == f"local:{email}"))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password.")
    return {"token": issue_local_token(user.id, email), "user": user_out(user)}


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return user_out(user)


@app.get("/api/me/usage")
def my_usage(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return usage(db, user.id)


MODEL_CHOICES = {
    "anthropic": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    "openai": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "o4-mini"],
    "deepseek": ["deepseek-reasoner", "deepseek-chat"],
    "gemini": ["gemini-2.5-flash-image"],
}


@app.get("/api/capabilities")
def capabilities(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    enabled = llm.enabled_providers(db, user)
    text_ready = any(p in enabled for p in llm.TEXT_PROVIDERS)
    exec_ok, exec_reason = execute_ready(db, user)
    reasons: dict[str, str] = {}
    if not text_ready:
        reasons["real_run_ready"] = "Enable an Anthropic, OpenAI or DeepSeek key to unlock runs."
    if not exec_ok:
        reasons["execute_ready"] = exec_reason
    if "gemini" not in enabled:
        reasons["figures_ready"] = "Enable a Gemini key to generate figures."
    if usage(db, user.id)["over_budget"]:
        reasons["real_run_ready"] = f"Monthly cap ${settings.monthly_cap_usd:.2f} reached."
    return {
        "real_run_ready": text_ready,
        "execute_ready": exec_ok,
        "figures_ready": "gemini" in enabled,
        "orchestration_modes": ["pipeline", "agentic"],
        "gpu_types": list(sandbox.GPU_RATES) if sandbox.backend_name() == "runpod" else ["cpu"],
        "enabled_providers": enabled,
        "models": MODEL_CHOICES,
        "reasons": reasons,
        "execution_backend": sandbox.backend_name(),
        "text_model": llm.text_model_for(db, user),
        "exec_timeout_s": settings.exec_timeout_s,
    }


@app.get("/api/status")
def system_status() -> dict:
    """Public telemetry for the landing page: aggregate counts only."""
    t0 = time.perf_counter()
    with SessionLocal() as db:
        active = db.scalar(select(func.count()).select_from(Run).where(Run.status.in_(["running", "review"]))) or 0
        active += db.scalar(select(func.count()).select_from(Campaign).where(Campaign.status == "running")) or 0
        queued = db.scalar(select(func.count()).select_from(Run).where(Run.status == "queued")) or 0
        minute_ago, hour_ago = utcnow() - timedelta(minutes=1), utcnow() - timedelta(hours=1)
        steps = db.scalar(select(func.count()).select_from(RunLog).where(RunLog.created_at >= minute_ago)) or 0
        steps += db.scalar(select(func.count()).select_from(Candidate).where(Candidate.created_at >= minute_ago)) or 0
        spend = db.scalar(select(func.coalesce(func.sum(SpendEvent.amount_usd), 0.0))
                          .where(SpendEvent.created_at >= hour_ago)) or 0.0
    return {
        "ok": True, "region": settings.region, "build": __version__, "ts": utcnow().isoformat(),
        "runs_active": int(active), "runs_queued": int(queued), "reviewers_online": len(pipeline.REVIEWERS),
        "gate_latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "steps_per_min": int(steps), "spend_per_hr": round(float(spend), 4),
    }


# ---- provider keys -----------------------------------------------------------------


@app.get("/api/provider-keys")
def list_keys(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(ProviderKey).where(ProviderKey.user_id == user.id).order_by(ProviderKey.provider))
    return [provider_key_out(k) for k in rows]


@app.post("/api/provider-keys")
def save_key(body: ProviderKeyIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    key = body.api_key.strip()
    row = db.scalar(select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.provider == body.provider))
    if row is None:
        row = ProviderKey(user_id=user.id, provider=body.provider, encrypted_key="")
        db.add(row)
    row.encrypted_key = encrypt_secret(key)
    row.key_last4 = key[-4:]
    row.model_name = body.model_name.strip() or llm.DEFAULT_MODELS.get(body.provider, "")
    row.is_enabled = body.is_enabled
    row.test_status = "untested"
    row.updated_at = utcnow()
    db.commit()
    return provider_key_out(row, key)


@app.delete("/api/provider-keys/{provider}")
def delete_key(provider: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.provider == provider))
    if row is None:
        raise HTTPException(404, "No key saved for this provider.")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/api/provider-keys/{provider}/test")
async def test_key(provider: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.scalar(select(ProviderKey).where(ProviderKey.user_id == user.id, ProviderKey.provider == provider))
    if row is None:
        raise HTTPException(404, "No key saved for this provider.")
    from .security import decrypt_secret

    try:
        plain = decrypt_secret(row.encrypted_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    ok, status, detail = await llm.test_key(provider, plain, row.model_name)
    row.test_status, row.last_tested_at = status, utcnow()
    db.commit()
    return {"provider": provider, "ok": ok, "status": status, "detail": detail, "tested_at": iso(row.last_tested_at)}


# ---- projects ------------------------------------------------------------------------


@app.get("/api/projects")
def list_projects(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.updated_at.desc()))
    return [project_out(db, p, detail=False) for p in rows]


@app.post("/api/projects")
def create_project(body: ProjectIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p = Project(owner_id=user.id, title=body.title.strip(), objective=body.objective.strip(),
                idea_text=body.idea_text.strip(), status="idea" if body.idea_text.strip() else "draft")
    db.add(p)
    db.commit()
    return project_out(db, p)


@app.get("/api/projects/{project_id}")
def get_project(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return project_out(db, owned_project(db, user, project_id))


@app.post("/api/projects/{project_id}/idea")
def save_idea(project_id: int, body: IdeaIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p = owned_project(db, user, project_id)
    p.idea_text = body.idea_text.strip()
    if p.status == "draft":
        p.status = "idea"
    p.updated_at = utcnow()
    db.commit()
    return project_out(db, p)


# ---- uploads -------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/uploads")
def list_uploads(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_project(db, user, project_id)
    return [{"name": n, "size_bytes": p.stat().st_size,
             "uploaded_at": iso(__import__("datetime").datetime.fromtimestamp(p.stat().st_mtime, utcnow().tzinfo))}
            for n, p in pipeline.list_uploads(project_id).items()]


@app.post("/api/projects/{project_id}/uploads")
def upload(project_id: int, body: UploadIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    owned_project(db, user, project_id)
    name = Path(body.filename).name.strip()
    if not SAFE_NAME.match(name):
        raise HTTPException(400, "Use a plain file name (letters, digits, dot, dash, underscore, space).")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "content_base64 is not valid base64.") from exc
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.max_upload_mb} MB.")
    d = pipeline.uploads_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return {"name": name, "size_bytes": len(data), "uploaded_at": utcnow().isoformat()}


@app.post("/api/projects/{project_id}/uploads/{filename}/run")
async def run_upload(project_id: int, filename: str, body: UploadRunIn, user: User = Depends(current_user),
                     db: Session = Depends(get_db)) -> dict:
    owned_project(db, user, project_id)
    files = pipeline.list_uploads(project_id)
    if filename not in files or not filename.endswith((".py", ".ipynb")):
        raise HTTPException(404, "No runnable upload with that name.")
    ok, reason = execute_ready(db, user)
    if not ok:
        raise HTTPException(400, reason)
    require_cap(db, user)
    runpod = None
    timeout = settings.exec_timeout_s
    if sandbox.backend_name() == "runpod":
        key = llm.provider_credentials(db, user, "runpod")
        endpoint = body.endpoint_id.strip() or (key[1] if key else "")
        if not key or not endpoint:
            raise HTTPException(400, "Enter your RunPod endpoint id.")
        runpod = (key[0], endpoint)
        rate = sandbox.GPU_RATES.get(body.gpu_type.lower(), sandbox.GPU_RATES["rtx 4090"])
        timeout = max(10, min(timeout, int(body.budget_usd / rate * 3600)))
    workdir = pipeline.project_dir(project_id) / "exec" / str(int(time.time() * 1000))
    pipeline.copy_uploads_to(workdir, project_id)
    entry = filename
    if filename.endswith(".ipynb"):
        entry = filename[:-6] + "_nb.py"
        (workdir / entry).write_text(sandbox.notebook_to_script(files[filename].read_text()))
    result = await sandbox.execute(workdir, entry, timeout=timeout, runpod=runpod, gpu_type=body.gpu_type)
    record(db, user.id, result.cost_usd, "exec", project_id, f"upload run {filename}")
    db.commit()
    return {"ok": result.ok, "status": result.status, "stdout": result.stdout, "stderr": result.stderr,
            "seconds": result.seconds, "cost_usd": round(result.cost_usd, 6)}


# ---- briefs (gate G1) ------------------------------------------------------------------


@app.post("/api/projects/{project_id}/briefs/generate")
async def generate_brief(project_id: int, request: Request, user: User = Depends(current_user),
                         db: Session = Depends(get_db)) -> dict:
    p = owned_project(db, user, project_id)
    if not (p.idea_text or p.objective):
        raise HTTPException(400, "Save a research idea before generating a brief.")
    text_client_or_400(db, user)
    require_cap(db, user)
    try:
        body = await request.json()
    except Exception:
        body = {}
    feedback = str((body or {}).get("notes", "")) if isinstance(body, dict) else ""
    try:
        brief = await pipeline.generate_brief(project_id, user.id, feedback)
    except llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    return brief_out(brief)


@app.patch("/api/briefs/{brief_id}")
def update_brief(brief_id: int, body: BriefPatch, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    b = db.get(Brief, brief_id)
    if b is None:
        raise HTTPException(404, "Brief not found.")
    owned_project(db, user, b.project_id)
    if b.is_approved:
        raise HTTPException(409, "This brief is approved and locked. Generate a new version to change it.")
    b.content_markdown = body.content_markdown
    db.commit()
    return brief_out(b)


@app.post("/api/briefs/{brief_id}/approve")
def approve_brief(brief_id: int, body: NotesIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    b = db.get(Brief, brief_id)
    if b is None:
        raise HTTPException(404, "Brief not found.")
    p = owned_project(db, user, b.project_id)
    latest = latest_brief(db, p.id)
    if latest and latest.id != b.id:
        raise HTTPException(409, f"Only the latest brief (v{latest.version}) can be approved.")
    if not b.content_markdown.strip():
        raise HTTPException(400, "The brief is empty.")
    b.is_approved, b.approved_by, b.approved_at, b.approval_notes = True, user.id, utcnow(), body.notes
    p.status, p.updated_at = "brief_approved", utcnow()
    audit(db, user, "brief.approve", p.id, body.notes, brief_id=b.id, version=b.version)
    db.commit()
    return brief_out(b)


# ---- runs (gate G2) ---------------------------------------------------------------------


def _run_preconditions(db: Session, user: User, p: Project) -> tuple[Brief, llm.TextClient]:
    brief = latest_brief(db, p.id)
    if brief is None or not brief.is_approved:
        raise HTTPException(400, "Approve the latest brief before estimating or starting a run.")
    return brief, text_client_or_400(db, user)


@app.post("/api/projects/{project_id}/runs/estimate")
def estimate_run(project_id: int, body: EstimateIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p = owned_project(db, user, project_id)
    _, client = _run_preconditions(db, user, p)
    cost, assumptions = pipeline.estimate(client.model, body.config_json)
    return {"cost_estimate": cost, "assumptions": assumptions["text"], "details": assumptions}


@app.post("/api/projects/{project_id}/runs")
async def start_run(project_id: int, body: RunIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    p = owned_project(db, user, project_id)
    if not body.approved_cost:
        raise HTTPException(400, "Starting a run requires approved_cost: true after reviewing the estimate.")
    brief, client = _run_preconditions(db, user, p)
    config = {
        "mode": "real",
        "orchestration": "agentic" if body.config_json.get("orchestration") == "agentic" else "pipeline",
        "execute": bool(body.config_json.get("execute")),
        "figures": bool(body.config_json.get("figures")),
        "provider": client.provider,
        "model": client.model,
    }
    if config["execute"]:
        ok, reason = execute_ready(db, user)
        if not ok:
            raise HTTPException(400, reason)
    if config["figures"] and not llm.provider_credentials(db, user, "gemini"):
        raise HTTPException(400, "Figure generation needs an enabled Gemini key.")
    cost, _ = pipeline.estimate(client.model, config)
    if body.budget_threshold <= 0:
        raise HTTPException(400, "Set a budget threshold above $0.")
    if cost > body.budget_threshold:
        raise HTTPException(400, f"Estimated ${cost:.2f} exceeds the ${body.budget_threshold:.2f} budget.")
    require_cap(db, user, min(cost, body.budget_threshold) * 0.25)
    active = db.scalar(select(func.count()).select_from(Run).where(Run.owner_id == user.id, Run.status.in_(ACTIVE_RUN)))
    if (active or 0) >= settings.max_active_runs:
        raise HTTPException(429, f"{active} runs already active; wait for one to finish or cancel it.")
    run = Run(project_id=p.id, owner_id=user.id, brief_id=brief.id, status="queued", cost_estimate=cost,
              budget_threshold=body.budget_threshold, config_json=config, approval_notes=body.notes)
    db.add(run)
    p.status, p.updated_at = "running", utcnow()
    db.flush()
    audit(db, user, "run.start", p.id, body.notes, run_id=run.id, estimate=cost, budget=body.budget_threshold)
    db.commit()
    spawn(f"run:{run.id}", _run_task(run.id))
    return run_out(db, run)


async def _run_task(run_id: int) -> None:
    try:
        await pipeline.execute_run(run_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # failure before the pipeline could take ownership
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            if run and run.status not in TERMINAL_RUN:
                run.status, run.error, run.ended_at = "failed", f"{type(exc).__name__}: {exc}", utcnow()
                db.commit()
        hub.publish(f"run:{run_id}", {"type": "status", "status": "failed"})


@app.get("/api/projects/{project_id}/runs")
def list_runs(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_project(db, user, project_id)
    return [run_out(db, r, with_logs=False)
            for r in db.scalars(select(Run).where(Run.project_id == project_id).order_by(Run.id.desc()))]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return run_out(db, owned_run(db, user, run_id))


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = owned_run(db, user, run_id)
    if run.status in TERMINAL_RUN:
        raise HTTPException(409, f"Run is already {run.status}.")
    task = tasks.get(f"run:{run_id}")
    if task:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    db.expire_all()
    run = db.get(Run, run_id)
    if run.status not in TERMINAL_RUN:
        run.status, run.ended_at = "cancelled", utcnow()
    audit(db, user, "run.cancel", run.project_id, run_id=run_id)
    db.commit()
    hub.publish(f"run:{run_id}", {"type": "status", "status": "cancelled"})
    return run_out(db, run)


async def _pump(ws: WebSocket, topic: str, initial: list[dict] | None = None, stop_on: set[str] | None = None) -> None:
    q = hub.subscribe(topic)
    try:
        for ev in initial or []:
            await ws.send_json(ev)
        receiver = asyncio.create_task(ws.receive_text())
        while True:
            getter = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                break  # client closed (or sent something; we do not take input)
            ev = getter.result()
            await ws.send_json(ev)
            if stop_on and ev.get("type") == "status" and ev.get("status") in stop_on:
                receiver.cancel()
                await ws.close()
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(topic, q)


@app.websocket("/api/runs/{run_id}/logs/ws")
async def run_logs_ws(ws: WebSocket, run_id: int) -> None:
    with SessionLocal() as db:
        user = user_from_token(ws.query_params.get("token"), db)
        run = db.get(Run, run_id)
        if user is None or run is None or run.owner_id != user.id:
            await ws.close(code=4401)
            return
        status = run.status
    await ws.accept()
    if status in TERMINAL_RUN:
        await ws.send_json({"type": "status", "status": status})
        await ws.close()
        return
    await _pump(ws, f"run:{run_id}", stop_on=TERMINAL_RUN)


# ---- reviews & export (G4) ----------------------------------------------------------------


@app.get("/api/projects/{project_id}/reviews")
def list_reviews(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_project(db, user, project_id)
    latest = db.scalar(select(Run).where(Run.project_id == project_id).order_by(Run.id.desc()).limit(1))
    if latest is None:
        return []
    return [finding_out(f) for f in db.scalars(select(ReviewFinding).where(ReviewFinding.run_id == latest.id)
                                               .order_by(ReviewFinding.id))]


@app.post("/api/projects/{project_id}/final/approve")
async def approve_final(project_id: int, body: FinalIn, user: User = Depends(current_user),
                        db: Session = Depends(get_db)) -> Any:
    p = owned_project(db, user, project_id)
    run = db.scalar(select(Run).where(Run.project_id == project_id).order_by(Run.id.desc()).limit(1))
    if run is None or run.status != "completed":
        raise HTTPException(400, "The latest run must be completed before export.")
    if not run.draft_markdown.strip():
        raise HTTPException(400, "The run produced no paper draft.")
    executed = bool((run.analysis_json or {}).get("execution"))
    report = await pipeline.groundedness(run.draft_markdown, executed, pipeline.results_text_for(run))
    issues = len(report["dead_urls"]) + len(report["placeholders"]) + len(report["fabrications"])
    if issues and not body.force:
        return JSONResponse(status_code=409, content={"detail": report})
    findings = list(db.scalars(select(ReviewFinding).where(ReviewFinding.run_id == run.id)))
    decisions = list(db.scalars(select(AuditEvent).where(AuditEvent.project_id == project_id).order_by(AuditEvent.id)))
    audit(db, user, "final.force_export" if body.force else "final.approve", p.id, body.notes, run_id=run.id,
          groundedness=report)
    db.flush()
    decisions.append(db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)).first())
    lines = [run.draft_markdown.rstrip(), "\n\n---\n\n# Review record\n"]
    for f in findings:
        lines.append(f"## {f.category} · {f.severity}\n\n{f.finding}\n\n**Suggested fix:** {f.suggested_fix}\n")
    lines.append(f"\n# Groundedness\n\nscore {report['score']:.3f} · {issues} issue(s)"
                 + (" · **force-exported**" if body.force else "") + "\n")
    lines.append("\n# Approval trail\n")
    for e in decisions:
        if e:
            lines.append(f"- {iso(e.created_at)} · {e.action} · user #{e.user_id}: {e.notes or '(no note)'}")
    package = "\n".join(lines) + "\n"
    path = pipeline.run_dir(run.id) / "artifacts" / "final_package.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(package)
    art = db.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.name == "final_package.md"))
    if art is None:
        art = Artifact(run_id=run.id, project_id=p.id, kind="report", name="final_package.md", path=str(path),
                       mime_type="text/markdown")
        db.add(art)
    art.size_bytes = len(package.encode())
    p.status, p.updated_at = "exported", utcnow()
    db.commit()
    return {"artifact_id": art.id, "markdown": package, "groundedness": report, "forced": body.force}


@app.get("/api/artifacts/{artifact_id}/download")
def download(artifact_id: int, request: Request, exp: int = 0, sig: str = "") -> FileResponse:
    with SessionLocal() as db:
        art = db.get(Artifact, artifact_id)
        if art is None:
            raise HTTPException(404, "Artifact not found.")
        if not (sig and verify_artifact_sig(artifact_id, exp, sig)):
            from .auth import bearer_token

            user = user_from_token(bearer_token(request), db)
            project = db.get(Project, art.project_id)
            if user is None or project is None or project.owner_id != user.id:
                raise HTTPException(401, "Sign in, or use a fresh artifact link.")
        if not Path(art.path).is_file():
            raise HTTPException(410, "Artifact file is missing on disk.")
        return FileResponse(art.path, media_type=art.mime_type, filename=art.name)


# ---- revisions (G3) -------------------------------------------------------------------------


def _revisable(run: Run) -> None:
    if run.status not in {"review", "completed", "revising"}:
        raise HTTPException(409, "Revisions are available once the run is in review or completed.")
    if not run.draft_markdown.strip():
        raise HTTPException(409, "This run has no draft to revise.")


@app.post("/api/runs/{run_id}/revisions/estimate")
def estimate_revision(run_id: int, body: RevisionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    run = owned_run(db, user, run_id)
    _revisable(run)
    client = text_client_or_400(db, user)
    cost = pipeline.estimate_revision(client.model, run.draft_markdown, body.target_excerpt)
    return {"cost_estimate": cost, "assumptions": f"{client.model} · one call · passage ±6k chars of context"}


@app.post("/api/runs/{run_id}/revisions/preview")
async def preview_revision(run_id: int, body: RevisionIn, user: User = Depends(current_user),
                           db: Session = Depends(get_db)) -> dict:
    run = owned_run(db, user, run_id)
    _revisable(run)
    if not body.approved_cost:
        raise HTTPException(400, "Previewing a revision spends credits; pass approved_cost: true.")
    excerpt = body.target_excerpt.strip()
    if excerpt not in run.draft_markdown:
        raise HTTPException(400, "The selected passage is not in the current draft verbatim.")
    client = text_client_or_400(db, user)
    cost = pipeline.estimate_revision(client.model, run.draft_markdown, excerpt)
    require_cap(db, user, cost)
    try:
        res = await pipeline.draft_revision(client, run.draft_markdown, excerpt, body.instruction)
    except llm.LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
    rev = Revision(run_id=run.id, instruction=body.instruction, target_excerpt=excerpt,
                   replacement_markdown=res.text.strip(), cost_estimate=cost, actual_cost=res.cost_usd)
    db.add(rev)
    run.actual_cost = round(run.actual_cost + res.cost_usd, 6)
    record(db, user.id, res.cost_usd, "revision", run.id, f"revision preview · {res.model}")
    db.commit()
    return revision_out(rev)


@app.get("/api/runs/{run_id}/revisions")
def list_revisions(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_run(db, user, run_id)
    return [revision_out(r) for r in db.scalars(select(Revision).where(Revision.run_id == run_id)
                                                .order_by(Revision.id.desc()))]


@app.post("/api/runs/{run_id}/revisions/{revision_id}/apply")
def apply_revision(run_id: int, revision_id: int, body: NotesIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    run = owned_run(db, user, run_id)
    _revisable(run)
    rev = db.get(Revision, revision_id)
    if rev is None or rev.run_id != run_id:
        raise HTTPException(404, "Revision not found.")
    if rev.status != "proposed":
        raise HTTPException(409, f"Revision is already {rev.status}.")
    if rev.target_excerpt not in run.draft_markdown:
        raise HTTPException(409, "The passage has changed since this revision was drafted; preview it again.")
    run.draft_markdown = run.draft_markdown.replace(rev.target_excerpt, rev.replacement_markdown, 1)
    rev.status, rev.applied_at, rev.notes = "applied", utcnow(), body.notes
    paper = db.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.name == "paper.md"))
    if paper is not None:
        Path(paper.path).write_text(run.draft_markdown)
        paper.size_bytes = len(run.draft_markdown.encode())
    project = db.get(Project, run.project_id)
    if project.status == "exported":
        project.status = "review"  # the signed package no longer matches the draft
    audit(db, user, "revision.apply", run.project_id, body.notes, run_id=run_id, revision_id=rev.id)
    db.commit()
    return run_out(db, run)


# ---- campaigns -----------------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/campaigns")
def list_campaigns(project_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_project(db, user, project_id)
    return [campaign_out(c) for c in db.scalars(select(Campaign).where(Campaign.project_id == project_id)
                                                .order_by(Campaign.id.desc()))]


@app.post("/api/projects/{project_id}/campaigns")
async def start_campaign(project_id: int, body: CampaignIn, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> dict:
    p = owned_project(db, user, project_id)
    text_client_or_400(db, user)
    ok, reason = execute_ready(db, user)
    if not ok:
        raise HTTPException(400, reason)
    require_cap(db, user)
    if db.scalar(select(Campaign).where(Campaign.project_id == p.id, Campaign.status.in_(["queued", "running"]))):
        raise HTTPException(409, "A campaign is already running for this project; stop it first.")
    has_program = any(n.endswith((".py", ".ipynb")) for n in pipeline.list_uploads(p.id))
    brief = latest_brief(db, p.id)
    if not has_program and not (brief and brief.is_approved) and not p.idea_text:
        raise HTTPException(400, "Upload a program that prints `SCORE: <float>`, or approve a brief to generate one.")
    budget = {k: float(v) for k, v in (body.budget or {}).items() if v is not None}
    max_iters = int(body.max_iters or budget.get("max_iters") or 20)
    budget["max_iters"] = max_iters
    config = {
        "budget": budget,
        "max_iters": max_iters,
        "explore": max(0.0, min(1.0, body.explore if body.explore is not None else 0.3)),
        "edit_families": body.edit_families or {f: 1.0 for f in search.DEFAULT_FAMILIES},
        "stopping": body.stopping or {},
        **({"entrypoint": body.entrypoint} if body.entrypoint else {}),
    }
    c = Campaign(project_id=p.id, owner_id=user.id, name=body.name or f"{p.title} · search", status="queued",
                 config=config, spend={"iters": 0, "wall_clock_minutes": 0, "model_calls": 0, "gpu_hours": 0, "usd": 0})
    db.add(c)
    db.flush()
    audit(db, user, "campaign.start", p.id, campaign_id=c.id, budget=budget)
    db.commit()
    spawn(f"campaign:{c.id}", search.run_campaign(c.id))
    return campaign_out(c)


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return campaign_out(owned_campaign(db, user, campaign_id))


@app.post("/api/campaigns/{campaign_id}/stop")
def stop_campaign(campaign_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    c = owned_campaign(db, user, campaign_id)
    if c.status not in {"queued", "running"}:
        raise HTTPException(409, f"Campaign is already {c.status}.")
    c.stop_requested = True
    if f"campaign:{campaign_id}" not in tasks:
        c.status, c.ended_at = "stopped", utcnow()
    audit(db, user, "campaign.stop", c.project_id, campaign_id=c.id)
    db.commit()
    return campaign_out(c)


@app.get("/api/campaigns/{campaign_id}/candidates")
def list_candidates(campaign_id: int, sort: str = "", filter: str = "", user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> list[dict]:
    owned_campaign(db, user, campaign_id)
    q = select(Candidate).where(Candidate.campaign_id == campaign_id)
    if filter in {"kept", "discarded", "invalid", "running", "queued"}:
        q = q.where(Candidate.status == filter)
    elif filter:
        q = q.where(Candidate.edit_family == filter)
    rows = list(db.scalars(q))
    if sort in {"score", "-score"}:
        rows.sort(key=lambda c: (c.score is None, -(c.score or 0)))
    elif sort == "cost":
        rows.sort(key=lambda c: (c.cost_vector or {}).get("usd", 0))
    else:
        rows.sort(key=lambda c: c.id)
    return [candidate_out(c) for c in rows]


@app.get("/api/candidates/{candidate_id}")
def get_candidate(candidate_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand, _ = owned_candidate(db, user, candidate_id)
    return candidate_out(cand)


@app.get("/api/candidates/{candidate_id}/trace")
def candidate_trace(candidate_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand, _ = owned_candidate(db, user, candidate_id)
    root = Path(cand.trace_dir) if cand.trace_dir else None
    return {"candidate_id": cand.id, "steps": cand.trace or [], "tree": search.trace_tree(root) if root else [],
            "stdout": cand.stdout, "stderr": cand.stderr}


@app.get("/api/candidates/{candidate_id}/trace/file")
def trace_file(candidate_id: int, path: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand, _ = owned_candidate(db, user, candidate_id)
    if not cand.trace_dir:
        raise HTTPException(404, "No trace directory.")
    root = Path(cand.trace_dir).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "File not found in this trace.")
    raw = target.read_bytes()[:500_000]
    return {"path": path, "content": raw.decode("utf-8", errors="replace")}


@app.get("/api/campaigns/{campaign_id}/leaderboard")
def leaderboard(campaign_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_campaign(db, user, campaign_id)
    return search.leaderboard(list(db.scalars(select(Candidate).where(Candidate.campaign_id == campaign_id))))


@app.get("/api/campaigns/{campaign_id}/frontier")
def frontier(campaign_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    owned_campaign(db, user, campaign_id)
    return search.frontier(list(db.scalars(select(Candidate).where(Candidate.campaign_id == campaign_id))))


def _decide(db: Session, user: User, candidate_id: int, decision: str, reason: str) -> dict:
    cand, camp = owned_candidate(db, user, candidate_id)
    if cand.status in {"running", "queued"}:
        raise HTTPException(409, "Candidate is still being evaluated.")
    if decision == "promoted" and cand.status == "invalid":
        raise HTTPException(409, f"Invalid candidates ({cand.failure_kind}) cannot be promoted.")
    a = dict(cand.audit or {})
    a.update({"decision": decision, "reviewed_by": user.name or user.email, "reviewed_at": utcnow().isoformat(),
              "reason": reason or None})
    cand.audit = a
    audit(db, user, f"candidate.{decision}", camp.project_id, reason, candidate_id=cand.id, campaign_id=camp.id)
    db.commit()
    return candidate_out(cand)


@app.post("/api/candidates/{candidate_id}/promote")
def promote(candidate_id: int, body: NotesIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _decide(db, user, candidate_id, "promoted", body.notes)


@app.post("/api/candidates/{candidate_id}/reject")
def reject(candidate_id: int, body: RejectIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return _decide(db, user, candidate_id, "rejected", body.reason)


@app.get("/api/candidates/{candidate_id}/audit")
def audit_report(candidate_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    cand, _ = owned_candidate(db, user, candidate_id)
    return search.audit_report(cand)


@app.websocket("/api/campaigns/{campaign_id}/trace/ws")
async def campaign_trace_ws(ws: WebSocket, campaign_id: int) -> None:
    with SessionLocal() as db:
        user = user_from_token(ws.query_params.get("token"), db)
        camp = db.get(Campaign, campaign_id)
        if user is None or camp is None or camp.owner_id != user.id:
            await ws.close(code=4401)
            return
        status, iteration = camp.status, camp.iteration
    await ws.accept()
    initial = [{"type": "status", "status": status, "campaign_id": campaign_id, "iteration": iteration,
                "ts": utcnow().isoformat()}]
    if status not in {"queued", "running"}:
        await ws.send_json(initial[0])
        await ws.close()
        return
    await _pump(ws, f"campaign:{campaign_id}", initial=initial)
