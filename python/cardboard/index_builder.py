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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Optional

from .database import Database
from .hashing import HASH_ALGO, decode_image, hash_full_and_art
from .indexpack import IndexPackError, bundled_pack_path, read_pack
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

    # ---------------- fast path: bundled pack + incremental top-up ----------------

    def ensure_index(
        self,
        progress: ProgressFn,
        should_cancel: Callable[[], bool] = lambda: False,
        pack_path: Optional[Path] = None,
    ) -> None:
        """Get the index current as cheaply as possible.

        First run imports the bundled pack (seconds, no image downloads); after that only
        cards printed since the last sync are fetched. Falls back to a full build when no
        usable pack exists.
        """
        pack = pack_path or bundled_pack_path()
        imported_through: Optional[str] = None

        if self._db.index_count() == 0 and pack.exists():
            imported_through = self.import_pack(pack, progress)

        anchor = self._db.get_meta("index_synced_through") or imported_through
        if anchor is None:
            # Nothing to build on — do the full (expensive) build.
            self.build("default_cards", progress, should_cancel)
            return

        self.update_since(anchor, progress, should_cancel)

    def import_pack(self, pack_path: Path, progress: ProgressFn) -> Optional[str]:
        """Import a pre-built pack. Returns its build date, or None if unusable."""
        progress(IndexProgress(message=f"Importing bundled index from {pack_path.name}…"))
        try:
            pack = read_pack(pack_path)
        except IndexPackError as e:
            progress(IndexProgress(message=f"Bundled index unusable: {e}"))
            return None

        if pack.algo != HASH_ALGO:
            # Guard against mixing hashes from a different implementation, which would
            # silently break matching (see hashing.HASH_ALGO).
            progress(IndexProgress(message=(
                f"Bundled index was built by '{pack.algo}' but this app uses '{HASH_ALGO}' — "
                f"ignoring it; a rebuild is required.")))
            return None

        added = self._db.upsert_index_entries(pack.entries)
        self._db.set_meta("index_hash_algo", HASH_ALGO)
        self._db.set_meta("index_synced_through", pack.built)
        progress(IndexProgress(added=added, message=(
            f"Imported {added:,} cards from the bundled index (built {pack.built}).")))
        return pack.built

    def update_since(
        self,
        date_iso: str,
        progress: ProgressFn,
        should_cancel: Callable[[], bool] = lambda: False,
    ) -> None:
        """Hash only cards printed on/after ``date_iso``.

        Avoids the ~558 MB bulk download entirely — typically a few hundred cards a month.
        Overlaps by a few days so cards added late to an already-released set aren't missed.
        """
        try:
            anchor = datetime.strptime(date_iso, "%Y-%m-%d").date() - timedelta(days=7)
        except ValueError:
            anchor = date.today() - timedelta(days=30)

        progress(IndexProgress(message=f"Checking Scryfall for cards printed since {anchor}…"))
        cards = self._scryfall.cards_released_since(anchor.isoformat())
        if should_cancel():
            progress(IndexProgress(done=True, message="Index update cancelled."))
            return

        already = self._db.complete_scryfall_ids()
        fresh = [c for c in cards if c.get("id") and c["id"] not in already and _image_url(c, "small")]
        skipped = len(cards) - len(fresh)

        if not fresh:
            self._db.set_meta("index_synced_through", date.today().isoformat())
            progress(IndexProgress(processed=len(cards), skipped=skipped, done=True, message=(
                f"Index already current — checked {len(cards):,} recent printings, nothing new.")))
            return

        progress(IndexProgress(processed=len(cards), skipped=skipped, message=(
            f"Hashing {len(fresh):,} new card(s)…")))

        added = 0
        with ThreadPoolExecutor(max_workers=self.image_concurrency) as pool:
            for start in range(0, len(fresh), self.batch_size):
                if should_cancel():
                    progress(IndexProgress(added=added, done=True,
                                           message="Index update cancelled (progress saved)."))
                    return
                batch = fresh[start:start + self.batch_size]
                added += self._flush_batch(pool, batch)
                progress(IndexProgress(processed=len(cards), added=added, skipped=skipped,
                                       message=f"Hashing new cards ({added:,}/{len(fresh):,})…"))

        self._db.set_meta("index_hash_algo", HASH_ALGO)
        self._db.set_meta("index_synced_through", date.today().isoformat())
        progress(IndexProgress(processed=len(cards), added=added, skipped=skipped, done=True,
                               message=(f"Index updated: {added:,} new card(s) added. "
                                        f"Total in index: {self._db.index_count():,}.")))

    # ---------------- full build ----------------

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
        self._db.set_meta("index_synced_through", date.today().isoformat())
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
