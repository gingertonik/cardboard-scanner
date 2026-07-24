"""Perceptual hashing — port of the C# Services/PerceptualHasher.cs.

The Windows app used CoenM.ImageHash's PerceptualHash. Reproducing its exact 64-bit
output was measured to be impossible (see ``tools/hash_parity.py``): CoenM resizes with
ImageSharp's bicubic resampler, which yields different 64x64 pixels than any OpenCV
filter, so the hashes differ structurally (~23 bits average drift) rather than by a
tunable parameter. Hashes from the two implementations are therefore NOT interchangeable
and a Python-built index is required — see ``HASH_ALGO``.

Freed from mimicking CoenM, the defaults below are chosen on quality grounds:
INTER_AREA (correct for downscaling), BT.601 luminance, an orthonormal DCT-II, and the
classic pHash practice of excluding the dominant DC term from the median.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import cv2
import numpy as np

# Art window as fractions of a full, upright card — identical to the C# constants so the
# indexed Scryfall image and the live warped card are cropped the same way. The art hash
# largely ignores the title/type bars and border, so it survives foil glare.
ART_X0, ART_Y0, ART_X1, ART_Y1 = 0.090, 0.110, 0.910, 0.560

HASH_SIZE = 64  # DCT input is 64x64
BLOCK = 8       # top-left 8x8 DCT block provides the 64 hash bits

#: Identifies which implementation produced the hashes in a database's match_index.
#: Stored in meta['index_hash_algo']; a mismatch means the index must be rebuilt rather
#: than trusted, which prevents the C# and Python versions silently mixing hashes.
HASH_ALGO = "py-v1"


@dataclass(frozen=True)
class PHashConfig:
    """Knobs covering the parts of CoenM's pipeline that had to be inferred."""

    #: cv2 interpolation used to reach 64x64.
    interpolation: int = cv2.INTER_AREA
    #: "bt601" (0.299/0.587/0.114) or "bt709" (0.2126/0.7152/0.0722).
    grayscale: str = "bt601"
    #: Scale DCT rows orthonormally (affects which coefficients exceed the median).
    orthonormal: bool = True
    #: Exclude the DC term (0,0) from the median calculation.
    exclude_dc_from_median: bool = True
    #: Bit i corresponds to the i-th coefficient counting from the LSB.
    lsb_first: bool = True


DEFAULT_CONFIG = PHashConfig()


@lru_cache(maxsize=8)
def _dct_matrix(n: int, orthonormal: bool) -> np.ndarray:
    """DCT-II basis matrix, so a 2D DCT is ``D @ img @ D.T``."""
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (x + 0.5) * k / n)
    if orthonormal:
        m *= np.sqrt(2.0 / n)
        m[0] *= np.sqrt(0.5)
    return m


def _to_gray_64(image_bgr: np.ndarray, cfg: PHashConfig) -> np.ndarray:
    """Resize to 64x64 and convert to a single luminance channel."""
    if image_bgr.ndim == 3:
        b, g, r = (image_bgr[:, :, i].astype(np.float64) for i in range(3))
        if cfg.grayscale == "bt709":
            gray = 0.2126 * r + 0.7152 * g + 0.0722 * b
        else:
            gray = 0.299 * r + 0.587 * g + 0.114 * b
    else:
        gray = image_bgr.astype(np.float64)

    return cv2.resize(gray, (HASH_SIZE, HASH_SIZE), interpolation=cfg.interpolation)


def phash(image_bgr: np.ndarray, cfg: PHashConfig = DEFAULT_CONFIG) -> int:
    """64-bit perceptual hash of a BGR (or grayscale) image array."""
    gray = _to_gray_64(image_bgr, cfg)
    d = _dct_matrix(HASH_SIZE, cfg.orthonormal)
    dct = d @ gray @ d.T
    block = dct[:BLOCK, :BLOCK].flatten()

    median = float(np.median(block[1:] if cfg.exclude_dc_from_median else block))

    bits = block > median
    hash_value = 0
    for i, bit in enumerate(bits):
        if bit:
            hash_value |= 1 << (i if cfg.lsb_first else 63 - i)
    return hash_value


def crop_art(image_bgr: np.ndarray) -> np.ndarray:
    """Crop the art window from an upright card image."""
    h, w = image_bgr.shape[:2]
    x0 = int(w * ART_X0)
    y0 = int(h * ART_Y0)
    x1 = max(x0 + 1, min(w, int(w * ART_X1)))
    y1 = max(y0 + 1, min(h, int(h * ART_Y1)))
    return image_bgr[y0:y1, x0:x1]


def hash_full_and_art(image_bgr: np.ndarray, cfg: PHashConfig = DEFAULT_CONFIG) -> tuple[int, int]:
    """Compute the whole-card hash and the art-crop hash from one image."""
    return phash(image_bgr, cfg), phash(crop_art(image_bgr), cfg)


def decode_image(data: bytes) -> Optional[np.ndarray]:
    """Decode encoded image bytes (PNG/JPG) into a BGR array."""
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    return image if image is not None and image.size else None


def hamming_distance(a: int, b: int) -> int:
    """Bits that differ between two 64-bit hashes (0 = identical, 64 = opposite)."""
    return int((a ^ b).bit_count())


def similarity(a: int, b: int) -> float:
    """Similarity as a 0..1 fraction of matching bits."""
    return (64 - hamming_distance(a, b)) / 64.0
