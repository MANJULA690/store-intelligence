"""
emit.py — Event schema builder and state machine for each visitor session.
Tracks zone transitions, dwell times, entry/exit, re-entry, and queue depth.
"""

import uuid
import json
import hashlib
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


# ── Event type constants ─────────────────────────────────────────────────────

ENTRY               = "ENTRY"
EXIT                = "EXIT"
ZONE_ENTER          = "ZONE_ENTER"
ZONE_EXIT           = "ZONE_EXIT"
ZONE_DWELL          = "ZONE_DWELL"
BILLING_QUEUE_JOIN  = "BILLING_QUEUE_JOIN"
BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
REENTRY             = "REENTRY"

DWELL_INTERVAL_MS   = 30_000     # emit ZONE_DWELL every 30 seconds


def _make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,
    zone_id: Optional[str],
    dwell_ms: int,
    is_staff: bool,
    confidence: float,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
) -> dict:
    return {
        "event_id":    str(uuid.uuid4()),
        "store_id":    store_id,
        "camera_id":   camera_id,
        "visitor_id":  visitor_id,
        "event_type":  event_type,
        "timestamp":   timestamp,
        "zone_id":     zone_id,
        "dwell_ms":    dwell_ms,
        "is_staff":    is_staff,
        "confidence":  round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone":    sku_zone,
            "session_seq": session_seq,
        },
    }


def _visitor_id_from_track(track_id: int, clip_start_utc: str) -> str:
    raw = f"{clip_start_utc}_{track_id}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:6]
    return f"VIS_{digest}"


@dataclass
class VisitorState:
    visitor_id:         str
    track_id:           int
    is_staff:           bool = False

    # Zone tracking
    current_zone:       Optional[str] = None
    zone_entry_time_ms: int = 0
    last_dwell_emit_ms: int = 0

    # Entry/exit tracking
    entered:            bool = False
    exited:             bool = False

    # Direction detection (for entry cameras)
    last_y_ratio:       float = -1.0   # normalised 0-1
    direction_samples:  list = field(default_factory=list)

    # Session
    session_seq:        int = 0
    first_seen_ms:      int = 0
    last_seen_ms:       int = 0
    total_frames:       int = 0


