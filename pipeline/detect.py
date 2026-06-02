"""
detect.py — Detection pipeline for Store Intelligence.

Processes each camera clip with YOLOv8 person detection + ByteTrack tracking.
Emits structured events via EventEmitter → events/output.jsonl

Usage:
    python pipeline/detect.py \
        --clips-dir /path/to/clips \
        --config  data/store_config.json \
        --output  events/output.jsonl \
        --model   yolov8n.pt \
        --every-n  2

PROMPT (AI-ASSISTED):
  "Design a CCTV person-detection pipeline using YOLOv8 and ByteTrack from the
   supervision library. Process each camera clip, assign zones based on bounding
   box position, detect entry/exit direction, flag staff by dwell ratio, and
   emit structured events as JSONL. Handle missing frames gracefully."

CHANGES MADE:
  - Added POS-aware clip_start_utc so timestamps align with transaction windows
  - Changed staff threshold from 0.6 to 0.7 after testing on short clips
  - Added every-n frame skipping to trade latency for speed on longer clips
  - Replaced default supervision tracker with explicit ByteTrack params
"""

import argparse
import json
import time
from pathlib import Path

import cv2
from ultralytics import YOLO
import supervision as sv

from emit import EventEmitter, save_events


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def build_zone_sku_map(config: dict) -> dict:
    return {z["zone_id"]: z.get("sku_zone") for z in config.get("zones", [])}


def process_clip(
    video_path: str,
    camera_cfg: dict,
    store_config: dict,
    model: YOLO,
    every_n: int = 2,
) -> list[dict]:
    """Process one camera clip and return all emitted events."""

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[detect] ERROR: Cannot open {video_path}")
        return []

    fps        = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h    = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_ms   = int((total_frames / fps) * 1000)

    print(
        f"[detect] {camera_cfg['camera_id']} | "
        f"{total_frames} frames @ {fps:.1f}fps | "
        f"{frame_w}x{frame_h} | {total_ms/1000:.1f}s"
    )

    zone_sku_map = build_zone_sku_map(store_config)

    emitter = EventEmitter(
        store_id        = store_config["store_id"],
        camera_id       = camera_cfg["camera_id"],
        camera_role     = camera_cfg["role"],
        clip_start_utc  = camera_cfg["clip_start_utc"],
        entry_line_y    = camera_cfg.get("entry_line_y_ratio", 0.5),
        zones_by_region = camera_cfg.get("zones_by_region", {}),
        zone_sku_map    = zone_sku_map,
    )

    tracker = sv.ByteTrack(
        track_activation_threshold=0.25,
        lost_track_buffer=30,
        minimum_matching_threshold=0.8,
        minimum_consecutive_frames=3,
    )

    frame_idx = 0
    t0 = time.time()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1

        # Skip frames for speed (but still count time)
        if frame_idx % every_n != 0:
            continue

        frame_ms = int((frame_idx / fps) * 1000)

        # ── YOLO inference ── class 0 = person, conf threshold 0.25
        results = model.predict(
            frame,
            classes=[0],
            conf=0.25,
            verbose=False,
            imgsz=640,
        )

        # ── Convert to supervision Detections ──
        detections = sv.Detections.from_ultralytics(results[0])

        # ── ByteTrack update ──
        tracked = tracker.update_with_detections(detections)

        # ── Feed each detection to emitter ──
        if len(tracked) > 0:
            for i in range(len(tracked)):
                track_id   = int(tracked.tracker_id[i])
                bbox       = tracked.xyxy[i]                 # [x1,y1,x2,y2]
                confidence = float(tracked.confidence[i]) if tracked.confidence is not None else 0.8

                emitter.update(
                    track_id          = track_id,
                    bbox_xyxy         = bbox,
                    frame_w           = frame_w,
                    frame_h           = frame_h,
                    confidence        = confidence,
                    frame_ms          = frame_ms,
                    total_clip_frames = total_frames,
                )

    cap.release()
    emitter.finalize(total_ms)
    events = emitter.get_events()

    elapsed = time.time() - t0
    print(
        f"[detect] {camera_cfg['camera_id']} done | "
        f"{len(events)} events in {elapsed:.1f}s"
    )
    return events


