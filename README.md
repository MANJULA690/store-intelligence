# Store Intelligence API

Real-time retail analytics from CCTV footage — Purplle Tech Challenge 2026, Round 2.

**Store:** Brigade Bangalore (ST1008) · **Pipeline:** YOLOv8n + ByteTrack · **API:** FastAPI + SQLite

---

## Setup in 5 Commands

**Prerequisites:** Docker Desktop, Python 3.12, Git

```bash
# 1. Clone and enter the project
git clone https://github.com/MANJULA690/store-intelligence && cd store-intelligence

# 2. Start the API
docker compose up --build -d

# 3. Install detection pipeline dependencies
pip install ultralytics==8.3.0 supervision==0.24.0 opencv-python-headless==4.10.0.84 \
    pandas python-dateutil httpx

# 4. Run detection + ingest (point CLIPS_DIR at your camera clips)
CLIPS_DIR=/path/to/clips ./pipeline/run.sh

# 5. View live metrics
curl http://localhost:8000/stores/ST1008/metrics | python3 -m json.tool
```

API docs: http://localhost:8000/docs  
Tests: `pytest`

The API is now running. Check it:

- **Metrics:** http://localhost:8000/stores/ST1008/metrics
- **Funnel:** http://localhost:8000/stores/ST1008/funnel
- **Heatmap:** http://localhost:8000/stores/ST1008/heatmap
- **Anomalies:** http://localhost:8000/stores/ST1008/anomalies
- **Health:** http://localhost:8000/health
- **API Docs:** http://localhost:8000/docs

---

## Mandatory Deliverables

| File              | Location              | Description                                      |
| ----------------- | --------------------- | ------------------------------------------------ |
| Event log (JSONL) | `events/output.jsonl` | 552 events, valid JSONL, full schema             |
| README.md         | `README.md`           | This file                                        |
| DESIGN.md         | `docs/DESIGN.md`      | Architecture + AI-Assisted Decisions (5 entries) |
| CHOICES.md        | `docs/CHOICES.md`     | Model, schema, and API architecture decisions    |

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # YOLOv8 + ByteTrack detection script
│   ├── emit.py         # Event schema + state machine per visitor
│   ├── dashboard.py    # Live terminal dashboard (rich)
│   └── run.sh          # One command: process clips → ingest into API
├── app/
│   ├── main.py         # FastAPI entrypoint (all routes + structured logging)
│   ├── models.py       # Pydantic request/response schemas
│   ├── database.py     # SQLite + SQLAlchemy ORM
│   ├── ingestion.py    # Idempotent event ingest (dedup by event_id)
│   ├── metrics.py      # Real-time metric computation
│   ├── funnel.py       # Session-based conversion funnel
│   ├── heatmap.py      # Zone visit frequency + dwell, normalised 0–100
│   ├── anomalies.py    # Queue spike, dead zone, conversion drop, stale feed
│   └── health.py       # DB reachability + stale feed per store
├── tests/
│   ├── test_pipeline.py   # Unit tests for emitter logic (incl. re-entry)
│   ├── test_metrics.py    # Integration tests for ingest + metrics endpoints
│   └── test_anomalies.py  # Tests for funnel, heatmap, anomalies
├── docs/
│   ├── DESIGN.md       # Architecture overview + AI-assisted decisions
│   └── CHOICES.md      # 3 key engineering decisions with full reasoning
├── data/
│   ├── store_config.json       # Zone definitions for ST1008 Brigade Bangalore
│   └── pos_transactions.csv    # POS transaction data (24 orders, April 10 2026)
├── events/
│   ├── output.jsonl            # Detection pipeline output (552 events)
│   └── pos_transactions.jsonl  # Normalised POS events for ingest
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Running the Detection Pipeline

### Full pipeline (detection + ingest)

```bash
CLIPS_DIR=/path/to/clips ./pipeline/run.sh
```

The script:

1. Runs `detect.py` on all 5 camera clips → writes `events/output.jsonl`
2. Waits for the API to be ready
3. Ingests POS transactions via `POST /pos/ingest`
4. Ingests events in batches of 500 via `POST /events/ingest`

### Detection only (no API required)

```bash
python pipeline/detect.py \
  --clips-dir /path/to/clips \
  --config    data/store_config.json \
  --output    events/output.jsonl \
  --model     yolov8n.pt \
  --every-n   2
```

- First run downloads `yolov8n.pt` automatically (~6 MB)
- `--every-n 2` processes every 2nd frame (recommended for speed)
- Output is newline-delimited JSON, one event per line

### Ingest manually

