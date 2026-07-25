"""Card detection — port of the C# Services/CardDetector.cs.

Finds a Magic card in a frame via contour detection, then perspective-warps it upright.
MTG cards are 63 x 88 mm (short/long ratio ~0.716); output is a fixed 488 x 680 canvas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np

CARD_WIDTH = 488
CARD_HEIGHT = 680


@dataclass
class DetectionResult:
    """Result of locating a card within a video frame."""

    found: bool
    #: Four corners in the original frame, for overlay drawing.
    quad: Optional[np.ndarray] = None
    #: Perspective-corrected upright card image (BGR).
    warped: Optional[np.ndarray] = None
    #: Fraction of the frame area occupied by the card.
    area_fraction: float = 0.0


class CardDetector:
    def __init__(self, min_area_fraction: float = 0.06) -> None:
        #: Minimum fraction of the frame the card must fill to be considered.
        self.min_area_fraction = min_area_fraction

    def detect(self, frame_bgr: np.ndarray) -> DetectionResult:
        if frame_bgr is None or frame_bgr.size == 0:
            return DetectionResult(found=False)

        h, w = frame_bgr.shape[:2]
        frame_area = float(w * h)

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)
        # Close small gaps so the card outline forms one continuous contour.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        edges = cv2.dilate(edges, kernel)

        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_quad: Optional[np.ndarray] = None
        best_area = 0.0

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < frame_area * self.min_area_fraction:
                continue

            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue

            quad = approx.reshape(4, 2).astype(np.float64)
            if not _aspect_looks_like_card(quad):
                continue

            if area > best_area:
                best_area = area
                best_quad = quad

        if best_quad is None:
            return DetectionResult(found=False)

        ordered = _order_corners(best_quad)
        warped = _warp_to_card(frame_bgr, ordered)
        return DetectionResult(
            found=True,
            quad=best_quad.astype(np.int32),
            warped=warped,
            area_fraction=best_area / frame_area,
        )


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def _aspect_looks_like_card(quad: np.ndarray) -> bool:
    o = _order_corners(quad)
    width = (_distance(o[0], o[1]) + _distance(o[3], o[2])) / 2.0
    height = (_distance(o[0], o[3]) + _distance(o[1], o[2])) / 2.0
    if width < 1 or height < 1:
        return False
    # Use long/short so the card may be held in portrait or landscape.
    ratio = min(width, height) / max(width, height)  # card ~0.716
    return 0.55 < ratio < 0.85


def _order_corners(points: np.ndarray) -> np.ndarray:
    """Order corners as [top-left, top-right, bottom-right, bottom-left]."""
    total = points.sum(axis=1)
    diff = points[:, 1] - points[:, 0]
    return np.array([
        points[np.argmin(total)],  # smallest x+y
        points[np.argmin(diff)],   # smallest y-x
        points[np.argmax(total)],  # largest x+y
        points[np.argmax(diff)],   # largest y-x
    ], dtype=np.float32)


def _warp_to_card(src: np.ndarray, ordered: np.ndarray) -> np.ndarray:
    """Warp the quad to an upright card canvas, rotating if held sideways."""
    width = (_distance(ordered[0], ordered[1]) + _distance(ordered[3], ordered[2])) / 2.0
    height = (_distance(ordered[0], ordered[3]) + _distance(ordered[1], ordered[2])) / 2.0
    landscape = width > height

    dst_w = CARD_HEIGHT if landscape else CARD_WIDTH
    dst_h = CARD_WIDTH if landscape else CARD_HEIGHT

    dst = np.array([
        [0, 0],
        [dst_w - 1, 0],
        [dst_w - 1, dst_h - 1],
        [0, dst_h - 1],
    ], dtype=np.float32)

    transform = cv2.getPerspectiveTransform(ordered.astype(np.float32), dst)
    warped = cv2.warpPerspective(src, transform, (dst_w, dst_h))

    if landscape:
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)
    return warped
