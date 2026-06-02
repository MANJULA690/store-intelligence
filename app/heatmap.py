"""
heatmap.py — Zone visit frequency + avg dwell, normalised 0-100.
"""

from datetime import datetime, timezone
from sqlalchemy import func, select, distinct
from sqlalchemy.orm import Session

from .database import EventRecord
from .models import StoreHeatmap, HeatmapZone


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_heatmap(store_id: str, db: Session) -> StoreHeatmap:
    """
    Compute per-zone visit count + avg dwell, normalise to 0-100.
    Zones with fewer than 20 sessions get data_confidence = LOW.
    """

    rows = db.execute(
        select(
            EventRecord.zone_id,
            func.count(distinct(EventRecord.visitor_id)).label("visit_count"),
            func.avg(EventRecord.dwell_ms).label("avg_dwell"),
        ).where(
            EventRecord.store_id  == store_id,
            EventRecord.zone_id   != None,
            EventRecord.zone_id.notin_(["ENTRY_EXIT"]),
            EventRecord.event_type.in_(["ZONE_ENTER", "ZONE_DWELL", "ZONE_EXIT",
                                        "BILLING_QUEUE_JOIN"]),
            EventRecord.is_staff  == False,
        ).group_by(EventRecord.zone_id)
    ).fetchall()

    if not rows:
        return StoreHeatmap(
            store_id        = store_id,
            as_of           = _now_utc(),
            zones           = [],
            data_confidence = "NO_DATA",
        )

    max_visits = max(r.visit_count for r in rows) or 1
    total_sessions = sum(r.visit_count for r in rows)

    zones = []
    for row in rows:
        score = round((row.visit_count / max_visits) * 100, 1)
        zones.append(
            HeatmapZone(
                zone_id          = row.zone_id,
                visit_count      = int(row.visit_count),
                avg_dwell_ms     = int(row.avg_dwell or 0),
                normalised_score = score,
            )
        )

    zones.sort(key=lambda z: z.normalised_score, reverse=True)

    confidence = "HIGH" if total_sessions >= 20 else "LOW"
    return StoreHeatmap(
        store_id        = store_id,
        as_of           = _now_utc(),
        zones           = zones,
        data_confidence = confidence,
    )
