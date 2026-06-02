"""
models.py — Pydantic v2 schemas for the Store Intelligence API.
"""

from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


# ── Inbound: event schema from detection pipeline ─────────────────────────────

class EventMetadata(BaseModel):
    queue_depth:  Optional[int]   = None
    sku_zone:     Optional[str]   = None
    session_seq:  int             = 0


class InboundEvent(BaseModel):
    event_id:   str
    store_id:   str
    camera_id:  str
    visitor_id: str
    event_type: str
    timestamp:  str
    zone_id:    Optional[str] = None
    dwell_ms:   int = 0
    is_staff:   bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata:   Optional[EventMetadata] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        allowed = {
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
            "ZONE_DWELL", "BILLING_QUEUE_JOIN",
            "BILLING_QUEUE_ABANDON", "REENTRY",
        }
        if v not in allowed:
            raise ValueError(f"Unknown event_type: {v}")
        return v


class IngestRequest(BaseModel):
    events: list[InboundEvent]

    @field_validator("events")
    @classmethod
    def max_batch(cls, v: list) -> list:
        if len(v) > 500:
            raise ValueError("Batch size must be ≤ 500")
        return v


# ── Inbound: POS transaction ───────────────────────────────────────────────────

class POSRecord(BaseModel):
    store_id:         str
    order_id:         str
    timestamp:        str    # ISO-8601 UTC
    basket_value_inr: float


# ── Outbound: ingest response ─────────────────────────────────────────────────

class IngestResponse(BaseModel):
    accepted:   int
    duplicates: int
    rejected:   int
    errors:     list[dict] = []


# ── Outbound: zone dwell ──────────────────────────────────────────────────────

class ZoneDwell(BaseModel):
    zone_id:      str
    avg_dwell_ms: int
    visit_count:  int


# ── Outbound: /metrics ────────────────────────────────────────────────────────

class StoreMetrics(BaseModel):
    store_id:            str
    as_of:               str          # ISO-8601 UTC
    unique_visitors:     int
    conversion_rate:     float        # 0.0 – 1.0
    avg_dwell_ms:        int          # overall (non-staff)
    zone_dwell:          list[ZoneDwell]
    current_queue_depth: int
    abandonment_rate:    float        # 0.0 – 1.0
    data_confidence:     str          # HIGH | LOW | NO_DATA


# ── Outbound: /funnel ────────────────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage:       str
    count:       int
    drop_off_pct: float   # % who dropped vs previous stage


class StoreFunnel(BaseModel):
    store_id: str
    as_of:    str
    stages:   list[FunnelStage]
    note:     Optional[str] = None


# ── Outbound: /heatmap ───────────────────────────────────────────────────────

class HeatmapZone(BaseModel):
    zone_id:          str
    visit_count:      int
    avg_dwell_ms:     int
    normalised_score: float   # 0 – 100


class StoreHeatmap(BaseModel):
    store_id:         str
    as_of:            str
    zones:            list[HeatmapZone]
    data_confidence:  str   # HIGH | LOW | NO_DATA


# ── Outbound: /anomalies ─────────────────────────────────────────────────────

class Anomaly(BaseModel):
    anomaly_id:       str
    anomaly_type:     str         # BILLING_QUEUE_SPIKE | CONVERSION_DROP | DEAD_ZONE | STALE_FEED
    severity:         str         # INFO | WARN | CRITICAL
    description:      str
    suggested_action: str
    detected_at:      str         # ISO-8601 UTC
    zone_id:          Optional[str] = None
    value:            Optional[Any] = None


class StoreAnomalies(BaseModel):
    store_id:  str
    as_of:     str
    anomalies: list[Anomaly]


# ── Outbound: /health ────────────────────────────────────────────────────────

class StoreHealth(BaseModel):
    store_id:           str
    last_event_at:      Optional[str]
    lag_seconds:        Optional[int]
    status:             str    # OK | STALE_FEED | NO_DATA


class HealthResponse(BaseModel):
    status:             str    # OK | DEGRADED | ERROR
    db_reachable:       bool
    as_of:              str
    stores:             list[StoreHealth]