def ingest_pos(pos_csv_path: str, store_id: str) -> list[dict]:
    """
    Load POS transactions from the Brigade Bangalore CSV.
    Normalises to the simple schema: store_id, order_id, timestamp, basket_value_inr.
    """
    import pandas as pd
    import traceback

    if not Path(pos_csv_path).exists():
        print(f"[detect] WARNING: POS file not found at {pos_csv_path}")
        return []

    try:
        df = pd.read_csv(pos_csv_path)

        # Normalise: the actual CSV has order_id, order_date, order_time, total_amount
        # Group by order_id to get one row per transaction
        if "order_id" in df.columns:
            txn = (
                df.groupby("order_id")
                .agg(
                    order_date    = ("order_date",   "first"),
                    order_time    = ("order_time",   "first"),
                    basket_value  = ("total_amount", "sum"),
                    store_id_col  = ("store_id",     "first"),
                )
                .reset_index()
            )
            records = []
            for _, row in txn.iterrows():
                try:
                    ts = f"{row['order_date']}T{row['order_time']}"
                    # Parse "10-04-2026T16:55:36"
                    from datetime import datetime
                    dt = datetime.strptime(ts, "%d-%m-%YT%H:%M:%S")
                    # Convert IST to UTC (subtract 5:30)
                    from datetime import timedelta
                    dt_utc = dt - timedelta(hours=5, minutes=30)
                    records.append({
                        "store_id":         store_id,
                        "order_id":         str(row["order_id"]),
                        "timestamp":        dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "basket_value_inr": float(row["basket_value"]),
                    })
                except Exception:
                    continue
            print(f"[detect] Loaded {len(records)} POS transactions")
            return records
        else:
            print("[detect] WARNING: Unexpected POS CSV format")
            return []

    except Exception as e:
        print(f"[detect] ERROR loading POS: {e}")
        traceback.print_exc()
        return []


def main():
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--clips-dir", default=".", help="Directory containing camera clips")
    parser.add_argument("--config",    default="data/store_config.json")
    parser.add_argument("--output",    default="events/output.jsonl")
    parser.add_argument("--pos-out",   default="events/pos_transactions.jsonl")
    parser.add_argument("--model",     default="yolov8n.pt")
    parser.add_argument("--every-n",   type=int, default=2, help="Process every Nth frame")
    args = parser.parse_args()

    config = load_config(args.config)
    print(f"[detect] Store: {config['store_id']} — {config['store_name']}")
    print(f"[detect] Processing {len(config['cameras'])} cameras")

    # Load YOLO model (downloads yolov8n.pt on first run)
    print(f"[detect] Loading model: {args.model}")
    model = YOLO(args.model)

    all_events = []

    for cam_cfg in config["cameras"]:
        clip_path = str(Path(args.clips_dir) / cam_cfg["file"])
        if not Path(clip_path).exists():
            print(f"[detect] SKIP: {clip_path} not found")
            continue

        events = process_clip(
            video_path  = clip_path,
            camera_cfg  = cam_cfg,
            store_config= config,
            model       = model,
            every_n     = args.every_n,
        )
        all_events.extend(events)

    # Save all events
    save_events(all_events, args.output)

    # Load and save POS transactions
    pos_path = config.get("pos_file", "data/pos_transactions.csv")
    pos_records = ingest_pos(pos_path, config["store_id"])
    if pos_records:
        Path(args.pos_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.pos_out, "w") as f:
            for r in pos_records:
                f.write(json.dumps(r) + "\n")
        print(f"[detect] POS written → {args.pos_out}")

    print(f"\n[detect] ✓ Done. Total events: {len(all_events)}")
    print(f"[detect] ✓ Output: {args.output}")


if __name__ == "__main__":
    main()
