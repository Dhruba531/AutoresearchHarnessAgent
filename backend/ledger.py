"""Spend accounting and the monthly cap."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SpendEvent


def month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def record(db: Session, user_id: int, amount: float, source: str, ref_id: int | None = None, detail: str = "") -> None:
    if amount <= 0:
        return
    db.add(SpendEvent(user_id=user_id, amount_usd=round(amount, 6), source=source, ref_id=ref_id, detail=detail[:255]))


def monthly_spend(db: Session, user_id: int) -> float:
    total = db.scalar(
        select(func.coalesce(func.sum(SpendEvent.amount_usd), 0.0)).where(
            SpendEvent.user_id == user_id, SpendEvent.created_at >= month_start()
        )
    )
    return float(total or 0.0)


def usage(db: Session, user_id: int) -> dict:
    spent = monthly_spend(db, user_id)
    cap = settings.monthly_cap_usd
    return {
        "monthly_spend_usd": round(spent, 4),
        "monthly_cap_usd": cap,
        "remaining_usd": round(max(0.0, cap - spent), 4),
        "over_budget": spent >= cap,
    }


class BudgetExceeded(RuntimeError):
    pass
