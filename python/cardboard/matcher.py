"""Hybrid card identification — port of the C# Services/CardMatcher.cs.

  1. OCR the title, look it up by fuzzy name on Scryfall.
  2. Perceptual-hash the card against the local index (fallback / confirmation).

Each index entry is scored by the better of its whole-card and art-crop distances, so a
foil whose whole-card hash is wrecked by glare can still match on its art. A standalone
image match must also clearly beat the runner-up, which prevents the "nearest neighbour of
an incomplete index" failure mode from producing confident-but-wrong results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .database import Database
from .hashing import hamming_distance, hash_full_and_art, similarity
from .models import CardIndexEntry, MatchMethod, MatchResult, ScannedCard
from .scryfall import ScryfallClient


@dataclass
class _Candidate:
    entry: CardIndexEntry
    distance: int
    second_distance: int
    via_art: bool


class CardMatcher:
    def __init__(self, db: Database, scryfall: ScryfallClient) -> None:
        self._db = db
        self._scryfall = scryfall
        self._index: list[CardIndexEntry] = []
        self._hashes: Optional[np.ndarray] = None
        self._art_hashes: Optional[np.ndarray] = None

        #: Max Hamming distance to accept a *standalone* image-hash match.
        self.phash_accept_distance = 8
        #: The best match must beat the runner-up by at least this many bits.
        self.phash_margin_requirement = 4
        #: Max distance for the image hash to "confirm" an OCR name hit.
        self.phash_confirm_distance = 16

    @property
    def index_size(self) -> int:
        return len(self._index)

    def reload_index(self) -> None:
        """Load the index and pack hashes into arrays for vectorised search."""
        self._index = self._db.load_index()
        if self._index:
            self._hashes = np.array([e.phash for e in self._index], dtype=np.uint64)
            # An art hash of 0 means "not computed" — mark it so it never wins.
            self._art_hashes = np.array([e.art_phash for e in self._index], dtype=np.uint64)
        else:
            self._hashes = self._art_hashes = None

    def hash_card(self, warped_card: np.ndarray) -> tuple[int, int]:
        return hash_full_and_art(warped_card)

    def identify(self, warped_card: np.ndarray, ocr_name: str) -> MatchResult:
        live_full, live_art = self.hash_card(warped_card)
        best = self._find_best_by_hash(live_full, live_art)

        # --- Path A: OCR name -> Scryfall fuzzy lookup ---
        if ocr_name and len(ocr_name.strip()) >= 3:
            named = self._scryfall.get_by_fuzzy_name(ocr_name)
            if named is not None:
                name_sim = string_similarity(_normalize(ocr_name), _normalize(named.name))

                # Does the image hash agree with the named card's printing?
                hash_confirms = (
                    best is not None
                    and best.distance <= self.phash_confirm_distance
                    and _normalize(best.entry.name) == _normalize(named.name)
                )
                if hash_confirms:
                    return MatchResult(
                        success=True,
                        method=MatchMethod.HYBRID_CONFIRMED,
                        confidence=min(0.99, 0.80 + name_sim * 0.19),
                        card=named,
                        ocr_text=ocr_name,
                        notes="OCR name confirmed by image hash.",
                    )

                if name_sim >= 0.55:
                    return MatchResult(
                        success=True,
                        method=MatchMethod.OCR_NAME_LOOKUP,
                        confidence=min(0.95, 0.45 + name_sim * 0.5),
                        card=named,
                        ocr_text=ocr_name,
                        notes="Matched by card name (OCR).",
                    )

        # --- Path B: pure perceptual-hash fallback ---
        if best is not None:
            margin = best.second_distance - best.distance
            if best.distance <= self.phash_accept_distance and margin >= self.phash_margin_requirement:
                card = self._scryfall.get_by_id(best.entry.scryfall_id) or _from_index(best.entry)
                # Confidence from distance (0 bits -> ~0.95, accept-limit -> ~0.60).
                conf = 0.60 + 0.35 * (self.phash_accept_distance - best.distance) / self.phash_accept_distance
                source = "art crop" if best.via_art else "whole card"
                return MatchResult(
                    success=True,
                    method=MatchMethod.PERCEPTUAL_HASH,
                    confidence=min(0.95, max(0.60, conf)),
                    card=card,
                    ocr_text=ocr_name,
                    notes=f"Image-hash match via {source} (distance {best.distance}, margin {margin}).",
                )

        # Nothing confident — explain the most likely reason so the user can act on it.
        if not self._index:
            why = ("No confident match — OCR couldn't read the name and the image index is empty. "
                   "Build the index and/or improve lighting.")
        elif best is not None and best.distance <= self.phash_accept_distance:
            why = (f"No confident match — closest image (dist {best.distance}) is ambiguous "
                   f"(margin {best.second_distance - best.distance}). The exact card may not be indexed yet.")
        else:
            dist = best.distance if best else "n/a"
            why = (f"No confident match — nearest indexed image is too different (dist {dist}). "
                   f"Try better lighting, or add it via Manual search.")
        return MatchResult.fail(ocr_name, why)

    def _find_best_by_hash(self, live_full: int, live_art: int) -> Optional[_Candidate]:
        if not self._index or self._hashes is None or self._art_hashes is None:
            return None

        full_d = _popcount(self._hashes ^ np.uint64(live_full))
        art_d = _popcount(self._art_hashes ^ np.uint64(live_art))
        # Unhashed art (0) must never win.
        art_d = np.where(self._art_hashes == np.uint64(0), np.uint8(64), art_d)

        via_art = art_d < full_d
        combined = np.minimum(full_d, art_d)

        best_i = int(np.argmin(combined))
        best_d = int(combined[best_i])
        # Runner-up = second smallest distance. Ties give a zero margin, which correctly
        # reads as "ambiguous" and is rejected by the caller.
        second_d = int(np.partition(combined, 1)[1]) if combined.size >= 2 else 64

        return _Candidate(
            entry=self._index[best_i],
            distance=best_d,
            second_distance=second_d,
            via_art=bool(via_art[best_i]),
        )


_POPCOUNT_TABLE = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount(values: np.ndarray) -> np.ndarray:
    """Vectorised 64-bit population count via a byte lookup table."""
    as_bytes = values.view(np.uint8).reshape(-1, 8)
    return _POPCOUNT_TABLE[as_bytes].sum(axis=1).astype(np.uint8)


def _from_index(entry: CardIndexEntry) -> ScannedCard:
    return ScannedCard(
        scryfall_id=entry.scryfall_id,
        name=entry.name,
        set_code=entry.set_code,
        collector_number=entry.collector_number,
        image_uri=entry.image_uri,
    )


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()


def string_similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity in [0, 1]."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return 1.0 - prev[len(b)] / max(len(a), len(b))
