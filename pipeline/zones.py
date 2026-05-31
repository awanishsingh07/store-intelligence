import json
import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class Zone:
    zone_id:    str
    sku_zone:   str | None
    is_billing: bool
    polygon:    np.ndarray  # shape (N, 1, 2) float32 — cv2 contour format

    def contains(self, x: float, y: float) -> bool:
        """
        Returns True if point (x, y) is inside or on the polygon boundary.
        Uses cv2.pointPolygonTest — returns >= 0 for inside or on edge.
        """
        result = cv2.pointPolygonTest(self.polygon, (float(x), float(y)), measureDist=False)
        return result >= 0


class ZoneMapper:
    def __init__(self, zones_by_camera: dict[str, list[Zone]]) -> None:
        self._zones_by_camera = zones_by_camera

    @classmethod
    def from_layout(cls, layout_path: Path, store_id: str) -> "ZoneMapper":
        with open(layout_path) as f:
            layout = json.load(f)

        # Support both flat layout {"store_id": ..., "cameras": {...}}
        # and nested layout {store_id: {"cameras": {...}}}
        if "cameras" in layout:
            cameras = layout["cameras"]
        elif store_id in layout:
            entry = layout[store_id]
            cameras = entry.get("cameras", {})
        else:
            raise KeyError(
                f"Cannot find cameras for store '{store_id}' in {layout_path}"
            )

        zones_by_camera: dict[str, list[Zone]] = {}

        for camera_id, zone_list in cameras.items():
            zones: list[Zone] = []
            for z in zone_list:
                raw_polygon = z.get("polygon", [])
                if len(raw_polygon) < 3:
                    logger.warning(
                        "skipping_invalid_polygon",
                        extra={"camera_id": camera_id, "zone_id": z.get("zone_id")},
                    )
                    continue

                # cv2 contour format: (N, 1, 2) float32
                polygon = np.array(raw_polygon, dtype=np.float32).reshape(-1, 1, 2)

                zones.append(Zone(
                    zone_id=z["zone_id"],
                    sku_zone=z.get("sku_zone"),
                    is_billing=z.get("is_billing", False),
                    polygon=polygon,
                ))

            zones_by_camera[camera_id] = zones
            logger.debug(
                "camera_zones_loaded",
                extra={"camera_id": camera_id, "zone_count": len(zones)},
            )

        logger.info(
            "zone_mapper_ready",
            extra={
                "store_id": store_id,
                "layout_path": str(layout_path),
                "camera_count": len(zones_by_camera),
                "total_zones": sum(len(z) for z in zones_by_camera.values()),
            },
        )
        return cls(zones_by_camera)

    def locate(self, camera_id: str, x: float, y: float) -> Zone | None:
        """
        Return the first Zone whose polygon contains point (x, y) for
        the given camera_id. Returns None if no zone matches.

        When polygons overlap, the first matching zone in definition order
        is returned. Define higher-priority zones first in the layout file.
        """
        for zone in self._zones_by_camera.get(camera_id, []):
            if zone.contains(x, y):
                return zone
        return None

    @property
    def billing_zone_ids(self) -> set[str]:
        return {
            zone.zone_id
            for zones in self._zones_by_camera.values()
            for zone in zones
            if zone.is_billing
        }

    @property
    def all_zone_ids(self) -> set[str]:
        return {
            zone.zone_id
            for zones in self._zones_by_camera.values()
            for zone in zones
        }

    def zones_for_camera(self, camera_id: str) -> list[Zone]:
        return list(self._zones_by_camera.get(camera_id, []))