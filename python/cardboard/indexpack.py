"""Pre-built match-index packs shipped with the app.

A full index is ~116k cards. Hashing that from scratch means downloading ~116k images, so
instead a pack is built once at release time and bundled; first run imports it in seconds
and then only tops up cards released since the pack was built.

Format (all little-endian, whole file LZMA-compressed):

    magic    b"CBIX"
    version  u16
    algo     16 bytes, UTF-8, NUL-padded   -- which hasher produced these hashes
    built    10 bytes ASCII "YYYY-MM-DD"   -- pack build date, the incremental-sync anchor
    count    u32
    ids      count * 16 bytes              -- scryfall UUIDs, packed binary
    hashes   count * 16 bytes              -- (phash u64, art_phash u64)
    names    u32 length + UTF-8, "\\n"-joined
    sets     u32 length + UTF-8, "\\n"-joined
    numbers  u32 length + UTF-8, "\\n"-joined

Columnar layout keeps like values adjacent, which compresses far better than row-major.
``image_uri`` is deliberately omitted: it is only ever a display fallback and roughly
doubles the pack size, so it is fetched on demand instead.

Measured on a real 116,017-card index: 3.83 MB (vs 10.94 MB for a gzipped SQLite dump).
"""

from __future__ import annotations

import lzma
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .models import CardIndexEntry

MAGIC = b"CBIX"
VERSION = 1
ALGO_FIELD = 16
DATE_FIELD = 10


class IndexPackError(Exception):
    """Raised when a pack is malformed or unreadable."""


@dataclass
class IndexPack:
    #: Hasher identity (see hashing.HASH_ALGO). Hashes are only valid for a matching app.
    algo: str
    #: "YYYY-MM-DD" — anchor for the incremental "what's new since" query.
    built: str
    entries: list[CardIndexEntry]


def _pack_strings(values: Sequence[str]) -> bytes:
    blob = "\n".join(values).encode("utf-8")
    return struct.pack("<I", len(blob)) + blob


def _read_strings(data: bytes, offset: int, count: int) -> tuple[list[str], int]:
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    blob = data[offset:offset + length].decode("utf-8")
    offset += length
    values = blob.split("\n") if blob else []
    if len(values) != count:
        raise IndexPackError(f"expected {count} strings, found {len(values)}")
    return values, offset


def write_pack(path: Path, entries: Iterable[CardIndexEntry], algo: str, built: str) -> int:
    """Write a pack; returns the compressed byte size."""
    entries = list(entries)
    if len(algo.encode()) > ALGO_FIELD:
        raise IndexPackError(f"algo '{algo}' exceeds {ALGO_FIELD} bytes")
    if len(built) != DATE_FIELD:
        raise IndexPackError("built must be formatted YYYY-MM-DD")

    ids = bytearray()
    hashes = bytearray()
    names, sets, numbers = [], [], []
    for e in entries:
        try:
            ids += uuid.UUID(e.scryfall_id).bytes
        except (ValueError, AttributeError) as exc:
            raise IndexPackError(f"'{e.scryfall_id}' is not a UUID") from exc
        hashes += struct.pack("<QQ", e.phash & 0xFFFFFFFFFFFFFFFF, e.art_phash & 0xFFFFFFFFFFFFFFFF)
        names.append(e.name or "")
        sets.append(e.set_code or "")
        numbers.append(e.collector_number or "")

    body = b"".join([
        MAGIC,
        struct.pack("<H", VERSION),
        algo.encode("utf-8").ljust(ALGO_FIELD, b"\0"),
        built.encode("ascii"),
        struct.pack("<I", len(entries)),
        bytes(ids),
        bytes(hashes),
        _pack_strings(names),
        _pack_strings(sets),
        _pack_strings(numbers),
    ])

    compressed = lzma.compress(body, preset=6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compressed)
    return len(compressed)


def read_pack(path: Path) -> IndexPack:
    try:
        data = lzma.decompress(Path(path).read_bytes())
    except (lzma.LZMAError, OSError) as exc:
        raise IndexPackError(f"could not decompress {path}: {exc}") from exc

    if data[:4] != MAGIC:
        raise IndexPackError("not an index pack (bad magic)")
    offset = 4
    (version,) = struct.unpack_from("<H", data, offset)
    offset += 2
    if version != VERSION:
        raise IndexPackError(f"unsupported pack version {version}")

    algo = data[offset:offset + ALGO_FIELD].rstrip(b"\0").decode("utf-8")
    offset += ALGO_FIELD
    built = data[offset:offset + DATE_FIELD].decode("ascii")
    offset += DATE_FIELD
    (count,) = struct.unpack_from("<I", data, offset)
    offset += 4

    ids = data[offset:offset + count * 16]
    offset += count * 16
    raw_hashes = data[offset:offset + count * 16]
    offset += count * 16

    names, offset = _read_strings(data, offset, count)
    sets, offset = _read_strings(data, offset, count)
    numbers, offset = _read_strings(data, offset, count)

    entries: list[CardIndexEntry] = []
    for i in range(count):
        phash, art = struct.unpack_from("<QQ", raw_hashes, i * 16)
        entries.append(CardIndexEntry(
            scryfall_id=str(uuid.UUID(bytes=bytes(ids[i * 16:(i + 1) * 16]))),
            name=names[i],
            set_code=sets[i] or None,
            collector_number=numbers[i] or None,
            phash=phash,
            art_phash=art,
        ))

    return IndexPack(algo=algo, built=built, entries=entries)


def bundled_pack_path() -> Path:
    """Location of the pack shipped alongside the app (absent in a source checkout)."""
    return Path(__file__).resolve().parent / "data" / "index-pack.cbix"
