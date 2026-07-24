"""Determine whether the Python pHash can reproduce the C# (CoenM) hashes.

Samples rows from an existing match_index, re-downloads the exact image size used when
the index was built ("small"), then brute-forces the uncertain pipeline choices to find
a PHashConfig that reproduces the stored 64-bit hashes.

Run:  python tools/hash_parity.py [sample_count]

Outcome matters: an exact match means an existing ~116k index stays valid for the Python
app; otherwise the index must be rebuilt (or thresholds relaxed if the drift is tiny).
"""

from __future__ import annotations

import itertools
import sys
import time
from pathlib import Path

import cv2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardboard.database import Database  # noqa: E402
from cardboard.hashing import (  # noqa: E402
    PHashConfig, crop_art, decode_image, hamming_distance, phash,
)

API = "https://api.scryfall.com"
HEADERS = {
    "User-Agent": "CardboardScanner/2.0 (+local MTG collection tool)",
    "Accept": "application/json",
}

INTERPOLATIONS = [
    ("INTER_AREA", cv2.INTER_AREA),
    ("INTER_CUBIC", cv2.INTER_CUBIC),
    ("INTER_LINEAR", cv2.INTER_LINEAR),
    ("INTER_LANCZOS4", cv2.INTER_LANCZOS4),
]


def small_image_url(scryfall_id: str) -> str | None:
    """The 'small' image is what IndexBuilder hashed (image_uri stores 'normal')."""
    time.sleep(0.12)  # Scryfall asks for ~100ms between API requests
    r = requests.get(f"{API}/cards/{scryfall_id}", headers=HEADERS, timeout=30)
    if not r.ok:
        return None
    data = r.json()
    uris = data.get("image_uris")
    if not uris and data.get("card_faces"):
        uris = data["card_faces"][0].get("image_uris")
    return (uris or {}).get("small")


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 6

    db = Database()
    print(f"database : {db.db_path}")
    total = db.index_count()
    print(f"index    : {total:,} rows")
    if total == 0:
        print("\nNo index rows to compare against — build the index in the Windows app first.")
        return 2

    entries = db.sample_index_entries(count)
    print(f"sampling : {len(entries)} cards\n")

    samples = []  # (entry, bgr image)
    for e in entries:
        url = small_image_url(e.scryfall_id)
        if not url:
            print(f"  skip {e.name}: no small image URL")
            continue
        resp = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
        if not resp.ok:
            print(f"  skip {e.name}: image download failed")
            continue
        img = decode_image(resp.content)
        if img is None:
            print(f"  skip {e.name}: undecodable image")
            continue
        samples.append((e, img))
        print(f"  got  {e.name} [{e.set_code}] {img.shape[1]}x{img.shape[0]}")

    if not samples:
        print("\nNo images fetched — cannot compare.")
        return 2

    # Brute-force the inferred pipeline choices.
    print(f"\nSearching {len(INTERPOLATIONS) * 2 * 2 * 2 * 2} configurations...\n")
    results = []
    for (iname, interp), gray, ortho, exdc, lsb in itertools.product(
        INTERPOLATIONS, ("bt601", "bt709"), (True, False), (True, False), (True, False)
    ):
        cfg = PHashConfig(interpolation=interp, grayscale=gray, orthonormal=ortho,
                          exclude_dc_from_median=exdc, lsb_first=lsb)
        full_d = art_d = 0
        for e, img in samples:
            full_d += hamming_distance(phash(img, cfg), e.phash)
            art_d += hamming_distance(phash(crop_art(img), cfg), e.art_phash)
        n = len(samples)
        results.append((full_d / n, art_d / n, f"{iname}/{gray}/ortho={ortho}/exdc={exdc}/lsb={lsb}"))

    results.sort(key=lambda r: r[0] + r[1])
    print(f"{'avg full':>9} {'avg art':>8}  configuration")
    for full_d, art_d, label in results[:8]:
        print(f"{full_d:9.2f} {art_d:8.2f}  {label}")

    best_full, best_art, best_label = results[0]
    print()
    if best_full == 0 and best_art == 0:
        print(f"EXACT PARITY: {best_label}")
        print("The existing index is valid for the Python app — no rebuild needed.")
        return 0
    if best_full <= 3 and best_art <= 3:
        print(f"CLOSE (not exact): {best_label}")
        print(f"avg drift {best_full:.2f}/{best_art:.2f} bits — index is usable, but thresholds "
              f"should absorb the drift. A rebuild would be cleaner.")
        return 1
    print(f"NO PARITY. Best was {best_label} at {best_full:.2f}/{best_art:.2f} bits average drift.")
    print("The Python app should rebuild its own index (hashes are not interchangeable).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
