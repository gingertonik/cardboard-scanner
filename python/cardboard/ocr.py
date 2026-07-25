"""Title OCR — port of the C# Services/OcrService.cs.

The Windows version used Windows.Media.Ocr. That API does not exist off Windows, so this
uses RapidOCR (ONNX), which bundles its own models and needs no external binary.

As in the C# version, several contrast-enhanced variants of the title strip are tried and
the best read wins — dim light and foil glare defeat a single pass.
"""

from __future__ import annotations

import re
from typing import Optional

import cv2
import numpy as np


class OcrService:
    def __init__(self) -> None:
        self._engine = None
        #: Why the engine could not start, if it could not. Surfaced rather than swallowed so
        #: a packaging problem (missing models/native libs) is diagnosable instead of silently
        #: degrading the app to image-hash-only matching.
        self.init_error: Optional[str] = None
        try:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
        except Exception as e:
            self._engine = None  # matching falls back to image hashing
            self.init_error = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return self._engine is not None

    def read_title(self, warped_card: np.ndarray) -> str:
        """Crop the title strip from a warped card and OCR it. May return ""."""
        if self._engine is None or warped_card is None or warped_card.size == 0:
            return ""

        gray = _crop_title_strip_gray(warped_card)
        best = ""
        for variant in _enhance_variants(gray):
            text = self._recognize(variant)
            if _score(text) > _score(best):
                best = text
            if _score(best) >= 6:  # good enough — stop early
                break
        return best

    def _recognize(self, image: np.ndarray) -> str:
        try:
            # RapidOCR wants 3-channel input; it returns [[box, text, confidence], ...].
            rgb = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR) if image.ndim == 2 else image
            result, _ = self._engine(rgb)
        except Exception:
            return ""
        if not result:
            return ""
        # Prefer the highest-confidence line; card names are a single-line title.
        best_line = max(result, key=lambda item: float(item[2]) if len(item) > 2 else 0.0)
        return _clean_name(str(best_line[1]))


def _score(text: str) -> int:
    """Letters in the read — a rough "how much real text did we get" score."""
    return sum(1 for ch in text if ch.isalpha())


def _crop_title_strip_gray(card: np.ndarray) -> np.ndarray:
    """Title band sits near the top; the name is on the left, mana cost on the right."""
    h, w = card.shape[:2]
    x0, x1 = int(w * 0.03), int(w * 0.78)
    y0, y1 = int(h * 0.032), int(h * 0.095)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, max(x0 + 1, x1)), min(h, max(y0 + 1, y1))

    crop = card[y0:y1, x0:x1]
    scaled = cv2.resize(crop, ((x1 - x0) * 3, (y1 - y0) * 3), interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY) if scaled.ndim == 3 else scaled


def _enhance_variants(gray: np.ndarray) -> list[np.ndarray]:
    """CLAHE-equalised (boosts contrast in dim light), plus Otsu and inverted-Otsu
    thresholds (for dark text on light, and light text on dark title bars)."""
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    equalized = clahe.apply(gray)
    _, thresh = cv2.threshold(equalized, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    _, inverted = cv2.threshold(equalized, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return [equalized, thresh, inverted]


def _clean_name(raw: str) -> str:
    """Normalise OCR output into a plausible card name."""
    if not raw or not raw.strip():
        return ""
    first_line = raw.replace("\r", " ").split("\n")[0].strip()
    kept = [ch for ch in first_line if ch.isalpha() or ch.isspace() or ch in "'-,./"]
    return re.sub(r"\s+", " ", "".join(kept)).strip()
