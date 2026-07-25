"""SQLite storage — port of the C# Services/Database.cs.

The ``collection`` schema is byte-for-byte compatible with the Windows version, so this
opens an existing ``cardscanner.db`` and shares the user's library with it.

The match index, however, lives in its own table (``match_index_py``). The two apps hash
images differently (see hashing.HASH_ALGO), so sharing one index table would mean each app
silently overwriting the other's hashes and breaking its matching. Separate tables let both
versions coexist during the migration; the index is derived data, so duplicating it is cheap.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import CardIndexEntry, ScannedCard


def default_db_path() -> Path:
    """Per-user data location. Matches the Windows app's path on Windows so the existing
    library is picked up; uses OS conventions elsewhere. ``CARDBOARD_DB`` overrides it."""
    override = os.environ.get("CARDBOARD_DB")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "CardScanner" / "cardscanner.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CardboardScanner" / "cardscanner.db"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cardboard-scanner" / "cardscanner.db"


def _to_signed(value: int) -> int:
    """SQLite INTEGER is signed 64-bit; store the unsigned hash's bit pattern."""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _to_unsigned(value: Optional[int]) -> int:
    if value is None:
        return 0
    return value + (1 << 64) if value < 0 else value


class Database:
    def __init__(self, db_path: Optional[os.PathLike | str] = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open, commit on success, and always close.

        Note: ``with sqlite3.connect(...)`` only manages the *transaction* — it leaves the
        connection (and the file handle) open, which locks the database file on Windows.
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------- schema ----------------

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS match_index_py (
                    scryfall_id      TEXT PRIMARY KEY,
                    oracle_id        TEXT,
                    name             TEXT NOT NULL,
                    set_code         TEXT,
                    collector_number TEXT,
                    image_uri        TEXT,
                    phash            INTEGER NOT NULL,
                    art_phash        INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_match_index_py_name ON match_index_py(name);

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS collection (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    scryfall_id      TEXT NOT NULL,
                    name             TEXT NOT NULL,
                    set_code         TEXT,
                    set_name         TEXT,
                    collector_number TEXT,
                    rarity           TEXT,
                    mana_cost        TEXT,
                    type_line        TEXT,
                    price_usd        TEXT,
                    price_usd_foil   TEXT,
                    image_uri        TEXT,
                    scryfall_uri     TEXT,
                    foil             INTEGER NOT NULL DEFAULT 0,
                    condition        TEXT NOT NULL DEFAULT 'NM',
                    language         TEXT NOT NULL DEFAULT 'en',
                    quantity         INTEGER NOT NULL DEFAULT 1,
                    scanned_at       TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_collection_scryfall ON collection(scryfall_id);
                """
            )
            self._migrate(conn, "collection", [
                ("price_usd_foil", "ALTER TABLE collection ADD COLUMN price_usd_foil TEXT"),
                ("foil", "ALTER TABLE collection ADD COLUMN foil INTEGER NOT NULL DEFAULT 0"),
                ("condition", "ALTER TABLE collection ADD COLUMN condition TEXT NOT NULL DEFAULT 'NM'"),
                ("language", "ALTER TABLE collection ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"),
            ])
            # match_index_py is created above with every column, so it needs no migration.

    @staticmethod
    def _migrate(conn: sqlite3.Connection, table: str, adds: list[tuple[str, str]]) -> None:
        """Add columns to a table created by an earlier version, if missing."""
        existing = {row[1].lower() for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in adds:
            if column.lower() not in existing:
                conn.execute(ddl)

    # ---------------- meta ----------------

    def get_meta(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ---------------- match index ----------------

    def index_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM match_index_py").fetchone()[0])

    def complete_scryfall_ids(self) -> set[str]:
        """Ids that are fully hashed (both whole-card and art hashes present)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scryfall_id FROM match_index_py WHERE phash != 0 AND art_phash != 0"
            ).fetchall()
        return {r[0] for r in rows}

    def upsert_index_entries(self, entries: Iterable[CardIndexEntry]) -> int:
        rows = [
            (e.scryfall_id, e.oracle_id, e.name, e.set_code, e.collector_number,
             e.image_uri, _to_signed(e.phash), _to_signed(e.art_phash))
            for e in entries
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO match_index_py
                    (scryfall_id, oracle_id, name, set_code, collector_number, image_uri, phash, art_phash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scryfall_id) DO UPDATE SET
                    oracle_id=excluded.oracle_id, name=excluded.name, set_code=excluded.set_code,
                    collector_number=excluded.collector_number, image_uri=excluded.image_uri,
                    phash=excluded.phash, art_phash=excluded.art_phash
                """,
                rows,
            )
        return len(rows)

    def load_index(self) -> list[CardIndexEntry]:
        """Load the full index into memory for fast Hamming-distance search."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scryfall_id, oracle_id, name, set_code, collector_number, "
                "image_uri, phash, art_phash FROM match_index_py"
            ).fetchall()
        return [
            CardIndexEntry(
                scryfall_id=r[0], oracle_id=r[1] or "", name=r[2], set_code=r[3],
                collector_number=r[4], image_uri=r[5],
                phash=_to_unsigned(r[6]), art_phash=_to_unsigned(r[7]),
            )
            for r in rows
        ]

    def index_entry(self, scryfall_id: str) -> Optional[CardIndexEntry]:
        """Fetch a single index row (used by the hash-parity test)."""
        with self._connect() as conn:
            r = conn.execute(
                "SELECT scryfall_id, oracle_id, name, set_code, collector_number, "
                "image_uri, phash, art_phash FROM match_index_py WHERE scryfall_id = ?",
                (scryfall_id,),
            ).fetchone()
        if not r:
            return None
        return CardIndexEntry(
            scryfall_id=r[0], oracle_id=r[1] or "", name=r[2], set_code=r[3],
            collector_number=r[4], image_uri=r[5],
            phash=_to_unsigned(r[6]), art_phash=_to_unsigned(r[7]),
        )

    def legacy_index_entries(self, limit: int) -> list[CardIndexEntry]:
        """Sample the C# app's ``match_index`` table, for hash-parity comparison only."""
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    "SELECT scryfall_id, oracle_id, name, set_code, collector_number, "
                    "image_uri, phash, art_phash FROM match_index "
                    "WHERE phash != 0 AND art_phash != 0 LIMIT ?",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # no legacy table in this database
        return [
            CardIndexEntry(
                scryfall_id=r[0], oracle_id=r[1] or "", name=r[2], set_code=r[3],
                collector_number=r[4], image_uri=r[5],
                phash=_to_unsigned(r[6]), art_phash=_to_unsigned(r[7]),
            )
            for r in rows
        ]

    def sample_index_entries(self, limit: int) -> list[CardIndexEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scryfall_id, oracle_id, name, set_code, collector_number, "
                "image_uri, phash, art_phash FROM match_index_py "
                "WHERE phash != 0 AND art_phash != 0 LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            CardIndexEntry(
                scryfall_id=r[0], oracle_id=r[1] or "", name=r[2], set_code=r[3],
                collector_number=r[4], image_uri=r[5],
                phash=_to_unsigned(r[6]), art_phash=_to_unsigned(r[7]),
            )
            for r in rows
        ]

    # ---------------- collection ----------------

    def add_or_increment(self, card: ScannedCard) -> int:
        """A "copy" is printing + finish + condition + language; an identical copy
        increments quantity, otherwise a new row is created. Returns the new quantity."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, quantity FROM collection "
                "WHERE scryfall_id = ? AND foil = ? AND condition = ? AND language = ? LIMIT 1",
                (card.scryfall_id, 1 if card.foil else 0, card.condition, card.language),
            ).fetchone()

            if row:
                row_id, qty = int(row[0]), int(row[1]) + 1
                conn.execute(
                    "UPDATE collection SET quantity = ?, scanned_at = ? WHERE id = ?",
                    (qty, card.scanned_at.isoformat(), row_id),
                )
                card.id, card.quantity = row_id, qty
                return qty

            cur = conn.execute(
                """
                INSERT INTO collection
                    (scryfall_id, name, set_code, set_name, collector_number, rarity, mana_cost,
                     type_line, price_usd, price_usd_foil, image_uri, scryfall_uri,
                     foil, condition, language, quantity, scanned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (card.scryfall_id, card.name, card.set_code, card.set_name, card.collector_number,
                 card.rarity, card.mana_cost, card.type_line,
                 None if card.price_usd is None else f"{card.price_usd}",
                 None if card.price_usd_foil is None else f"{card.price_usd_foil}",
                 card.image_uri, card.scryfall_uri,
                 1 if card.foil else 0, card.condition, card.language,
                 card.scanned_at.isoformat()),
            )
            card.id = int(cur.lastrowid or 0)
            card.quantity = 1
            return 1

    def get_collection(self) -> list[ScannedCard]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, scryfall_id, name, set_code, set_name, collector_number, rarity,
                       mana_cost, type_line, price_usd, price_usd_foil, image_uri, scryfall_uri,
                       foil, condition, language, quantity, scanned_at
                FROM collection ORDER BY scanned_at DESC
                """
            ).fetchall()

        def parse_price(v) -> Optional[float]:
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        cards: list[ScannedCard] = []
        for r in rows:
            try:
                scanned = datetime.fromisoformat(r[17])
            except (TypeError, ValueError):
                scanned = datetime.now()
            cards.append(ScannedCard(
                id=int(r[0]), scryfall_id=r[1], name=r[2], set_code=r[3], set_name=r[4],
                collector_number=r[5], rarity=r[6], mana_cost=r[7], type_line=r[8],
                price_usd=parse_price(r[9]), price_usd_foil=parse_price(r[10]),
                image_uri=r[11], scryfall_uri=r[12],
                foil=bool(r[13]), condition=r[14] or "NM", language=r[15] or "en",
                quantity=int(r[16]), scanned_at=scanned,
            ))
        return cards

    def remove_one(self, row_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE collection SET quantity = quantity - 1 WHERE id = ?", (row_id,))
            conn.execute("DELETE FROM collection WHERE id = ? AND quantity <= 0", (row_id,))

    def delete_row(self, row_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM collection WHERE id = ?", (row_id,))

    def set_quantity(self, row_id: int, quantity: int) -> None:
        """A value <= 0 removes the row."""
        with self._connect() as conn:
            if quantity <= 0:
                conn.execute("DELETE FROM collection WHERE id = ?", (row_id,))
            else:
                conn.execute("UPDATE collection SET quantity = ? WHERE id = ?", (quantity, row_id))

    def update_attributes(self, row_id: int, foil: bool, condition: str, language: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection SET foil = ?, condition = ?, language = ? WHERE id = ?",
                (1 if foil else 0, condition, language, row_id),
            )

    def update_prices(self, scryfall_id: str, usd: Optional[float], usd_foil: Optional[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection SET price_usd = ?, price_usd_foil = ? WHERE scryfall_id = ?",
                (None if usd is None else f"{usd}",
                 None if usd_foil is None else f"{usd_foil}",
                 scryfall_id),
            )

    def distinct_scryfall_ids(self) -> list[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT scryfall_id FROM collection")]

    def collection_total_cards(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM collection").fetchone()[0])
