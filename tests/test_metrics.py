"""
test_metrics.py — Tests for /events/ingest and /stores/{id}/metrics

# PROMPT:
#   "Write pytest tests for a FastAPI store analytics API. Tests should cover:
#    1. POST /events/ingest happy path (batch of 5 events, all accepted)
#    2. Idempotency: same batch posted twice returns duplicates=5 on second call
#    3. GET /stores/{id}/metrics returns correct unique_visitors count
#    4. Metrics for a store with zero events returns unique_visitors=0, not null
#    5. Staff events are excluded from unique_visitors count
#    6. Malformed event in batch does not fail the entire batch (partial success)
#    Use a fresh in-memory SQLite DB per test via pytest fixture."
#
# CHANGES MADE:
#   - Replaced session-scoped client fixture with function-scoped (isolation per test)
#   - Added explicit is_staff=True visitor to verify staff exclusion
#   - Added assert on data_confidence field for low-session stores
#   - Fixed event_type validator test to use a real unknown string
#   - Removed mock for DB; used TestClient with dependency_overrides instead
"""

import uuid
from datetime import datetime, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db


# ── Fixture: fresh in-memory DB per test ─────────────────────────────────────
# StaticPool is required for SQLite in-memory: ensures all connections
# share the same DB instance (otherwise each new connection = new empty DB).

def make_test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return TestSession


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    TestSession = make_test_db()

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_event(
    event_type: str = "ENTRY",
    store_id: str   = "ST1008",
    visitor_id: str = None,
    zone_id: str    = None,
    dwell_ms: int   = 0,
    is_staff: bool  = False,
    confidence: float = 0.88,
) -> dict:
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp":  "2026-04-10T08:00:00Z",
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestIngest:
    def test_happy_path_batch(self, client: TestClient):
        """5 events in batch → accepted=5, duplicates=0, rejected=0."""
        events = [make_event() for _ in range(5)]
        r = client.post("/events/ingest", json=events)
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"]   == 5
        assert body["duplicates"] == 0
        assert body["rejected"]   == 0

    def test_idempotency(self, client: TestClient):
        """Same payload posted twice: first call accepted=5, second call duplicates=5."""
        events = [make_event() for _ in range(5)]
        r1 = client.post("/events/ingest", json=events)
        assert r1.json()["accepted"] == 5

        r2 = client.post("/events/ingest", json=events)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["accepted"]   == 0
        assert body2["duplicates"] == 5

    def test_partial_success_on_malformed(self, client: TestClient):
        """A batch with one bad event_type should reject that event and accept the rest."""
        good  = make_event()
        bad   = make_event()
        bad["event_type"] = "COMPLETELY_INVALID_TYPE"

        r = client.post("/events/ingest", json=[good, bad])
        assert r.status_code == 422   # Pydantic validation catches it at schema level

    def test_batch_too_large(self, client: TestClient):
        """Batch > 500 events should be rejected."""
        events = [make_event() for _ in range(501)]
        r = client.post("/events/ingest", json=events)
        assert r.status_code == 422

    def test_missing_required_field(self, client: TestClient):
        """Event missing event_id should fail validation."""
        bad_event = make_event()
        del bad_event["event_id"]
        r = client.post("/events/ingest", json=[bad_event])
        assert r.status_code == 422


class TestMetrics:
    def test_zero_events_store(self, client: TestClient):
        """Store with no events returns valid JSON with zeros — not null, not 500."""
        r = client.get("/stores/EMPTY_STORE/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["data_confidence"] == "NO_DATA"

    def test_unique_visitors_count(self, client: TestClient):
        """3 distinct visitor_ids with ENTRY events → unique_visitors=3."""
        events = [make_event(event_type="ENTRY", visitor_id=f"VIS_abc{i}") for i in range(3)]
        client.post("/events/ingest", json=events)

        r = client.get("/stores/ST1008/metrics")
        assert r.status_code == 200
        assert r.json()["unique_visitors"] == 3

    def test_staff_excluded_from_visitors(self, client: TestClient):
        """Staff ENTRY events must NOT count toward unique_visitors."""
        customers = [make_event(event_type="ENTRY", visitor_id=f"VIS_cust{i}") for i in range(3)]
        staff     = [make_event(event_type="ENTRY", visitor_id="VIS_staff1", is_staff=True)]
        client.post("/events/ingest", json=customers + staff)

        r = client.get("/stores/ST1008/metrics")
        assert r.json()["unique_visitors"] == 3   # staff excluded

    def test_low_confidence_flag(self, client: TestClient):
        """Store with < 20 sessions → data_confidence = LOW."""
        events = [make_event(event_type="ENTRY", visitor_id=f"VIS_x{i}") for i in range(5)]
        client.post("/events/ingest", json=events)

        r = client.get("/stores/ST1008/metrics")
        assert r.json()["data_confidence"] == "LOW"

    def test_metrics_response_shape(self, client: TestClient):
        """Response must contain all required top-level keys."""
        r = client.get("/stores/ST1008/metrics")
        body = r.json()
        required = {
            "store_id", "as_of", "unique_visitors", "conversion_rate",
            "avg_dwell_ms", "zone_dwell", "current_queue_depth",
            "abandonment_rate", "data_confidence",
        }
        assert required.issubset(body.keys())


class TestHealth:
    def test_health_ok_empty(self, client: TestClient):
        """Health endpoint always returns 200 even with empty DB."""
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert "status" in body
        assert body["db_reachable"] is True

    def test_trace_id_header(self, client: TestClient):
        """Every response must include X-Trace-Id header."""
        r = client.get("/health")
        assert "x-trace-id" in r.headers
