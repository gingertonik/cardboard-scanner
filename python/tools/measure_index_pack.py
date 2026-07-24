"""Measure candidate formats for a pre-built index shipped with the app.

Compares a trimmed SQLite snapshot against a columnar packed binary, with and without
the image_uri column (the largest text field, and only ever a display fallback).

Run:  python tools/measure_index_pack.py
"""

from __future__ import annotations

import gzip
import lzma
import sqlite3
import struct
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardboard.database import Database  # noqa: E402

TMP = Path(tempfile.gettempdir())


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):7.2f} MB"


def sqlite_snapshot(rows, include_uri: bool) -> Path:
    path = TMP / f"idxsnap_{'uri' if include_uri else 'nouri'}.sqlite"
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        cols = "scryfall_id TEXT PRIMARY KEY, name TEXT, set_code TEXT, collector_number TEXT, phash INTEGER, art_phash INTEGER"
        if include_uri:
            cols += ", image_uri TEXT"
        conn.execute(f"CREATE TABLE match_index ({cols})")
        if include_uri:
            conn.executemany(
                "INSERT OR REPLACE INTO match_index VALUES (?,?,?,?,?,?,?)",
                [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows],
            )
        else:
            conn.executemany(
                "INSERT OR REPLACE INTO match_index VALUES (?,?,?,?,?,?)",
                [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
            )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    return path


def packed_columnar(rows, include_uri: bool) -> bytes:
    """Columnar layout: like values adjacent, which compresses far better than row-major."""
    ids = bytearray()
    hashes = bytearray()
    names, sets, numbers, uris = [], [], [], []
    for scryfall_id, name, set_code, number, phash, art, uri in rows:
        try:
            ids += uuid.UUID(scryfall_id).bytes  # 16 bytes instead of 36 chars
        except (ValueError, AttributeError):
            ids += b"\0" * 16
        hashes += struct.pack("<qq", phash or 0, art or 0)
        names.append(name or "")
        sets.append(set_code or "")
        numbers.append(number or "")
        if include_uri:
            uris.append(uri or "")

    parts = [
        b"CBIX\x01\x00",
        struct.pack("<I", len(rows)),
        bytes(ids),
        bytes(hashes),
        "\n".join(names).encode(),
        b"\x00",
        "\n".join(sets).encode(),
        b"\x00",
        "\n".join(numbers).encode(),
        b"\x00",
    ]
    if include_uri:
        parts += ["\n".join(uris).encode(), b"\x00"]
    return b"".join(parts)


def main() -> int:
    db = Database()
    print(f"database: {db.db_path}")
    with sqlite3.connect(str(db.db_path)) as conn:
        rows = conn.execute(
            "SELECT scryfall_id, name, set_code, collector_number, phash, art_phash, image_uri "
            "FROM match_index"
        ).fetchall()
    print(f"rows    : {len(rows):,}\n")
    if not rows:
        print("Index is empty — nothing to measure.")
        return 2

    print(f"{'variant':44} {'raw':>11} {'gzip':>11} {'lzma':>11}")
    print("-" * 80)
    for include_uri in (True, False):
        label = "with image_uri" if include_uri else "no image_uri"

        snap = sqlite_snapshot(rows, include_uri)
        raw = snap.read_bytes()
        print(f"{'SQLite snapshot (' + label + ')':44} {mb(len(raw)):>11} "
              f"{mb(len(gzip.compress(raw, 9))):>11} {mb(len(lzma.compress(raw))):>11}")
        snap.unlink(missing_ok=True)

        packed = packed_columnar(rows, include_uri)
        print(f"{'packed columnar (' + label + ')':44} {mb(len(packed)):>11} "
              f"{mb(len(gzip.compress(packed, 9))):>11} {mb(len(lzma.compress(packed))):>11}")

    print("\nNote: UUIDs and 64-bit hashes are high-entropy and barely compress;")
    print("names/sets/numbers compress well. image_uri is pure overhead if fetched on demand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
