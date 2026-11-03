"""Request bodies and response serializers.

The response shapes mirror the interfaces in src/lib/api.ts field for field;
change one side and change the other.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (
    Artifact,
    Brief,
    Campaign,
    Candidate,
    Project,
    ProviderKey,
    ReviewFinding,
    Revision,
    Run,
    RunLog,
    User,
    iso,
)
from .security import mask_key, sign_artifact

# ---- request bodies ----------------------------------------------------------


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=8)
    name: str = ""


class ProviderKeyIn(BaseModel):
    provider: Literal["openai", "anthropic", "deepseek", "runpod", "gemini"]
    api_key: str = Field(min_length=8)
    model_name: str = ""
    is_enabled: bool = True


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    objective: str = ""
    idea_text: str = ""


class IdeaIn(BaseModel):
    idea_text: str = Field(min_length=1)


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str


class UploadRunIn(BaseModel):
    endpoint_id: str = ""
    gpu_type: str = "rtx 4090"
    budget_usd: float = 0.25


class BriefPatch(BaseModel):
    content_markdown: str


class NotesIn(BaseModel):
    notes: str = ""


class EstimateIn(BaseModel):
    budget_threshold: float = 0
    config_json: dict[str, Any] = Field(default_factory=dict)


class RunIn(EstimateIn):
    approved_cost: bool = False
    mode: str = "real"
    notes: str = ""


class FinalIn(BaseModel):
    notes: str = ""
    force: bool = False


class RevisionIn(BaseModel):
    instruction: str = Field(min_length=3)
    target_excerpt: str = Field(min_length=1)
    approved_cost: bool = False


class CampaignIn(BaseModel):
    name: str | None = None
    budget: dict[str, float] = Field(default_factory=dict)
    max_iters: int | None = None
    edit_families: dict[str, float] | None = None
    explore: float | None = None
    stopping: dict[str, float] | None = None
    entrypoint: str | None = None


class RejectIn(BaseModel):
    reason: str = Field(min_length=1)


# ---- serializers ---------------------------------------------------------------

INLINE_IMAGE_LIMIT = 1_500_000


def user_out(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def provider_key_out(k: ProviderKey, plain_hint: str | None = None) -> dict:
    return {
        "id": k.id,
        "provider": k.provider,
        "model_name": k.model_name,
        "is_configured": True,
        "is_enabled": k.is_enabled,
        "masked_key": mask_key(plain_hint) if plain_hint else f"…{k.key_last4}",
        "key_last4": k.key_last4,
        "test_status": k.test_status,
        "created_at": iso(k.created_at),
        "updated_at": iso(k.updated_at),
        "last_tested_at": iso(k.last_tested_at),
    }


def brief_out(b: Brief | None) -> dict | None:
    if b is None:
        return None
    return {
        "id": b.id,
        "project_id": b.project_id,
        "version": b.version,
        "content_markdown": b.content_markdown,
        "is_approved": b.is_approved,
        "approval_notes": b.approval_notes,
        "approved_at": iso(b.approved_at),
    }


def artifact_out(a: Artifact) -> dict:
    exp, sig = sign_artifact(a.id)
    out = {
        "id": a.id,
        "kind": a.kind,
        "name": a.name,
        "size_bytes": a.size_bytes,
        "mime_type": a.mime_type,
        # A relative path, so the console prefixes whatever backend base it uses.
        "path": f"/api/artifacts/{a.id}/download?exp={exp}&sig={sig}",
    }
    # Figures render inline, so ship small images as data rather than a link.
    if a.mime_type.startswith("image/") and a.size_bytes <= INLINE_IMAGE_LIMIT:
        p = Path(a.path)
        if p.is_file():
            out["content_base64"] = base64.b64encode(p.read_bytes()).decode()
            out.pop("path")
    return out


def run_out(db: Session, r: Run, with_logs: bool = True) -> dict:
    out: dict[str, Any] = {
        "id": r.id,
        "project_id": r.project_id,
        "status": r.status,
        "mode": r.mode,
        "cost_estimate": round(r.cost_estimate, 4),
        "budget_threshold": r.budget_threshold,
        "actual_cost": round(r.actual_cost, 4),
        "draft_markdown": r.draft_markdown,
        "session_id": r.id,
        "brief_id": r.brief_id,
        "config_json": r.config_json or {},
        "analysis_json": r.analysis_json or {},
        "error": r.error or None,
        "created_at": iso(r.created_at),
        "started_at": iso(r.started_at),
        "ended_at": iso(r.ended_at),
    }
    if with_logs:
        logs = db.scalars(select(RunLog).where(RunLog.run_id == r.id).order_by(RunLog.sequence)).all()
        out["logs"] = [
            {"sequence": l.sequence, "level": l.level, "message": l.message, "created_at": iso(l.created_at)}
            for l in logs
        ]
        arts = db.scalars(select(Artifact).where(Artifact.run_id == r.id).order_by(Artifact.id)).all()
        out["artifacts"] = [artifact_out(a) for a in arts]
    return out


def finding_out(f: ReviewFinding) -> dict:
    return {
        "id": f.id,
        "run_id": f.run_id,
        "category": f.category,
        "severity": f.severity,
        "finding": f.finding,
        "suggested_fix": f.suggested_fix,
        "status": f.status,
    }


def project_out(db: Session, p: Project, detail: bool = True) -> dict:
    latest = db.scalar(select(Run).where(Run.project_id == p.id).order_by(Run.id.desc()).limit(1))
    brief = db.scalar(select(Brief).where(Brief.project_id == p.id).order_by(Brief.version.desc()).limit(1))
    findings: list[ReviewFinding] = []
    if latest is not None:
        findings = list(db.scalars(select(ReviewFinding).where(ReviewFinding.run_id == latest.id).order_by(ReviewFinding.id)))
    return {
        "id": p.id,
        "title": p.title,
        "objective": p.objective,
        "idea_text": p.idea_text,
        "status": p.status,
        "latest_run": run_out(db, latest, with_logs=detail) if latest else None,
        "latest_brief": brief_out(brief),
        "review_findings": [finding_out(f) for f in findings],
        "created_at": iso(p.created_at),
    }


def revision_out(r: Revision) -> dict:
    return {
        "id": r.id,
        "run_id": r.run_id,
        "status": r.status,
        "instruction": r.instruction,
        "target_excerpt": r.target_excerpt,
        "replacement_markdown": r.replacement_markdown,
        "cost_estimate": round(r.cost_estimate, 4),
        "actual_cost": round(r.actual_cost, 4),
        "created_at": iso(r.created_at),
        "applied_at": iso(r.applied_at),
    }


def campaign_out(c: Campaign) -> dict:
    spend = {"iters": 0, "wall_clock_minutes": 0.0, "model_calls": 0, "gpu_hours": 0.0, "usd": 0.0}
    spend.update(c.spend or {})
    return {
        "id": c.id,
        "project_id": c.project_id,
        "name": c.name,
        "status": c.status,
        "iteration": c.iteration,
        "config": c.config or {"budget": {}},
        "spend": spend,
        "best_score": c.best_score,
        "best_candidate_id": c.best_candidate_id,
        "metrics": c.metrics or None,
        "failure_counts": c.failure_counts or None,
        "error": c.error or None,
        "created_at": iso(c.created_at),
        "started_at": iso(c.started_at),
        "ended_at": iso(c.ended_at),
    }


def candidate_out(c: Candidate) -> dict:
    return {
        "id": c.id,
        "campaign_id": c.campaign_id,
        "run_id": None,
        "parent_id": c.parent_id,
        "iteration": c.iteration,
        "edit_family": c.edit_family,
        "status": c.status,
        "score": c.score,
        "cost_vector": c.cost_vector or {},
        "diff": c.diff,
        "metrics": c.metrics or {},
        "failure_kind": c.failure_kind,
        "notes": c.notes or None,
        "trace_dir": c.trace_dir,
        "audit": c.audit or None,
        "created_at": iso(c.created_at),
        "ended_at": iso(c.ended_at),
    }
