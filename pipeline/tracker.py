import logging
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)

_PERSON_CLASS          = 0
_MODEL_WEIGHTS         = "yolov8n.pt"
_OCCLUSION_GAP_FRAMES  = 30
_REENTRY_WINDOW_FRAMES = 900
_REENTRY_IOU_THRESHOLD = 0.25


@dataclass(slots=True)
class TrackedPerson:
    visitor_id:   str
    track_id:     int
    x1:           float
    y1:           float
    x2:           float
    y2:           float
    confidence:   float
    frame_number: int
    timestamp_ms: float
    is_reentry:   bool = False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def xyxy(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2], dtype=np.float32)


@dataclass
class ExitedVisitor:
    visitor_id: str
    track_id:   int
    last_x1:    float
    last_y1:    float
    last_x2:    float
    last_y2:    float
    exit_frame: int


def _make_visitor_id(clip_path: Path, track_id: int, entry_frame: int) -> str:
    raw = f"{clip_path.stem}:{track_id}:{entry_frame}"
    return f"VIS_{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    ix1 = max(a[0], b[0])
    iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2])
    iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


class ByteTracker:
    def __init__(
        self,
        video_path: Path,
        weights: str = _MODEL_WEIGHTS,
        confidence_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        frame_stride: int = 1,
        device: str = "",
    ) -> None:
        self.video_path   = video_path
        self.frame_stride = frame_stride

        self._model = YOLO(weights)
        self._model.overrides["conf"]    = confidence_threshold
        self._model.overrides["iou"]     = iou_threshold
        self._model.overrides["classes"] = [_PERSON_CLASS]
        self._model.overrides["verbose"] = False
        if device:
            self._model.overrides["device"] = device

        self._id_map:      dict[int, str]       = {}
        self._entry_frame: dict[int, int]       = {}
        self._last_seen:   dict[int, int]       = {}
        self._last_bbox:   dict[int, np.ndarray] = {}
        self._exited:      list[ExitedVisitor]  = []

        logger.info("bytetracker_init", extra={
            "video": str(video_path),
            "conf": confidence_threshold,
            "stride": frame_stride,
        })

    def _match_reentry(self, frame_number: int, bbox: np.ndarray) -> str | None:
        self._exited = [
            e for e in self._exited
            if (frame_number - e.exit_frame) <= _REENTRY_WINDOW_FRAMES
        ]
        best_score = _REENTRY_IOU_THRESHOLD
        best_match = None
        for ex in self._exited:
            ex_bbox = np.array(
                [ex.last_x1, ex.last_y1, ex.last_x2, ex.last_y2], dtype=np.float32
            )
            score = _iou(bbox, ex_bbox)
            if score > best_score:
                best_score = score
                best_match = ex
        if best_match:
            self._exited.remove(best_match)
            return best_match.visitor_id
        return None

    def _resolve(
        self, track_id: int, frame_number: int, bbox: np.ndarray
    ) -> tuple[str, bool]:
        if track_id in self._id_map:
            self._last_seen[track_id] = frame_number
            self._last_bbox[track_id] = bbox
            return self._id_map[track_id], False

        reentry_id = self._match_reentry(frame_number, bbox)
        is_reentry = reentry_id is not None
        visitor_id = reentry_id or _make_visitor_id(
            self.video_path, track_id, frame_number
        )
        self._id_map[track_id]      = visitor_id
        self._entry_frame[track_id] = frame_number
        self._last_seen[track_id]   = frame_number
        self._last_bbox[track_id]   = bbox

        if is_reentry:
            logger.debug("reentry_matched", extra={
                "track_id": track_id, "visitor_id": visitor_id, "frame": frame_number
            })
        return visitor_id, is_reentry

    def _flush_exits(self, active_ids: set[int], frame_number: int) -> set[int]:
        lost: set[int] = set()
        for tid, last_frame in list(self._last_seen.items()):
            if tid in active_ids:
                continue
            if (frame_number - last_frame) > _OCCLUSION_GAP_FRAMES:
                vid  = self._id_map.pop(tid, None)
                bbox = self._last_bbox.pop(tid, np.zeros(4, dtype=np.float32))
                self._entry_frame.pop(tid, None)
                del self._last_seen[tid]
                if vid:
                    self._exited.append(ExitedVisitor(
                        visitor_id=vid, track_id=tid,
                        last_x1=float(bbox[0]), last_y1=float(bbox[1]),
                        last_x2=float(bbox[2]), last_y2=float(bbox[3]),
                        exit_frame=frame_number,
                    ))
                    lost.add(tid)
                    logger.debug("track_exited", extra={
                        "track_id": tid, "visitor_id": vid, "frame": frame_number
                    })
        return lost

    def iter_tracks(
        self,
    ) -> Generator[tuple[list[TrackedPerson], set[int]], None, None]:
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise FileNotFoundError(f"Cannot open video: {self.video_path}")

        fps          = cap.get(cv2.CAP_PROP_FPS) or 15.0
        frame_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        logger.info("tracking_start", extra={
            "video": str(self.video_path),
            "fps": fps,
            "resolution": f"{frame_width}x{frame_height}",
        })

        frame_number = 0

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                if frame_number % self.frame_stride != 0:
                    frame_number += 1
                    continue

                timestamp_ms = cap.get(cv2.CAP_PROP_POS_MSEC)

                results = self._model.track(
                    frame,
                    persist=True,
                    tracker="bytetrack.yaml",
                    verbose=False,
                )
                result = results[0]

                active_ids:      set[int]            = set()
                tracked_persons: list[TrackedPerson] = []

                if (
                    result.boxes is not None
                    and result.boxes.id is not None
                    and len(result.boxes) > 0
                ):
                    boxes    = result.boxes
                    xyxy_all = boxes.xyxy.cpu().numpy()
                    conf_all = boxes.conf.cpu().numpy()
                    cls_all  = boxes.cls.cpu().numpy()
                    ids_all  = boxes.id.cpu().numpy()

                    for i in range(len(boxes)):
                        if int(cls_all[i]) != _PERSON_CLASS:
                            continue

                        track_id = int(ids_all[i])
                        conf     = float(conf_all[i])
                        x1 = max(0.0,             float(xyxy_all[i][0]))
                        y1 = max(0.0,             float(xyxy_all[i][1]))
                        x2 = min(float(frame_width),  float(xyxy_all[i][2]))
                        y2 = min(float(frame_height), float(xyxy_all[i][3]))

                        if x2 <= x1 or y2 <= y1:
                            continue

                        bbox = np.array([x1, y1, x2, y2], dtype=np.float32)
                        active_ids.add(track_id)

                        visitor_id, is_reentry = self._resolve(
                            track_id, frame_number, bbox
                        )

                        tracked_persons.append(TrackedPerson(
                            visitor_id=visitor_id,
                            track_id=track_id,
                            x1=x1, y1=y1, x2=x2, y2=y2,
                            confidence=conf,
                            frame_number=frame_number,
                            timestamp_ms=timestamp_ms,
                            is_reentry=is_reentry,
                        ))

                lost_ids = self._flush_exits(active_ids, frame_number)
                yield tracked_persons, lost_ids
                frame_number += 1

        finally:
            cap.release()
            logger.info("tracking_complete", extra={
                "video": str(self.video_path),
                "total_frames": frame_number,
            })


