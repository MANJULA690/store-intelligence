#!/usr/bin/env bash
# run.sh — One command to process all clips and ingest events into the API
# Usage: ./pipeline/run.sh [--clips-dir /path/to/clips] [--api http://localhost:8000]

set -e

CLIPS_DIR="${CLIPS_DIR:-./clips}"
API_URL="${API_URL:-http://localhost:8000}"
CONFIG="data/store_config.json"
EVENTS_FILE="events/output.jsonl"
POS_FILE="events/pos_transactions.jsonl"
MODEL="yolov8n.pt"
EVERY_N="${EVERY_N:-2}"

# Parse CLI args
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --clips-dir) CLIPS_DIR="$2"; shift ;;
        --api)       API_URL="$2"; shift ;;
        --model)     MODEL="$2"; shift ;;
        --every-n)   EVERY_N="$2"; shift ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

echo "============================================="
echo "  Store Intelligence — Detection Pipeline"
echo "============================================="
echo "  Clips dir : $CLIPS_DIR"
echo "  Config    : $CONFIG"
echo "  Output    : $EVENTS_FILE"
echo "  API       : $API_URL"
echo "  Model     : $MODEL"
echo "  Frame skip: every $EVERY_N"
echo "============================================="

# ── Step 1: Run detection ──────────────────────────────────────────────────
echo ""
echo "[1/3] Running detection pipeline..."
python pipeline/detect.py \
    --clips-dir "$CLIPS_DIR" \
    --config    "$CONFIG"    \
    --output    "$EVENTS_FILE" \
    --pos-out   "$POS_FILE"  \
    --model     "$MODEL"     \
    --every-n   "$EVERY_N"

echo ""
echo "[1/3] Detection complete. Events: $EVENTS_FILE"

# ── Step 2: Wait for API to be ready ──────────────────────────────────────
echo ""
echo "[2/3] Waiting for API at $API_URL ..."
MAX_WAIT=60
WAITED=0
until curl -sf "$API_URL/health" > /dev/null 2>&1; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: API not ready after ${MAX_WAIT}s. Is it running? (docker compose up)"
        exit 1
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done
echo "[2/3] API is ready."

# ── Step 3: Ingest POS transactions ───────────────────────────────────────
if [ -f "$POS_FILE" ]; then
    echo ""
    echo "[3/4] Ingesting POS transactions..."
    python -c "
import json, httpx, sys
with open('$POS_FILE') as f:
    records = [json.loads(l) for l in f if l.strip()]
r = httpx.post('$API_URL/pos/ingest', json=records, timeout=30)
print(f'  POS ingest: {r.status_code} — {len(records)} transactions')
if r.status_code >= 400:
    print(r.text)
    sys.exit(1)
"
fi

# ── Step 4: Ingest events in batches of 500 ───────────────────────────────
echo ""
echo "[4/4] Ingesting events into API (batches of 500)..."
python -c "
import json, httpx, sys, time

with open('$EVENTS_FILE') as f:
    events = [json.loads(l) for l in f if l.strip()]

BATCH = 500
total  = len(events)
sent   = 0
errors = 0

for i in range(0, total, BATCH):
    batch = events[i:i+BATCH]
    try:
        r = httpx.post('$API_URL/events/ingest', json=batch, timeout=60)
        if r.status_code == 200:
            sent += len(batch)
            print(f'  Batch {i//BATCH + 1}: {len(batch)} events ingested (total={sent})')
        else:
            errors += 1
            print(f'  Batch {i//BATCH + 1}: ERROR {r.status_code} — {r.text[:200]}')
    except Exception as e:
        errors += 1
        print(f'  Batch {i//BATCH + 1}: EXCEPTION {e}')
    time.sleep(0.1)

print(f'Done. {sent}/{total} events ingested. Errors: {errors}')
if errors > 0:
    sys.exit(1)
"

echo ""
echo "============================================="
echo "  All done! Check metrics at:"
echo "  $API_URL/stores/ST1008/metrics"
echo "  $API_URL/stores/ST1008/funnel"
echo "  $API_URL/stores/ST1008/anomalies"
echo "  $API_URL/health"
echo "============================================="
