"""
pipeline/emit.py

Converts tracker.py output (TrackedPerson, lost_ids) into event dicts
that match the API ingest schema exactly.

Event generation rules:
    ENTRY              -- visitor first seen, is_reentry is False
    REENTRY            -- visitor first seen, is_reentry is True
    EXIT               -- track_id appears in lost_ids
    ZONE_ENTER         -- centroid enters a named zone
    ZONE_DWELL         -- every 30 s of continued presence in same zone
    BILLING_QUEUE_JOIN -- enters billing zone while queue_depth > 0
"""

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

from tracker import TrackedPerson, track_clip
from zones import Zone, ZoneMapper

logger = logging.getLogger(__name__)

_DWELL_INTERVAL_MS = 30_000


def _clip_utc(clip_start: datetime, timestamp_ms: float) -> str:
    ts = clip_start + timedelta(milliseconds=timestamp_ms)
    return ts.isoformat().replace("+00:00", "Z")


def _make_event(
    event_type:  str,
    store_id:    str,
    camera_id:   str,
    visitor_id:  str,
    timestamp:   str,
    confidence:  float,
    zone_id:     str | None = None,
    dwell_ms:    int = 0,
    is_staff:    bool = False,
    session_seq: int = 0,
    queue_depth: int | None = None,
    sku_zone:    str | None = None,
) -> dict:
    return {
        "event_id":   str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp":  timestamp,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone":    sku_zone,
            "session_seq": session_seq,
        },
    }


class _VisitorState:
    __slots__ = (
        "visitor_id",
        "session_seq",
        "current_zone_id",
        "current_zone_sku",
        "zone_enter_ms",
        "last_dwell_emit_ms",
    )

    def __init__(self, visitor_id: str) -> None:
        self.visitor_id:         str          = visitor_id
        self.session_seq:        int          = 0
        self.current_zone_id:    str | None   = None
        self.current_zone_sku:   str | None   = None
        self.zone_enter_ms:      float | None = None
        self.last_dwell_emit_ms: float | None = None

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


class EventEmitter:
    def __init__(
        self,
        store_id:    str,
        camera_id:   str,
        clip_start:  datetime,
        zone_mapper: ZoneMapper,
    ) -> None:
        self._store_id    = store_id
        self._camera_id   = camera_id
        self._clip_start  = clip_start
        self._zone_mapper = zone_mapper
        self._billing_ids = zone_mapper.billing_zone_ids

        self._visitors:         dict[str, _VisitorState] = {}
        self._track_to_visitor: dict[int, str]           = {}
        self._queue_depth:      dict[str, int]           = {}

    def _state(self, visitor_id: str) -> _VisitorState:
        if visitor_id not in self._visitors:
            self._visitors[visitor_id] = _VisitorState(visitor_id)
        return self._visitors[visitor_id]

    def _ts(self, timestamp_ms: float) -> str:
        return _clip_utc(self._clip_start, timestamp_ms)

    def _ev(
        self,
        event_type:   str,
        state:        _VisitorState,
        timestamp_ms: float,
        confidence:   float,
        zone:         Zone | None = None,
        dwell_ms:     int = 0,
        queue_depth:  int | None = None,
    ) -> dict:
        return _make_event(
            event_type=event_type,
            store_id=self._store_id,
            camera_id=self._camera_id,
            visitor_id=state.visitor_id,
            timestamp=self._ts(timestamp_ms),
            confidence=confidence,
            zone_id=zone.zone_id if zone else None,
            dwell_ms=dwell_ms,
            session_seq=state.next_seq(),
            queue_depth=queue_depth,
            sku_zone=zone.sku_zone if zone else None,
        )

    def _handle_person(self, p: TrackedPerson) -> Iterator[dict]:
        state = self._state(p.visitor_id)
        self._track_to_visitor[p.track_id] = p.visitor_id

        if state.session_seq == 0:
            event_type = "REENTRY" if p.is_reentry else "ENTRY"
            yield self._ev(event_type, state, p.timestamp_ms, p.confidence)

        cx, cy = p.centroid
        zone = self._zone_mapper.locate(self._camera_id, cx, cy)
        current_zone_id = zone.zone_id if zone else None

        if current_zone_id != state.current_zone_id:
            if zone is not None:
                if zone.is_billing:
                    self._queue_depth[zone.zone_id] = (
                        self._queue_depth.get(zone.zone_id, 0) + 1
                    )
                    depth = self._queue_depth[zone.zone_id]
                    if depth > 1:
                        yield self._ev(
                            "BILLING_QUEUE_JOIN",
                            state, p.timestamp_ms, p.confidence,
                            zone=zone, queue_depth=depth,
                        )
                    else:
                        yield self._ev(
                            "ZONE_ENTER",
                            state, p.timestamp_ms, p.confidence, zone=zone,
                        )
                else:
                    yield self._ev(
                        "ZONE_ENTER",
                        state, p.timestamp_ms, p.confidence, zone=zone,
                    )

            if (
                state.current_zone_id is not None
                and state.current_zone_id in self._billing_ids
            ):
                prev = self._queue_depth.get(state.current_zone_id, 0)
                self._queue_depth[state.current_zone_id] = max(0, prev - 1)

            state.current_zone_id    = current_zone_id
            state.current_zone_sku   = zone.sku_zone if zone else None
            state.zone_enter_ms      = p.timestamp_ms if zone else None
            state.last_dwell_emit_ms = p.timestamp_ms if zone else None

        elif zone is not None and state.last_dwell_emit_ms is not None:
            elapsed_ms = p.timestamp_ms - state.last_dwell_emit_ms
            if elapsed_ms >= _DWELL_INTERVAL_MS:
                dwell_since_enter = int(
                    p.timestamp_ms - (state.zone_enter_ms or p.timestamp_ms)
                )
                yield self._ev(
                    "ZONE_DWELL",
                    state, p.timestamp_ms, p.confidence,
                    zone=zone, dwell_ms=dwell_since_enter,
                )
                state.last_dwell_emit_ms = p.timestamp_ms

    def _handle_exits(
        self,
        lost_ids:     set[int],
        timestamp_ms: float,
        confidence:   float,
    ) -> Iterator[dict]:
        for track_id in lost_ids:
            visitor_id = self._track_to_visitor.pop(track_id, None)
            if visitor_id is None:
                continue
            state = self._visitors.get(visitor_id)
            if state is None:
                continue

            if (
                state.current_zone_id is not None
                and state.current_zone_id in self._billing_ids
            ):
                prev = self._queue_depth.get(state.current_zone_id, 0)
                self._queue_depth[state.current_zone_id] = max(0, prev - 1)

            yield self._ev("EXIT", state, timestamp_ms, confidence)

            state.current_zone_id    = None
            state.current_zone_sku   = None
            state.zone_enter_ms      = None
            state.last_dwell_emit_ms = None

    def process(
        self,
        track_stream: Iterator[tuple[list[TrackedPerson], set[int]]],
    ) -> Iterator[dict]:
        last_ts   = 0.0
        last_conf = 0.5

        for tracked_persons, lost_ids in track_stream:
            if tracked_persons:
                last_ts   = tracked_persons[-1].timestamp_ms
                last_conf = tracked_persons[-1].confidence

            for p in tracked_persons:
                yield from self._handle_person(p)

            if lost_ids:
                yield from self._handle_exits(lost_ids, last_ts, last_conf)