def track_clip(
    video_path: Path,
    weights: str = _MODEL_WEIGHTS,
    confidence_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    frame_stride: int = 1,
    device: str = "",
) -> Generator[tuple[list[TrackedPerson], set[int]], None, None]:
    tracker = ByteTracker(
        video_path=video_path,
        weights=weights,
        confidence_threshold=confidence_threshold,
        iou_threshold=iou_threshold,
        frame_stride=frame_stride,
        device=device,
    )
    yield from tracker.iter_tracks()

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Run ByteTrack on a video clip and print per-frame tracking output."
    )
    parser.add_argument("video", type=Path, help="Path to video clip")
    parser.add_argument("--weights", default=_MODEL_WEIGHTS)
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--iou", type=float, default=0.45)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--device", default="")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    print(f"{'FRAME':>6}  {'TRACK_ID':>8}  {'VISITOR_ID':<14}  {'CONF':>6}")
    print("-" * 44)

    all_visitor_ids: set[str] = set()

    for tracked_persons, lost_ids in track_clip(
        video_path=Path(args.video),
        weights=args.weights,
        confidence_threshold=args.conf,
        iou_threshold=args.iou,
        frame_stride=args.stride,
        device=args.device,
    ):
        for p in tracked_persons:
            all_visitor_ids.add(p.visitor_id)
            print(
                f"{p.frame_number:>6}  "
                f"{p.track_id:>8}  "
                f"{p.visitor_id:<14}  "
                f"{p.confidence:>6.3f}"
            )

    print("-" * 44)
    print(f"Total unique visitors: {len(all_visitor_ids)}")