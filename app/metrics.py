"""
metrics.py — Real-time metric computation for /stores/{id}/metrics.
All values computed directly from DB on each request (no cache).
"""

from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import func, select, distinct
from sqlalchemy.orm import Session

from .database import EventRecord, POSTransaction
from .models import StoreMetrics, ZoneDwell

CONVERSION_WINDOW_SEC = 300   # 5-minute window for POS correlation


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_store_metrics(store_id: str, db: Session) -> StoreMetrics:
    """Compute and return all store metrics for today."""

    # ── 1. Unique visitors (non-staff, ANY event, distinct visitor_id) ─────────
    # Count all distinct visitor_ids — not just ENTRY events — because
    # floor/billing cameras detect customers who may not have crossed
    # the entry threshold in the entry camera frame.
    unique_visitors: int = db.scalar(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            EventRecord.store_id == store_id,
            EventRecord.is_staff == False,
        )
    ) or 0

    # ── 2. POS transactions for this store ────────────────────────────────────
    pos_records = db.execute(
        select(POSTransaction.timestamp, POSTransaction.basket_value_inr).where(
            POSTransaction.store_id == store_id
        )
    ).fetchall()

    # ── 3. Conversion: visitors in BILLING within 5 min before each transaction
    converted_visitors: set[str] = set()
    billing_events = db.execute(
        select(EventRecord.visitor_id, EventRecord.timestamp).where(
            EventRecord.store_id  == store_id,
            EventRecord.zone_id   == "BILLING",
            EventRecord.is_staff  == False,
        )
    ).fetchall()

    # Build a simple lookup: visitor_id → list of billing timestamps
    billing_times: dict[str, list[datetime]] = {}
    for vid, ts_str in billing_events:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            billing_times.setdefault(vid, []).append(dt)
        except Exception:
            continue

    for pos_ts_str, _ in pos_records:
        try:
            pos_dt = datetime.fromisoformat(pos_ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        window_start = pos_dt - timedelta(seconds=CONVERSION_WINDOW_SEC)
        for vid, times in billing_times.items():
            for bt in times:
                if window_start <= bt <= pos_dt:
                    converted_visitors.add(vid)
                    break

    conversion_rate = min(1.0, (
        len(converted_visitors) / unique_visitors
        if unique_visitors > 0 else 0.0
    ))

    # ── 4. Average dwell per zone (from ZONE_DWELL events, non-staff) ─────────
    zone_rows = db.execute(
        select(
            EventRecord.zone_id,
            func.avg(EventRecord.dwell_ms).label("avg_dwell"),
            func.count(EventRecord.id).label("cnt"),
        ).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type.in_(["ZONE_DWELL", "ZONE_EXIT"]),
            EventRecord.zone_id   != None,
            EventRecord.is_staff  == False,
        ).group_by(EventRecord.zone_id)
    ).fetchall()

    zone_dwell_list = [
        ZoneDwell(
            zone_id=row.zone_id,
            avg_dwell_ms=int(row.avg_dwell or 0),
            visit_count=int(row.cnt),
        )
        for row in zone_rows
    ]

    # ── 5. Overall avg dwell (across all zones, non-staff) ────────────────────
    overall_avg_dwell: int = db.scalar(
        select(func.avg(EventRecord.dwell_ms)).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type.in_(["ZONE_DWELL", "ZONE_EXIT"]),
            EventRecord.is_staff  == False,
        )
    ) or 0

    # ── 6. Current queue depth (latest BILLING_QUEUE_JOIN metadata) ───────────
    latest_queue_event = db.execute(
        select(EventRecord.metadata_json).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
        ).order_by(EventRecord.timestamp.desc()).limit(1)
    ).scalar_one_or_none()

    current_queue_depth = 0
    if latest_queue_event:
        import json
        try:
            meta = json.loads(latest_queue_event)
            current_queue_depth = meta.get("queue_depth", 0) or 0
        except Exception:
            pass

    # ── 7. Abandonment rate ───────────────────────────────────────────────────
    queue_joins = db.scalar(
        select(func.count(EventRecord.id)).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
        )
    ) or 0

    abandonments = db.scalar(
        select(func.count(EventRecord.id)).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type == "BILLING_QUEUE_ABANDON",
        )
    ) or 0

    abandonment_rate = (
        abandonments / queue_joins if queue_joins > 0 else 0.0
    )

    # ── 8. Data confidence ────────────────────────────────────────────────────
    if unique_visitors == 0:
        data_confidence = "NO_DATA"
    elif unique_visitors < 20:
        data_confidence = "LOW"
    else:
        data_confidence = "HIGH"

    return StoreMetrics(
        store_id            = store_id,
        as_of               = _now_utc(),
        unique_visitors     = unique_visitors,
        conversion_rate     = round(conversion_rate, 4),
        avg_dwell_ms        = int(overall_avg_dwell),
        zone_dwell          = zone_dwell_list,
        current_queue_depth = current_queue_depth,
        abandonment_rate    = round(abandonment_rate, 4),
        data_confidence     = data_confidence,
    )