```bash
python -c "
import json, httpx
events = [json.loads(l) for l in open('events/output.jsonl') if l.strip()]
r = httpx.post('http://localhost:8000/events/ingest', json=events[:500], timeout=30)
print(r.json())
"
```

---

## Event Log Schema

The JSONL event file (`events/output.jsonl`) follows this schema per line:

```json
{
  "event_id": "uuid-v4",
  "store_id": "ST1008",
  "camera_id": "CAM_ZONE_01",
  "visitor_id": "VIS_810a3a",
  "event_type": "ZONE_ENTER",
  "timestamp": "2026-04-10T07:30:00Z",
  "zone_id": "FRAGRANCE",
  "dwell_ms": 0,
  "is_staff": false,
  "confidence": 0.846,
  "metadata": {
    "queue_depth": null,
    "sku_zone": "PERFUME",
    "session_seq": 1
  }
}
```

**Supported `event_type` values:** `ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`

**Re-entry note:** The provided clips are ~2.5 min each. No visitor re-entered within the 60-second re-entry window, so zero `REENTRY` events appear in the output. The detection logic is implemented in `pipeline/emit.py` and covered in `tests/test_pipeline.py`.

---

## API Reference

### POST /events/ingest

Accepts a JSON array of up to 500 events. Idempotent by `event_id`.

```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '[...events array...]'
```

Response:

```json
{ "accepted": 142, "duplicates": 0, "rejected": 0, "errors": [] }
```

### GET /stores/{store_id}/metrics

```bash
curl http://localhost:8000/stores/ST1008/metrics
```

```json
{
  "store_id": "ST1008",
  "as_of": "2026-04-10T16:30:00Z",
  "unique_visitors": 23,
  "conversion_rate": 0.391,
  "avg_dwell_ms": 45200,
  "zone_dwell": [
    { "zone_id": "SKINCARE", "avg_dwell_ms": 62000, "visit_count": 14 }
  ],
  "current_queue_depth": 2,
  "abandonment_rate": 0.087,
  "data_confidence": "HIGH"
}
```

### GET /stores/{store_id}/funnel

```json
{
  "stages": [
    { "stage": "Entry", "count": 23, "drop_off_pct": 0.0 },
    { "stage": "Zone Visit", "count": 19, "drop_off_pct": 17.4 },
    { "stage": "Billing Queue", "count": 10, "drop_off_pct": 47.4 },
    { "stage": "Purchase", "count": 9, "drop_off_pct": 10.0 }
  ]
}
```

### GET /stores/{store_id}/anomalies

```json
{
  "anomalies": [
    {
      "anomaly_type": "BILLING_QUEUE_SPIKE",
      "severity": "HIGH",
      "description": "Billing queue depth is 4 — exceeds threshold of 3.",
      "suggested_action": "Open additional billing counter or deploy staff to billing area."
    }
  ]
}
```

**Anomaly types:** `BILLING_QUEUE_SPIKE` (queue ≥ 3), `DEAD_ZONE` (no visits in 30 min), `CONVERSION_DROP` (>30% below baseline), `STALE_FEED` (no events in 10 min)

### GET /stores/{store_id}/heatmap

Returns zone visit frequency and average dwell, each normalised 0–100 relative to the busiest zone.

### GET /health

```json
{ "status": "ok", "db": "reachable", "stale_feeds": [] }
```

---

## Running Tests

```bash
pip install pytest httpx pytest-cov
pytest
```

Coverage report is displayed automatically. Target: ≥70%.

---

## Environment Variables

| Variable    | Default                 | Description                       |
| ----------- | ----------------------- | --------------------------------- |
| `DB_PATH`   | `store_intelligence.db` | SQLite file path                  |
| `CLIPS_DIR` | `.`                     | Directory containing camera clips |
| `API_URL`   | `http://localhost:8000` | API base URL for `run.sh`         |
| `EVERY_N`   | `2`                     | Frame skip ratio for detection    |

---

## Production Migration Path

| Scale                       | Storage                    | Change Required                                 |
| --------------------------- | -------------------------- | ----------------------------------------------- |
| Current (1 store, batch)    | SQLite                     | None                                            |
| 5–10 stores, near-real-time | PostgreSQL                 | Change `DATABASE_URL` in `database.py`          |
| 40 stores, live             | PostgreSQL + Redis Streams | Add streaming consumer; Redis cache for metrics |

---

## Live Dashboard (Part E)

A real-time terminal dashboard using the `rich` library shows live metrics as events flow in:

```bash
pip install rich
python pipeline/dashboard.py --store ST1008 --api http://localhost:8000
```

Displays: visitor count, conversion rate, queue depth, active anomalies — refreshed every 5 seconds.

Source: `pipeline/dashboard.py`
