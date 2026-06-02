"""
funnel.py — Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
Session is the unit; re-entries do NOT double-count a visitor.
"""

from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, distinct
from sqlalchemy.orm import Session

from .database import EventRecord, POSTransaction
from .models import StoreFunnel, FunnelStage

CONVERSION_WINDOW_SEC = 300


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct_drop(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0
    return round((1 - current / previous) * 100, 1)


def get_funnel(store_id: str, db: Session) -> StoreFunnel:
    """
    Build funnel for the store.
    Each stage counts unique visitors (non-staff) who reached that stage.
    A re-entrant visitor (REENTRY event) counts as ONE unique visitor.
    """

    # ── Stage 1: Unique visitors who entered ─────────────────────────────────
    # Count distinct visitor_ids with ENTRY or REENTRY events
    entered_visitors: set[str] = set(
        row[0]
        for row in db.execute(
            select(distinct(EventRecord.visitor_id)).where(
                EventRecord.store_id  == store_id,
                EventRecord.event_type.in_(["ENTRY", "REENTRY"]),
                EventRecord.is_staff  == False,
            )
        ).fetchall()
    )
    n_entered = len(entered_visitors)

    # ── Stage 2: Visitors who visited at least one product zone ───────────────
    visited_zone: set[str] = set(
        row[0]
        for row in db.execute(
            select(distinct(EventRecord.visitor_id)).where(
                EventRecord.store_id  == store_id,
                EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT"]),
                EventRecord.zone_id.notin_(["ENTRY_EXIT", "BILLING"]),
                EventRecord.is_staff  == False,
            )
        ).fetchall()
        if row[0] in entered_visitors
    )
    n_zone = len(visited_zone)

    # ── Stage 3: Visitors who reached billing queue ───────────────────────────
    billing_visitors: set[str] = set(
        row[0]
        for row in db.execute(
            select(distinct(EventRecord.visitor_id)).where(
                EventRecord.store_id  == store_id,
                EventRecord.zone_id   == "BILLING",
                EventRecord.is_staff  == False,
            )
        ).fetchall()
        if row[0] in entered_visitors
    )
    n_billing = len(billing_visitors)

    # ── Stage 4: Visitors who purchased (POS correlation) ────────────────────
    pos_records = db.execute(
        select(POSTransaction.timestamp).where(
            POSTransaction.store_id == store_id
        )
    ).fetchall()

    billing_events = db.execute(
        select(EventRecord.visitor_id, EventRecord.timestamp).where(
            EventRecord.store_id  == store_id,
            EventRecord.zone_id   == "BILLING",
            EventRecord.is_staff  == False,
        )
    ).fetchall()

    billing_times: dict[str, list[datetime]] = {}
    for vid, ts_str in billing_events:
        if vid not in entered_visitors:
            continue
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            billing_times.setdefault(vid, []).append(dt)
        except Exception:
            continue

    purchased_visitors: set[str] = set()
    for (pos_ts_str,) in pos_records:
        try:
            pos_dt = datetime.fromisoformat(pos_ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        window_start = pos_dt - timedelta(seconds=CONVERSION_WINDOW_SEC)
        for vid, times in billing_times.items():
            for bt in times:
                if window_start <= bt <= pos_dt:
                    purchased_visitors.add(vid)

    n_purchased = len(purchased_visitors)

    # ── Build stages ──────────────────────────────────────────────────────────
    stages = [
        FunnelStage(
            stage       = "Entry",
            count       = n_entered,
            drop_off_pct= 0.0,
        ),
        FunnelStage(
            stage        = "Zone Visit",
            count        = n_zone,
            drop_off_pct = _pct_drop(n_zone, n_entered),
        ),
        FunnelStage(
            stage        = "Billing Queue",
            count        = n_billing,
            drop_off_pct = _pct_drop(n_billing, n_zone),
        ),
        FunnelStage(
            stage        = "Purchase",
            count        = n_purchased,
            drop_off_pct = _pct_drop(n_purchased, n_billing),
        ),
    ]

    note = None
    if n_entered < 20:
        note = f"Low session count ({n_entered}). Metrics may not be statistically reliable."

    return StoreFunnel(
        store_id = store_id,
        as_of    = _now_utc(),
        stages   = stages,
        note     = note,
    )
