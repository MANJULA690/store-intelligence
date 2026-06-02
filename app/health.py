"""
health.py — /health endpoint.
Returns DB reachability, last event time per store, and STALE_FEED warnings.
This is what an on-call engineer checks first.
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import func, select, distinct, text
from sqlalchemy.orm import Session

from .database import EventRecord
from .models import HealthResponse, StoreHealth

STALE_THRESHOLD_MINUTES = 10


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_health(db: Session) -> HealthResponse:
    now = datetime.now(timezone.utc)

    # ── Check DB reachability ──────────────────────────────────────────────
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False

    if not db_ok:
        return HealthResponse(
            status      = "ERROR",
            db_reachable= False,
            as_of       = _now_utc(),
            stores      = [],
        )

    # ── Per-store last event time ──────────────────────────────────────────
    rows = db.execute(
        select(
            EventRecord.store_id,
            func.max(EventRecord.timestamp).label("last_ts"),
        ).group_by(EventRecord.store_id)
    ).fetchall()

    store_healths: list[StoreHealth] = []
    overall_degraded = False

    for row in rows:
        last_ts_str = row.last_ts
        try:
            last_dt  = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
            lag_sec  = int((now - last_dt).total_seconds())
            is_stale = lag_sec > STALE_THRESHOLD_MINUTES * 60
        except Exception:
            lag_sec  = None
            is_stale = True

        if is_stale:
            overall_degraded = True
        status = "STALE_FEED" if is_stale else "OK"

        store_healths.append(
            StoreHealth(
                store_id      = row.store_id,
                last_event_at = last_ts_str,
                lag_seconds   = lag_sec,
                status        = status,
            )
        )

    # If no stores have data yet
    if not store_healths:
        overall_status = "OK"     # system is running, just no data yet
    elif overall_degraded:
        overall_status = "DEGRADED"
    else:
        overall_status = "OK"

    return HealthResponse(
        status       = overall_status,
        db_reachable = True,
        as_of        = _now_utc(),
        stores       = store_healths,
    )