class EventEmitter:
    """
    One emitter per camera clip. Maintains a state machine for each tracked
    visitor and emits structured events when state transitions occur.
    """

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        camera_role: str,           # 'entry' | 'floor' | 'billing'
        clip_start_utc: str,        # ISO-8601
        entry_line_y: float = 0.5,  # entry cameras only
        zones_by_region: dict = None,
        zone_sku_map: dict = None,
    ):
        self.store_id        = store_id
        self.camera_id       = camera_id
        self.camera_role     = camera_role
        self.clip_start      = datetime.fromisoformat(clip_start_utc.replace("Z", "+00:00"))
        self.entry_line_y    = entry_line_y
        self.zones_by_region = zones_by_region or {}
        self.zone_sku_map    = zone_sku_map or {}

        self._visitors: dict[int, VisitorState] = {}
        self._events:   list[dict] = []
        self._recent_exits: dict[str, int] = {}   # visitor_id → last_seen_ms

        # Queue depth tracking (billing cameras only)
        self._current_queue_depth = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def update(
        self,
        track_id: int,
        bbox_xyxy: tuple,           # (x1, y1, x2, y2) in pixels
        frame_w: int,
        frame_h: int,
        confidence: float,
        frame_ms: int,              # milliseconds from clip start
        total_clip_frames: int,
    ):
        """Call once per tracked detection per frame."""
        x1, y1, x2, y2 = bbox_xyxy
        cx = (x1 + x2) / 2 / frame_w   # normalised [0,1]
        cy = (y1 + y2) / 2 / frame_h

        # First appearance: create state
        if track_id not in self._visitors:
            vid = _visitor_id_from_track(track_id, self.clip_start.isoformat())
            vs = VisitorState(
                visitor_id=vid,
                track_id=track_id,
                first_seen_ms=frame_ms,
                last_seen_ms=frame_ms,
                last_y_ratio=cy,
            )
            self._visitors[track_id] = vs

        vs = self._visitors[track_id]
        vs.last_seen_ms = frame_ms
        vs.total_frames += 1

        # Determine current zone from camera role + position
        zone = self._get_zone(cx, cy)

        # --- Entry camera logic ---
        if self.camera_role == "entry":
            self._handle_entry_camera(vs, cy, zone, confidence, frame_ms)

        # --- Floor camera logic ---
        elif self.camera_role == "floor":
            self._handle_floor_camera(vs, zone, confidence, frame_ms)

        # --- Billing camera logic ---
        elif self.camera_role == "billing":
            self._handle_billing_camera(vs, zone, confidence, frame_ms)

        vs.last_y_ratio = cy
        vs.direction_samples.append(cy)
        if len(vs.direction_samples) > 10:
            vs.direction_samples.pop(0)

    def finalize(self, total_clip_duration_ms: int):
        """Call after all frames processed. Closes open sessions, flags staff."""
        self._classify_staff(total_clip_duration_ms)
        for vs in self._visitors.values():
            if vs.entered and not vs.exited:
                ts = self._ts(total_clip_duration_ms)
                if vs.current_zone:
                    dwell = total_clip_duration_ms - vs.zone_entry_time_ms
                    self._emit(ZONE_EXIT, vs, ts, vs.current_zone, dwell)
                # Only emit EXIT for entry cameras — floor/billing cameras
                # do not track store entry/exit, they track zone presence.
                if self.camera_role == "entry":
                    self._emit(EXIT, vs, ts, None, 0)

    def get_events(self) -> list[dict]:
        return list(self._events)

    # ── Internal: camera role handlers ────────────────────────────────────────

    def _handle_entry_camera(self, vs, cy, zone, confidence, frame_ms):
        ts = self._ts(frame_ms)

        if not vs.entered:
            # Detect inbound crossing: person moves from top → bottom past line
            if vs.last_y_ratio >= 0 and vs.last_y_ratio < self.entry_line_y <= cy:
                # Check re-entry
                reentry = self._check_reentry(vs.visitor_id, frame_ms)
                event_type = REENTRY if reentry else ENTRY
                vs.entered = True
                vs.session_seq += 1
                self._emit(event_type, vs, ts, None, 0, confidence)

        elif vs.entered and not vs.exited:
            # Detect outbound crossing: bottom → top
            if vs.last_y_ratio >= self.entry_line_y > cy:
                vs.exited = True
                self._recent_exits[vs.visitor_id] = frame_ms
                self._emit(EXIT, vs, ts, None, 0, confidence)

    def _handle_floor_camera(self, vs, zone, confidence, frame_ms):
        ts = self._ts(frame_ms)

        # Mark as entered if seen on floor camera (proxy — may have come from entry camera)
        if not vs.entered:
            vs.entered = True
            vs.session_seq += 1
            vs.current_zone = zone
            vs.zone_entry_time_ms = frame_ms
            self._emit(ZONE_ENTER, vs, ts, zone, 0, confidence)
            return

        # Zone transition
        if zone != vs.current_zone:
            if vs.current_zone:
                dwell = frame_ms - vs.zone_entry_time_ms
                self._emit(ZONE_EXIT, vs, ts, vs.current_zone, dwell, confidence)
            vs.current_zone = zone
            vs.zone_entry_time_ms = frame_ms
            vs.last_dwell_emit_ms = frame_ms
            if zone:
                vs.session_seq += 1
                self._emit(ZONE_ENTER, vs, ts, zone, 0, confidence)
        else:
            # Continuous dwell — emit every 30 s
            dwell_so_far = frame_ms - vs.zone_entry_time_ms
            since_last   = frame_ms - vs.last_dwell_emit_ms
            if since_last >= DWELL_INTERVAL_MS and zone:
                vs.session_seq += 1
                self._emit(ZONE_DWELL, vs, ts, zone, dwell_so_far, confidence)
                vs.last_dwell_emit_ms = frame_ms

    def _handle_billing_camera(self, vs, zone, confidence, frame_ms):
        ts = self._ts(frame_ms)

        # Count people in billing as queue depth
        active_count = sum(
            1 for v in self._visitors.values()
            if v.last_seen_ms == frame_ms and not v.is_staff
        )
        self._current_queue_depth = max(0, active_count - 1)

        if not vs.entered:
            vs.entered = True
            vs.current_zone = "BILLING"
            vs.zone_entry_time_ms = frame_ms
            vs.session_seq += 1
            if self._current_queue_depth > 0:
                self._emit(
                    BILLING_QUEUE_JOIN, vs, ts, "BILLING", 0, confidence,
                    queue_depth=self._current_queue_depth
                )
            else:
                self._emit(ZONE_ENTER, vs, ts, "BILLING", 0, confidence)
        else:
            dwell_so_far = frame_ms - vs.zone_entry_time_ms
            since_last   = frame_ms - vs.last_dwell_emit_ms
            if since_last >= DWELL_INTERVAL_MS:
                vs.session_seq += 1
                self._emit(ZONE_DWELL, vs, ts, "BILLING", dwell_so_far, confidence)
                vs.last_dwell_emit_ms = frame_ms

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_zone(self, cx: float, cy: float) -> Optional[str]:
        """Map normalised (cx, cy) to zone_id based on camera role."""
        r = self.zones_by_region
        if not r:
            return None

        if "all" in r:
            return r["all"]

        # 4-quadrant layout
        if "top_left" in r:
            if cy < 0.5:
                return r["top_left"] if cx < 0.5 else r.get("top_right")
            else:
                return r.get("bottom_left") if cx < 0.5 else r.get("bottom_right")

        # Entry camera — just one zone
        return r.get("top") or r.get("bottom")

    def _classify_staff(self, total_ms: int):
        """Flag visitors active for >70% of clip as staff."""
        if total_ms == 0:
            return
        for vs in self._visitors.values():
            active_span = vs.last_seen_ms - vs.first_seen_ms
            if total_ms > 0 and active_span / total_ms > 0.70:
                vs.is_staff = True
                # Retroactively mark all events for this visitor
                for ev in self._events:
                    if ev["visitor_id"] == vs.visitor_id:
                        ev["is_staff"] = True

    def _check_reentry(self, visitor_id: str, frame_ms: int) -> bool:
        for vid, exit_ms in self._recent_exits.items():
            if frame_ms - exit_ms < 60_000:   # within 60 seconds
                return True
        return False

    def _ts(self, frame_ms: int) -> str:
        dt = self.clip_start + timedelta(milliseconds=frame_ms)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _emit(
        self,
        event_type: str,
        vs: VisitorState,
        timestamp: str,
        zone_id: Optional[str],
        dwell_ms: int,
        confidence: float = 0.80,
        queue_depth: Optional[int] = None,
    ):
        ev = _make_event(
            store_id    = self.store_id,
            camera_id   = self.camera_id,
            visitor_id  = vs.visitor_id,
            event_type  = event_type,
            timestamp   = timestamp,
            zone_id     = zone_id,
            dwell_ms    = dwell_ms,
            is_staff    = vs.is_staff,
            confidence  = confidence,
            queue_depth = queue_depth,
            sku_zone    = self.zone_sku_map.get(zone_id) if zone_id else None,
            session_seq = vs.session_seq,
        )
        self._events.append(ev)


def save_events(events: list[dict], output_path: str):
    """Write events as newline-delimited JSON."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    print(f"[emit] Wrote {len(events)} events → {output_path}")


def load_events(path: str) -> list[dict]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events