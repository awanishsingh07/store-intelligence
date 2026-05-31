import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)

# COCO class index for "person"
_PERSON_CLASS = 0

# YOLOv8 model weights — nano for CPU tractability
_MODEL_WEIGHTS = "yolov8n.pt"


@dataclass(slots=True)
class Detection:
    """
    Single person detection from one video frame.
    Coordinates are absolute pixel values in the original frame resolution.
    """
    frame_number: int
    # Bounding box in [x1, y1, x2, y2] format (top-left, bottom-right)
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float

    @property
    def xyxy(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2], dtype=np.float32)

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def area(self) -> float:
        return self.width * self.height


@dataclass
class FrameDetections:
    """All person detections extracted from a single video frame."""
    frame_number: int
    timestamp_ms: float          # position in the clip in milliseconds
    frame_width: int
    frame_height: int
    detections: list[Detection] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.detections)


def build_detector(
    weights: str = _MODEL_WEIGHTS,
    confidence_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    device: str = "",
) -> YOLO:
    """
    Load YOLOv8n model.

    Args:
        weights:              Model weights path or name (auto-downloaded if missing).
        confidence_threshold: Minimum detection confidence to keep. Low default
                              intentional — per spec, low-confidence detections must
                              not be silently dropped; confidence is passed through
                              so the tracker and emitter can decide.
        iou_threshold:        NMS IoU threshold.
        device:               '' = auto (CUDA if available, else CPU), 'cpu', '0', etc.
    """
    model = YOLO(weights)
    model.overrides["conf"] = confidence_threshold
    model.overrides["iou"] = iou_threshold
    model.overrides["classes"] = [_PERSON_CLASS]
    model.overrides["verbose"] = False
    if device:
        model.overrides["device"] = device
    logger.info(
        "detector_loaded",
        extra={"weights": weights, "conf": confidence_threshold, "device": device or "auto"},
    )
    return model


def _open_capture(video_path: Path) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video file: {video_path}")
    return cap


def iter_detections(
    video_path: Path,
    model: YOLO,
    frame_stride: int = 1,
    max_frames: int | None = None,
) -> Generator[FrameDetections, None, None]:
    """
    Yield FrameDetections for every sampled frame in the video.

    Args:
        video_path:   Path to the video clip.
        model:        Loaded YOLO detector from build_detector().
        frame_stride: Process every Nth frame. 1 = every frame (15fps for
                      1080p clips). 3 = every 3rd frame (~5fps) — faster
                      processing with minimal tracking accuracy loss at
                      walking speeds.
        max_frames:   Stop after this many processed frames (useful for testing).

    Yields:
        FrameDetections — one per sampled frame, even when detections is empty.
        Empty frames are yielded so the tracker can detect gaps (e.g. a person
        momentarily occluded). Callers must not assume detections is non-empty.

    Notes:
        - Only the PERSON class (index 0) is returned; model.overrides already
          filters at inference time.
        - Confidence is passed through at its raw value — never thresholded here.
          The tracker decides what to do with low-confidence detections.
        - frame_stride skips frames by seeking, not by running inference and
          discarding, so it is computationally efficient.
    """
    cap = _open_capture(video_path)

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(
        "video_opened",
        extra={
            "path": str(video_path),
            "fps": fps,
            "resolution": f"{frame_width}x{frame_height}",
            "total_frames": total_frames,
            "frame_stride": frame_stride,
        },
    )

    frame_number = 0
    processed = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_number % frame_stride == 0:
                timestamp_ms = cap.get(cv2.CAP_PROP_POS_MSEC)

                # Run YOLOv8 inference — returns Results list (one item per image)
                results = model(frame, verbose=False)
                result = results[0]

                frame_dets = FrameDetections(
                    frame_number=frame_number,
                    timestamp_ms=timestamp_ms,
                    frame_width=frame_width,
                    frame_height=frame_height,
                )

                if result.boxes is not None and len(result.boxes) > 0:
                    boxes = result.boxes
                    xyxy_all = boxes.xyxy.cpu().numpy()      # (N, 4)
                    conf_all = boxes.conf.cpu().numpy()       # (N,)
                    cls_all  = boxes.cls.cpu().numpy()        # (N,)

                    for i in range(len(boxes)):
                        # Double-check class filter (model.overrides should handle
                        # this, but guard defensively against model version quirks)
                        if int(cls_all[i]) != _PERSON_CLASS:
                            continue

                        x1, y1, x2, y2 = xyxy_all[i]
                        conf = float(conf_all[i])

                        # Clamp to frame bounds — avoids negative coordinates
                        # from detections at image edges after NMS
                        x1 = max(0.0, float(x1))
                        y1 = max(0.0, float(y1))
                        x2 = min(float(frame_width),  float(x2))
                        y2 = min(float(frame_height), float(y2))

                        # Skip degenerate boxes (zero area) that can appear
                        # near frame borders after clamping
                        if x2 <= x1 or y2 <= y1:
                            continue

                        frame_dets.detections.append(
                            Detection(
                                frame_number=frame_number,
                                x1=x1, y1=y1, x2=x2, y2=y2,
                                confidence=conf,
                            )
                        )

                yield frame_dets
                processed += 1

                if max_frames is not None and processed >= max_frames:
                    logger.debug(
                        "max_frames_reached",
                        extra={"processed": processed, "frame_number": frame_number},
                    )
                    break

            frame_number += 1

    finally:
        cap.release()

    logger.info(
        "video_processed",
        extra={
            "path": str(video_path),
            "frames_read": frame_number,
            "frames_processed": processed,
        },
    )


