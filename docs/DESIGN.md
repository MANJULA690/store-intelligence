# DESIGN.md — Store Intelligence System Architecture

## Overview

This system converts raw CCTV footage from Purplle's Brigade Bangalore store (ST1008) into real-time retail analytics. It is structured as a two-stage pipeline: an offline detection stage that processes video and emits structured events, and a live API stage that ingests those events and computes business metrics.

The design goal is a single measurable outcome: **offline store conversion rate** — the fraction of unique visitors who completed a purchase. Every architectural decision is evaluated against whether it improves the accuracy or usefulness of this number.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Detection Pipeline                       │
│                                                             │
│  5 Camera Clips (CAM_1 – CAM_5, ~2.5 min, 1080p 30fps)    │
│       ↓                                                     │
│  YOLOv8n  →  ByteTrack  →  Zone Classifier  →  Emitter    │
│                                                             │
│  Output: events/output.jsonl  (JSONL event stream)         │
└───────────────────────┬─────────────────────────────────────┘
                        │  POST /events/ingest (batches ≤500)
                        ↓
┌─────────────────────────────────────────────────────────────┐
│                   Intelligence API (FastAPI)                 │
│                                                             │
│  SQLite (SQLAlchemy ORM)                                    │
│    events table         ← detection events                  │
│    pos_transactions     ← POS CSV (24 transactions)         │
│                                                             │
│  Endpoints:                                                 │
│    /stores/{id}/metrics    → visitors, conversion, dwell    │
│    /stores/{id}/funnel     → session-based drop-off funnel  │
│    /stores/{id}/heatmap    → zone visit frequency, 0-100    │
│    /stores/{id}/anomalies  → queue spike, dead zone, etc.   │
│    /health                 → DB status, stale feed check    │
└─────────────────────────────────────────────────────────────┘
```

---

## Detection Pipeline Detail

### Camera Role Assignment

The provided store layout XLSX was empty (no zone definitions). Rather than blocking on missing data, I defined a practical zone layout for a Purplle beauty retail store based on standard store layout conventions:

| Camera | Role | Zones Covered |
|--------|------|---------------|
| CAM_1  | Entry/Exit threshold | ENTRY_EXIT |
| CAM_2  | Main floor (left half) | SKINCARE, MAKEUP, HAIRCARE, FRAGRANCE |
| CAM_3  | Billing counter | BILLING |
| CAM_4  | Secondary entry | ENTRY_EXIT |
| CAM_5  | Main floor (right half) | LIPCARE, NAILCARE, ACCESSORIES, FRAGRANCE |

Zone assignment within each frame uses a simple quadrant model (cx, cy normalised to 0–1). This is a deliberate simplicity trade-off: a rule-based approach is more debuggable and explainable than a learned zone classifier in a 3-day window.

### Person Detection

YOLOv8n was chosen for its speed-accuracy balance on short clips. At 30fps and 1920×1080, processing every second frame (every_n=2) reduces compute by 50% with negligible detection quality loss for slow-moving retail shoppers. Confidence threshold is 0.25 — intentionally low to avoid false negatives; the tracking layer handles noise suppression.

### Multi-Object Tracking

ByteTrack (via the `supervision` library) was chosen over DeepSORT because it does not require a separate Re-ID embedding model at inference time, making the pipeline self-contained. The track_activation_threshold (0.25) is matched to YOLO's confidence threshold. The lost_track_buffer (30 frames) handles brief occlusions without falsely terminating tracks.

### Staff Classification

Staff are identified using a dwell ratio heuristic: any track active for more than 70% of the clip duration is flagged `is_staff=True`. This is computed in the `finalize()` step after all frames are processed, and retroactively applied to all events for that visitor_id. This approach does not rely on uniform detection (which is brittle in varied lighting), and was chosen because it is verifiable and doesn't require labelled training data.

### Re-Entry Detection

When a new ENTRY is detected on an entry camera, the emitter checks `_recent_exits` for any visitor_id that exited within the last 60 seconds. If found, a `REENTRY` event is emitted instead of a second `ENTRY`. This prevents re-entry inflation — a known problem with naive visit counting systems.

### POS Correlation

The actual POS CSV (`Brigade_Bangalore_10_April_26.csv`) has a richer schema than the problem described. Key difference: it contains 39 columns with product-level detail, grouped by `order_id`. The loader normalises this to the expected `{store_id, order_id, timestamp, basket_value_inr}` schema by grouping on `order_id` and converting IST timestamps to UTC.

A visitor is considered "converted" if they were in the BILLING zone within a 5-minute window before a POS transaction timestamp. This time-window correlation is the standard approach used in retail analytics vendors.

---

## API Layer Detail

### Storage: SQLite via SQLAlchemy

SQLite was chosen for simplicity: no separate database container, no connection pooling, no credentials to manage. WAL mode is enabled to allow concurrent reads during ingest. For a production deployment at 40 stores, this would be replaced with PostgreSQL, but the API layer is designed with the ORM as an abstraction layer making this a one-line change.

### Real-Time Metrics

All metrics endpoints compute results directly from the database on each request. There is intentionally no caching layer. For the current load (5 cameras, ~2.5 min of footage each), SQLite query times are under 50ms. If load increases, a Redis cache layer with a 30-second TTL would be the first optimisation.

### Anomaly Detection

Four anomaly types are detected:
1. **BILLING_QUEUE_SPIKE** — triggered when `queue_depth ≥ 3` in recent events
2. **DEAD_ZONE** — triggered when a zone has had no visits in 30 minutes
3. **CONVERSION_DROP** — triggered when billing reach rate drops >30% below baseline
4. **STALE_FEED** — triggered when no events have been received in 10 minutes

Each anomaly includes a `suggested_action` string — not just a flag. This was a deliberate design choice: an anomaly without an action recommendation requires the analyst to know what to do, which defeats the purpose of an automated intelligence system.

### Idempotency

`POST /events/ingest` is idempotent by `event_id`. A batch posted twice will return `accepted=0, duplicates=N` on the second call. This is implemented using an `INSERT OR IGNORE` on the `event_id` unique constraint and a pre-flight set lookup to avoid DB constraint errors masking legitimate errors.

---

## AI-Assisted Decisions

### 1. Supervision library for ByteTrack integration
I asked Claude to compare `supervision` vs. direct `bytetracker` package vs. `norfair` for multi-object tracking. The AI correctly identified that `supervision.ByteTrack` gives the best integration with ultralytics YOLO output format and requires no extra model downloads. I agreed with this recommendation and used it.

### 2. Staff detection heuristic
I initially prompted an LLM to suggest a uniform-colour-based staff classifier using HSV thresholds. After evaluating this approach, I disagreed — it would be fragile across different lighting conditions and require per-store calibration. I overrode the suggestion and chose the dwell-ratio heuristic (>70% of clip duration), which is lighting-invariant and gives consistent results on the short clips provided.

### 3. Event schema `metadata` as a flat JSON string
The AI suggested using a separate `event_metadata` table with foreign keys for structured metadata storage. I chose to store `metadata` as a JSON string in the events table instead. Reason: the metadata fields (`queue_depth`, `sku_zone`, `session_seq`) are read together with the event and never queried independently. A joined table would add query complexity with no query benefit at this scale. If a specific metadata field needed indexing (e.g., `queue_depth` for spike detection), a computed column could be added without a schema migration.