def emit_clip(
    video_path:           Path,
    store_id:             str,
    camera_id:            str,
    clip_start:           datetime,
    layout_path:          Path,
    frame_stride:         int = 1,
    weights:              str = "yolov8n.pt",
    confidence_threshold: float = 0.25,
    device:               str = "",
) -> Iterator[dict]:
    zone_mapper = ZoneMapper.from_layout(layout_path, store_id)
    emitter = EventEmitter(
        store_id=store_id,
        camera_id=camera_id,
        clip_start=clip_start,
        zone_mapper=zone_mapper,
    )
    stream = track_clip(
        video_path=video_path,
        weights=weights,
        confidence_threshold=confidence_threshold,
        frame_stride=frame_stride,
        device=device,
    )
    yield from emitter.process(stream)


if __name__ == "__main__":
    import argparse
    import sys
    import requests

    parser = argparse.ArgumentParser(
        description="Run detection pipeline and emit events to .jsonl and/or API."
    )
    parser.add_argument("video",         type=Path)
    parser.add_argument("--store-id",    required=True)
    parser.add_argument("--camera-id",   required=True)
    parser.add_argument("--layout",      type=Path, required=True)
    parser.add_argument("--clip-start",  required=True)
    parser.add_argument("--output",      type=Path, default=Path("output/events.jsonl"))
    parser.add_argument("--api-url",     default=None)
    parser.add_argument("--batch-size",  type=int, default=500)
    parser.add_argument("--stride",      type=int, default=1)
    parser.add_argument("--weights",     default="yolov8n.pt")
    parser.add_argument("--conf",        type=float, default=0.25)
    parser.add_argument("--device",      default="")
    parser.add_argument("--log-level",   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    clip_start = datetime.fromisoformat(args.clip_start.replace("Z", "+00:00"))
    args.output.parent.mkdir(parents=True, exist_ok=True)

    batch:       list[dict] = []
    total_events = 0
    total_posted = 0

    def _post_batch(b: list[dict]) -> None:
        global total_posted
        if not args.api_url or not b:
            return
        url = f"{args.api_url.rstrip('/')}/events/ingest"
        try:
            resp = requests.post(url, json=b, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            total_posted += result.get("accepted", 0)
            if result.get("rejected", 0):
                logger.warning("ingest_rejected", extra={
                    "rejected": result["rejected"],
                    "errors":   result.get("errors"),
                })
        except Exception as exc:
            logger.error("ingest_post_failed", extra={"error": str(exc)})

    with open(args.output, "w") as fh:
        for event in emit_clip(
            video_path=args.video,
            store_id=args.store_id,
            camera_id=args.camera_id,
            clip_start=clip_start,
            layout_path=args.layout,
            frame_stride=args.stride,
            weights=args.weights,
            confidence_threshold=args.conf,
            device=args.device,
        ):
            fh.write(json.dumps(event) + "\n")
            total_events += 1
            batch.append(event)
            if len(batch) >= args.batch_size:
                _post_batch(batch)
                batch.clear()

    _post_batch(batch)

    msg = f"Done. Events written: {total_events} -> {args.output}"
    if args.api_url:
        msg += f" | Posted to API: {total_posted}"
    print(msg, file=sys.stderr)