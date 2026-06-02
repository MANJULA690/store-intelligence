# CHOICES.md — Engineering Decision Log

Three decisions that shaped the system, with full reasoning.

---

## Decision 1: Detection Model — YOLOv8n + ByteTrack

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| YOLOv8n (chosen) | Fast, 6ms/frame on CPU, single-class tuning for person | Lower accuracy than larger variants |
| YOLOv8s | Better small-person detection | ~4× slower; unnecessary for 1080p close-range retail footage |
| RT-DETR | Transformer-based, better occlusion handling | Much heavier; requires GPU for real-time use |
| MediaPipe Pose | Accurate body tracking | Designed for single-person; degrades in crowds |
| VLM (GPT-4V / Claude Vision) | Could classify staff, zones, behaviour in one call | ~2s/frame API latency; not viable for 30fps video |

**What AI suggested:** Claude suggested using YOLOv8s for better detection at billing (where people cluster and partial occlusion occurs). It also suggested using a VLM (specifically GPT-4V with frame sampling) for staff detection and zone classification, noting that a well-prompted VLM could identify store uniforms.

**What I chose and why:** I used YOLOv8n rather than YOLOv8s. The actual clips are ~2.5 minutes each (not 20 minutes as specified), which means the entire dataset is ~12.5 minutes of footage — well within CPU-processable range even with the lighter model. For a production system at real-time inference speed, the latency advantage of YOLOv8n matters.

On the VLM suggestion: I partially agreed. Using a VLM per-frame is not practical at video speed, but I considered it for a key use case — classifying zones in the FLOOR camera, where the spatial layout isn't known from the (empty) store layout file. I decided against it because: (1) a frame-sampling approach at 1 frame/second would add ~90s of API latency per clip at current GPT-4V speeds; (2) my rule-based zone classifier based on bounding box position is deterministic and auditable. If I had 24 hours more, I would use a VLM on a single representative frame per camera to auto-generate the zone-to-quadrant mapping.

---

## Decision 2: Event Schema Design

### The Core Tension

The schema needs to support two conflicting requirements:
1. **Granularity**: Events must capture individual zone transitions at second-level precision to compute dwell and funnel.
2. **Compactness**: Batch ingest must handle up to 500 events efficiently; a fat schema increases ingest time and storage.

### Options Considered

**Option A — One event per state change (chosen):**
Emit a separate event for each transition: ENTRY, ZONE_ENTER, ZONE_EXIT, ZONE_DWELL (every 30s), EXIT. This produces more rows but makes each query trivial — "how many BILLING events?" is a single SQL filter.

**Option B — Session-level summary events:**
Emit one event per visitor that summarises their full session: zones visited, total dwell, zone sequence. Compact, but cannot support real-time anomaly detection (no current-state signal) and cannot be ingested incrementally.

**Option C — Timeseries-style (one row per frame per person):**
Maximum granularity; maximum storage. A 2.5-minute clip at 30fps with 10 people = 4,500 rows. Queries require time-window aggregations, which are expensive without pre-aggregation.

**What AI suggested:** The AI recommended Option B with a supplemental real-time state table for live metrics. This is architecturally sound for a mature system, but adds significant complexity in state management and synchronisation.

**What I chose and why:** Option A. It aligns directly with the required event type catalogue from the problem spec. Each event type answers a specific business question independently. The 30-second ZONE_DWELL periodicity gives temporal resolution without generating a row per frame. This also makes idempotency trivial (deduplicate by event_id) and partial batch failures easy to diagnose.

**Re-entry handling in schema:** The REENTRY event type reuses the same `visitor_id` as the original ENTRY event. This means the funnel deduplication logic needs to count distinct visitor_ids from both ENTRY and REENTRY event types — which is a simple OR filter. I explicitly tested this in `test_anomalies.py::TestFunnel::test_reentry_not_double_counted`.

---

## Decision 3: API Architecture — FastAPI + SQLite vs. Event-Streaming Architecture

### The Scaling Question

The problem mentions 40 live stores sending events in real time. This raises the question: should the API be built around a message queue (Kafka/Redis Streams) with the metrics layer reading from a streaming consumer, or should it be a simple REST API over a relational database?

### Options Considered

| Option | Real-time capability | Complexity | Appropriate for |
|--------|---------------------|------------|-----------------|
| FastAPI + SQLite (chosen) | Adequate for batch + polling | Low | This challenge; ≤5 concurrent stores |
| FastAPI + PostgreSQL | Good; handles concurrent writes | Medium | Production, 5–40 stores |
| FastAPI + Redis Streams | True real-time; sub-second lag | High | 40+ stores, live dashboard |
| FastAPI + Kafka | Enterprise-grade; exactly-once delivery | Very high | Multi-datacenter production |

**What AI suggested:** Claude recommended a Redis Streams architecture with a Lua script for atomic queue depth tracking and a separate metrics aggregator process that consumes the stream and pre-computes metrics into a Redis hash. This is the correct production architecture, and I documented it because it directly answers the "what's the first thing that breaks at 40 stores?" follow-up question.

**What I chose and why:** FastAPI + SQLite with WAL mode. The reasons:
1. The challenge requirement is `docker compose up starts everything` — a Redis container is acceptable, but Kafka is not a reasonable dependency for a single-command startup.
2. The submission is evaluated against the 5 provided camera clips, not against 40 live stores. Optimising for the wrong scale adds code complexity without improving score.
3. SQLite with WAL mode handles concurrent reads cleanly, and the ingest endpoint uses bulk INSERT OR IGNORE which keeps write throughput adequate for batch ingest.

**The honest answer on scale:** At 40 stores × 3 cameras × 30fps × 10 avg persons per frame, the event rate would be roughly 36,000 events/minute. SQLite would saturate at ~5,000 writes/minute under load. The correct migration path is: SQLite → PostgreSQL → Timescale DB with a Redis read-through cache for the metrics endpoints. This migration path is documented in the README.

**On the VLM question for zone classification:**
I considered using a VLM to automatically infer zone layouts from a single frame per camera. A prompt like: *"This is a frame from a beauty retail store's floor camera. Identify the product sections visible in each quadrant and label them by category (skincare, makeup, haircare, etc.)."* would likely produce reasonable zone labels. I chose not to implement this due to time constraints, but I sketched the approach in `data/store_config.json`'s zone definitions — if a VLM were used, it would replace the static `zones_by_region` entries.
