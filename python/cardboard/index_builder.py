"""Match-index builder — port of the C# Services/IndexBuilder.cs.

The bulk JSON (hundreds of MB) is downloaded to a temp file *first*, then parsed from disk
while card images are downloaded and hashed with bounded concurrency. Downloading up front
avoids holding one giant HTTP response open for the entire hashing pass, which in the C#
version caused the transfer to be cut off after a few thousand cards.

Resumable: an intact download is reused, and cards already fully hashed are skipped.
"""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .database import Database
from .hashing import HASH_ALGO, decode_image, hash_full_and_art
from .models import CardIndexEntry
from .scryfall import ScryfallClient, _image_url

APPROX_COUNTS = {"unique_artwork": 55_000, "default_cards": 110_000}


@dataclass
class IndexProgress:
    processed: int = 0
    added: int = 0
    skipped: int = 0
    current_name: Optional[str] = None
    done: bool = False
    message: Optional[str] = None


ProgressFn = Callable[[IndexProgress], None]


class IndexBuilder:
    def __init__(self, db: Database, scryfall: ScryfallClient) -> None:
        self._db = db
        self._scryfall = scryfall
        #: Concurrent image downloads (served from Scryfall's CDN).
        self.image_concurrency = 6
        #: Cards per batch (download+hash, then one DB write).
        self.batch_size = 200

    def build(
        self,
        bulk_type: str,
        progress: ProgressFn,
        should_cancel: Callable[[], bool] = lambda: False,
    ) -> None:
        progress(IndexProgress(message="Fetching Scryfall bulk-data descriptor..."))
        info = self._scryfall.get_bulk_data_info(bulk_type)
        if not info or not info.get("download_uri"):
            progress(IndexProgress(done=True, message="Could not locate Scryfall bulk data."))
            return

        size = int(info.get("size") or 0)
        size_mb = size // (1024 * 1024)
        tmp = Path(tempfile.gettempdir()) / f"cardboard_bulk_{bulk_type}.json"

        # 1) Download to disk (reuse an intact prior download to resume cheaply).
        if tmp.exists() and size and tmp.stat().st_size == size:
            progress(IndexProgress(message=f"Reusing downloaded '{info.get('name')}' ({size_mb} MB)."))
        else:
            def on_bytes(received: int) -> None:
                progress(IndexProgress(
                    message=f"Downloading '{info.get('name')}': {received // (1024 * 1024)} / {size_mb} MB"))

            if not self._scryfall.download_bulk_to_file(
                info["download_uri"], tmp, on_bytes, should_cancel
            ):
                progress(IndexProgress(done=True, message="Index build cancelled."))
                return

        # 2) Parse from disk, hashing images with bounded concurrency.
        # "Complete" = both hashes present, so rows from an older index get upgraded.
        already = self._db.complete_scryfall_ids()
        approx = APPROX_COUNTS.get(bulk_type, 100_000)
        state = IndexProgress(message=f"Hashing images (0 / ~{approx:,}). {len(already):,} already indexed.")
        progress(state)

        processed = added = skipped = 0
        batch: list[dict] = []

        with ThreadPoolExecutor(max_workers=self.image_concurrency) as pool:
            for card in _iter_bulk_cards(tmp):
                if should_cancel():
                    progress(IndexProgress(processed, added, skipped, done=True,
                                           message="Index build cancelled (progress saved — re-run to resume)."))
                    return
                processed += 1

                card_id = card.get("id")
                if not card_id or card_id in already or not _image_url(card, "small"):
                    skipped += 1
                else:
                    batch.append(card)

                if len(batch) >= self.batch_size:
                    added += self._flush_batch(pool, batch)
                    batch.clear()
                    progress(IndexProgress(
                        processed, added, skipped, card.get("name"),
                        message=f"Hashing images ({added:,} added / ~{approx:,}, {skipped:,} skipped)"))

            if batch:
                added += self._flush_batch(pool, batch)

        self._db.set_meta("index_hash_algo", HASH_ALGO)
        progress(IndexProgress(
            processed, added, skipped, done=True,
            message=(f"Index build complete. Added {added:,}, skipped {skipped:,}. "
                     f"Total in index: {self._db.index_count():,}.")))

        try:
            tmp.unlink()
        except OSError:
            pass  # leave the temp file for a possible retry

    def _flush_batch(self, pool: ThreadPoolExecutor, cards: list[dict]) -> int:
        """Download + hash a batch concurrently, then upsert. Returns count added."""
        entries = [e for e in pool.map(self._hash_card, cards) if e is not None]
        return self._db.upsert_index_entries(entries)

    def _hash_card(self, card: dict) -> Optional[CardIndexEntry]:
        try:
            url = _image_url(card, "small")
            if not url:
                return None
            data = self._scryfall.download_image(url)
            if not data:
                return None
            image = decode_image(data)
            if image is None:
                return None
            full, art = hash_full_and_art(image)
            return CardIndexEntry(
                scryfall_id=card["id"],
                oracle_id=card.get("oracle_id") or "",
                name=card.get("name", ""),
                set_code=card.get("set"),
                collector_number=card.get("collector_number"),
                image_uri=_image_url(card, "normal"),
                phash=full,
                art_phash=art,
            )
        except Exception:
            return None


def _iter_bulk_cards(path: Path) -> Iterator[dict]:
    """Stream card objects from Scryfall's bulk JSON array without loading it all.

    The file is a single top-level array of objects, one per line in practice, so parse
    line-wise and fall back to a whole-file load if the layout differs.
    """
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.readline().strip()
        if not first.startswith("["):
            fh.seek(0)
            for card in json.load(fh):
                yield card
            return

        # Handle "[{...}," on the first line (compact single-line arrays fall back below).
        remainder = first[1:].strip()
        if remainder and not remainder.endswith(","):
            fh.seek(0)
            for card in json.load(fh):
                yield card
            return
        if remainder:
            try:
                yield json.loads(remainder.rstrip(","))
            except json.JSONDecodeError:
                fh.seek(0)
                for card in json.load(fh):
                    yield card
                return

        for line in fh:
            line = line.strip().rstrip(",")
            if not line or line == "]":
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