def detect_clip(
    video_path: Path,
    weights: str = _MODEL_WEIGHTS,
    confidence_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    frame_stride: int = 1,
    device: str = "",
    max_frames: int | None = None,
) -> Generator[FrameDetections, None, None]:
    """
    Convenience wrapper: builds detector and yields FrameDetections.
    Use this as the entry point from tracker.py and run.sh.
    """
    model = build_detector(
        weights=weights,
        confidence_threshold=confidence_threshold,
        iou_threshold=iou_threshold,
        device=device,
    )
    yield from iter_detections(
        video_path=video_path,
        model=model,
        frame_stride=frame_stride,
        max_frames=max_frames,
    )


# ---------------------------------------------------------------------------
# CLI entry point — for manual testing of a single clip
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run YOLOv8n person detection on a single video clip."
    )
    parser.add_argument("video", type=Path, help="Path to the video clip")
    parser.add_argument(
        "--weights", default=_MODEL_WEIGHTS,
        help=f"YOLO weights (default: {_MODEL_WEIGHTS})"
    )
    parser.add_argument(
        "--conf", type=float, default=0.25,
        help="Confidence threshold (default: 0.25)"
    )
    parser.add_argument(
        "--iou", type=float, default=0.45,
        help="NMS IoU threshold (default: 0.45)"
    )
    parser.add_argument(
        "--stride", type=int, default=1,
        help="Process every Nth frame (default: 1)"
    )
    parser.add_argument(
        "--device", default="",
        help="Device: '' auto, 'cpu', '0' for GPU (default: auto)"
    )
    parser.add_argument(
        "--max-frames", type=int, default=None,
        help="Stop after N processed frames (default: all)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    total_detections = 0
    total_frames = 0

    for frame_dets in detect_clip(
        video_path=args.video,
        weights=args.weights,
        confidence_threshold=args.conf,
        iou_threshold=args.iou,
        frame_stride=args.stride,
        device=args.device,
        max_frames=args.max_frames,
    ):
        total_frames += 1
        total_detections += frame_dets.count

        if frame_dets.count > 0:
            logger.debug(
                "frame_detections",
                extra={
                    "frame": frame_dets.frame_number,
                    "ts_ms": round(frame_dets.timestamp_ms, 1),
                    "count": frame_dets.count,
                    "confidences": [
                        round(d.confidence, 3) for d in frame_dets.detections
                    ],
                },
            )

    print(
        f"\nDone. Frames processed: {total_frames} | "
        f"Total detections: {total_detections} | "
        f"Avg detections/frame: "
        f"{total_detections / total_frames:.2f}" if total_frames else "0"
    )