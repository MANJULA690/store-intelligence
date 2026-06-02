"""
anomalies.py — Detect and report operational anomalies.
Severity: INFO / WARN / CRITICAL. Each anomaly includes suggested_action.
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, select, distinct
from sqlalchemy.orm import Session

from .database import EventRecord, POSTransaction
from .models import Anomaly, StoreAnomalies

QUEUE_SPIKE_THRESHOLD    = 5     # persons in billing → CRITICAL
QUEUE_WARN_THRESHOLD     = 3     # → WARN
DEAD_ZONE_MINUTES        = 30    # no visits in N min
STALE_FEED_MINUTES       = 10    # no events in N min
CONVERSION_DROP_THRESHOLD= 0.30  # 30% relative drop vs 7-day avg


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def get_anomalies(store_id: str, db: Session) -> StoreAnomalies:
    anomalies: list[Anomaly] = []
    now = datetime.now(timezone.utc)

    # For batch-processed historical data, use the latest event timestamp
    # as the reference point instead of wall-clock time.
    # This prevents false STALE_FEED / DEAD_ZONE alerts when replaying old clips.
    latest_event_str = db.scalar(
        select(func.max(EventRecord.timestamp)).where(
            EventRecord.store_id == store_id
        )
    )
    if latest_event_str:
        try:
            reference_time = _parse_ts(latest_event_str)
        except Exception:
            reference_time = now
    else:
        reference_time = now

    # ── 1. Queue depth spike ──────────────────────────────────────────────────
    latest_queue = db.execute(
        select(EventRecord.metadata_json, EventRecord.timestamp).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type == "BILLING_QUEUE_JOIN",
        ).order_by(EventRecord.timestamp.desc()).limit(1)
    ).first()

    if latest_queue:
        try:
            meta  = json.loads(latest_queue[0] or "{}")
            depth = int(meta.get("queue_depth", 0) or 0)
            event_ts = _parse_ts(latest_queue[1])
            if (now - event_ts).total_seconds() < 600:  # within last 10 min
                if depth >= QUEUE_SPIKE_THRESHOLD:
                    anomalies.append(Anomaly(
                        anomaly_id       = f"QS-{uuid.uuid4().hex[:8]}",
                        anomaly_type     = "BILLING_QUEUE_SPIKE",
                        severity         = "CRITICAL",
                        description      = f"Billing queue depth is {depth} — exceeds threshold of {QUEUE_SPIKE_THRESHOLD}.",
                        suggested_action = "Open additional billing counter or deploy staff to billing area.",
                        detected_at      = _now_utc(),
                        zone_id          = "BILLING",
                        value            = depth,
                    ))
                elif depth >= QUEUE_WARN_THRESHOLD:
                    anomalies.append(Anomaly(
                        anomaly_id       = f"QS-{uuid.uuid4().hex[:8]}",
                        anomaly_type     = "BILLING_QUEUE_SPIKE",
                        severity         = "WARN",
                        description      = f"Billing queue depth is {depth} — approaching spike threshold.",
                        suggested_action = "Monitor queue — consider pre-emptively directing staff to billing.",
                        detected_at      = _now_utc(),
                        zone_id          = "BILLING",
                        value            = depth,
                    ))
        except Exception:
            pass

    # ── 2. Dead zones (no visits in the last DEAD_ZONE_MINUTES) ──────────────
    cutoff = (reference_time - timedelta(minutes=DEAD_ZONE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Get all zones that have had visits at any point
    all_zones = set(
        row[0]
        for row in db.execute(
            select(distinct(EventRecord.zone_id)).where(
                EventRecord.store_id  == store_id,
                EventRecord.zone_id   != None,
                EventRecord.zone_id.notin_(["ENTRY_EXIT"]),
                EventRecord.is_staff  == False,
            )
        ).fetchall()
    )

    # Get zones with recent activity
    recent_zones = set(
        row[0]
        for row in db.execute(
            select(distinct(EventRecord.zone_id)).where(
                EventRecord.store_id  == store_id,
                EventRecord.zone_id   != None,
                EventRecord.zone_id.notin_(["ENTRY_EXIT"]),
                EventRecord.timestamp >= cutoff,
                EventRecord.is_staff  == False,
            )
        ).fetchall()
    )

    dead_zones = all_zones - recent_zones
    for zone_id in dead_zones:
        anomalies.append(Anomaly(
            anomaly_id       = f"DZ-{uuid.uuid4().hex[:8]}",
            anomaly_type     = "DEAD_ZONE",
            severity         = "INFO",
            description      = f"Zone '{zone_id}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
            suggested_action = f"Check if zone '{zone_id}' is properly stocked and accessible. Consider promotional placement.",
            detected_at      = _now_utc(),
            zone_id          = zone_id,
        ))

    # ── 3. Conversion rate drop vs. 7-day average ─────────────────────────────
    # Simplified: compare today's conversion against a historical average
    # (Since we only have one day of data, we use ratio of billing visitors / total)
    total_visitors = db.scalar(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            EventRecord.store_id  == store_id,
            EventRecord.event_type == "ENTRY",
            EventRecord.is_staff  == False,
        )
    ) or 0

    billing_visitors = db.scalar(
        select(func.count(distinct(EventRecord.visitor_id))).where(
            EventRecord.store_id  == store_id,
            EventRecord.zone_id   == "BILLING",
            EventRecord.is_staff  == False,
        )
    ) or 0

    if total_visitors > 10:
        current_rate = billing_visitors / total_visitors
        # Baseline assumption: typical retail conversion to billing is ~40%
        BASELINE_CONVERSION = 0.40
        if current_rate < BASELINE_CONVERSION * (1 - CONVERSION_DROP_THRESHOLD):
            anomalies.append(Anomaly(
                anomaly_id       = f"CD-{uuid.uuid4().hex[:8]}",
                anomaly_type     = "CONVERSION_DROP",
                severity         = "WARN",
                description      = (
                    f"Current billing reach rate is {current_rate:.1%} "
                    f"(baseline ~{BASELINE_CONVERSION:.0%}). "
                    "More visitors are leaving without reaching the billing counter."
                ),
                suggested_action = "Review floor staff placement. Consider targeted promotions in high-dwell zones.",
                detected_at      = _now_utc(),
                value            = round(current_rate, 4),
            ))

    # ── 4. Stale feed (no events in last 10 minutes of activity window) ───────
    stale_cutoff = (reference_time - timedelta(minutes=STALE_FEED_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    if latest_event_str and latest_event_str < stale_cutoff:
        lag_sec = int((reference_time - _parse_ts(latest_event_str)).total_seconds())
        anomalies.append(Anomaly(
            anomaly_id       = f"SF-{uuid.uuid4().hex[:8]}",
            anomaly_type     = "STALE_FEED",
            severity         = "CRITICAL",
            description      = f"No events received in the last {lag_sec // 60} min {lag_sec % 60} sec.",
            suggested_action = "Check camera connectivity and detection pipeline process.",
            detected_at      = _now_utc(),
            value            = lag_sec,
        ))
    elif not latest_event_str:
        anomalies.append(Anomaly(
            anomaly_id       = f"SF-{uuid.uuid4().hex[:8]}",
            anomaly_type     = "STALE_FEED",
            severity         = "WARN",
            description      = "No events have been received for this store yet.",
            suggested_action = "Run detection pipeline: ./pipeline/run.sh",
            detected_at      = _now_utc(),
            value            = None,
        ))

    return StoreAnomalies(
        store_id  = store_id,
        as_of     = _now_utc(),
        anomalies = anomalies,
    )
