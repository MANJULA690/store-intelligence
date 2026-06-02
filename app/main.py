"""
main.py — FastAPI entrypoint for the Store Intelligence API.

Endpoints:
  POST  /events/ingest
  POST  /pos/ingest
  GET   /stores/{store_id}/metrics
  GET   /stores/{store_id}/funnel
  GET   /stores/{store_id}/heatmap
  GET   /stores/{store_id}/anomalies
  GET   /health

Production features:
  - Structured JSON logging with trace_id, latency_ms, store_id, status_code
  - Graceful degradation: DB error → 503 with structured body
  - Idempotent ingest (safe to POST same events twice)
  - No raw stack traces in responses
"""

import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import List

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .database import get_db, init_db
from .ingestion import ingest_events, ingest_pos
from .metrics import get_store_metrics
from .funnel import get_funnel
from .heatmap import get_heatmap
from .anomalies import get_anomalies
from .health import get_health
from .models import (
    InboundEvent, IngestResponse, IngestRequest,
    POSRecord,
    StoreMetrics, StoreFunnel, StoreHeatmap, StoreAnomalies,
    HealthResponse,
)

# ── Structured logging setup ──────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger("store_intelligence")


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", msg="Initialising database tables")
    init_db()
    log.info("startup", msg="Store Intelligence API ready")
    yield
    log.info("shutdown", msg="Shutting down")


app = FastAPI(
    title       = "Store Intelligence API",
    description = "Real-time retail analytics from CCTV detection events.",
    version     = "1.0.0",
    lifespan    = lifespan,
)


# ── Request logging middleware ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id   = str(uuid.uuid4())[:8]
    start_time = time.perf_counter()

    request.state.trace_id = trace_id

    try:
        response = await call_next(request)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        log.error(
            "request_error",
            trace_id   = trace_id,
            method     = request.method,
            path       = request.url.path,
            latency_ms = latency_ms,
            error      = str(exc),
        )
        return JSONResponse(
            status_code = 500,
            content     = {"error": "Internal server error", "trace_id": trace_id},
        )

    latency_ms   = int((time.perf_counter() - start_time) * 1000)
    store_id_val = request.path_params.get("store_id", "-")

    log.info(
        "request",
        trace_id     = trace_id,
        method       = request.method,
        path         = request.url.path,
        store_id     = store_id_val,
        status_code  = response.status_code,
        latency_ms   = latency_ms,
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ── DB error handler ──────────────────────────────────────────────────────────

def _handle_db_error(exc: Exception, trace_id: str = "") -> JSONResponse:
    log.error("db_error", error=str(exc), trace_id=trace_id)
    return JSONResponse(
        status_code = 503,
        content     = {
            "error":      "Database unavailable",
            "detail":     "Service is temporarily unable to reach the database.",
            "trace_id":   trace_id,
        },
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post(
    "/events/ingest",
    response_model = IngestResponse,
    summary        = "Ingest a batch of detection events (up to 500)",
)
async def post_ingest_events(
    request: Request,
    payload: list[InboundEvent],
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    if len(payload) > 500:
        raise HTTPException(
            status_code=422,
            detail={"error": "Batch size must be ≤ 500", "received": len(payload)},
        )
    try:
        result = ingest_events(payload, db)
        log.info(
            "ingest",
            trace_id    = trace_id,
            event_count = len(payload),
            accepted    = result.accepted,
            duplicates  = result.duplicates,
            rejected    = result.rejected,
        )
        return result
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)
    except Exception as exc:
        log.error("ingest_error", error=str(exc), trace_id=trace_id)
        raise HTTPException(status_code=500, detail={"error": str(exc), "trace_id": trace_id})


@app.post(
    "/pos/ingest",
    summary = "Ingest POS transaction records",
)
async def post_ingest_pos(
    request: Request,
    payload: list[POSRecord],
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    try:
        result = ingest_pos(payload, db)
        log.info("pos_ingest", trace_id=trace_id, **result)
        return result
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)


@app.get(
    "/stores/{store_id}/metrics",
    response_model = StoreMetrics,
    summary        = "Real-time store metrics: visitors, conversion, dwell, queue",
)
async def get_metrics(
    store_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    try:
        return get_store_metrics(store_id, db)
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)


@app.get(
    "/stores/{store_id}/funnel",
    response_model = StoreFunnel,
    summary        = "Conversion funnel: Entry → Zone → Billing → Purchase",
)
async def get_store_funnel(
    store_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    try:
        return get_funnel(store_id, db)
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)


@app.get(
    "/stores/{store_id}/heatmap",
    response_model = StoreHeatmap,
    summary        = "Zone visit frequency + avg dwell, normalised 0-100",
)
async def get_store_heatmap(
    store_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    try:
        return get_heatmap(store_id, db)
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)


@app.get(
    "/stores/{store_id}/anomalies",
    response_model = StoreAnomalies,
    summary        = "Active anomalies: queue spike, dead zone, conversion drop, stale feed",
)
async def get_store_anomalies(
    store_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    trace_id = getattr(request.state, "trace_id", "-")
    try:
        return get_anomalies(store_id, db)
    except OperationalError as exc:
        return _handle_db_error(exc, trace_id)


@app.get(
    "/health",
    response_model = HealthResponse,
    summary        = "Service health: DB status, last event per store, stale feed warnings",
)
async def health_check(
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        return get_health(db)
    except Exception as exc:
        log.error("health_error", error=str(exc))
        return HealthResponse(
            status       = "ERROR",
            db_reachable = False,
            as_of        = "",
            stores       = [],
        )
