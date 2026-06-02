"""
ingestion.py — Event ingest logic: validate, deduplicate by event_id, store.
Idempotent: safe to call twice with same payload.
"""

import json
import logging
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from .database import EventRecord, POSTransaction
from .models import InboundEvent, POSRecord, IngestResponse

logger = logging.getLogger(__name__)


def ingest_events(events: Sequence[InboundEvent], db: Session) -> IngestResponse:
    """
    Insert events into DB. Duplicate event_ids are silently skipped (idempotent).
    Returns counts of accepted / duplicate / rejected.
    """
    accepted   = 0
    duplicates = 0
    rejected   = 0
    errors     = []

    # Fetch already-existing event_ids in one query
    incoming_ids = [e.event_id for e in events]
    existing = set(
        row[0]
        for row in db.execute(
            select(EventRecord.event_id).where(EventRecord.event_id.in_(incoming_ids))
        ).fetchall()
    )

    records_to_insert = []
    for ev in events:
        if ev.event_id in existing:
            duplicates += 1
            continue

        try:
            meta_dict = ev.metadata.model_dump() if ev.metadata else {}
            records_to_insert.append(
                {
                    "event_id":      ev.event_id,
                    "store_id":      ev.store_id,
                    "camera_id":     ev.camera_id,
                    "visitor_id":    ev.visitor_id,
                    "event_type":    ev.event_type,
                    "timestamp":     ev.timestamp,
                    "zone_id":       ev.zone_id,
                    "dwell_ms":      ev.dwell_ms,
                    "is_staff":      ev.is_staff,
                    "confidence":    ev.confidence,
                    "metadata_json": json.dumps(meta_dict),
                }
            )
            existing.add(ev.event_id)   # deduplicate within the same batch
            accepted += 1
        except Exception as exc:
            rejected += 1
            errors.append({"event_id": ev.event_id, "error": str(exc)})
            logger.warning("Failed to prepare event %s: %s", ev.event_id, exc)

    if records_to_insert:
        # Bulk insert using INSERT OR IGNORE for safety
        stmt = insert(EventRecord).prefix_with("OR IGNORE")
        db.execute(stmt, records_to_insert)
        db.commit()

    logger.info(
        "Ingest complete: accepted=%d duplicates=%d rejected=%d",
        accepted, duplicates, rejected,
    )
    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=errors,
    )


def ingest_pos(records: Sequence[POSRecord], db: Session) -> dict:
    """
    Insert POS transactions. Duplicate order_ids are silently skipped.
    """
    accepted   = 0
    duplicates = 0

    incoming_ids = [r.order_id for r in records]
    existing = set(
        row[0]
        for row in db.execute(
            select(POSTransaction.order_id).where(
                POSTransaction.order_id.in_(incoming_ids)
            )
        ).fetchall()
    )

    to_insert = []
    for rec in records:
        if rec.order_id in existing:
            duplicates += 1
            continue
        to_insert.append(
            {
                "store_id":         rec.store_id,
                "order_id":         rec.order_id,
                "timestamp":        rec.timestamp,
                "basket_value_inr": rec.basket_value_inr,
            }
        )
        existing.add(rec.order_id)
        accepted += 1

    if to_insert:
        stmt = insert(POSTransaction).prefix_with("OR IGNORE")
        db.execute(stmt, to_insert)
        db.commit()

    return {"accepted": accepted, "duplicates": duplicates}
