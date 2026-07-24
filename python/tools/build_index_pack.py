"""Build the shippable index pack from a local database.

Run at release time, then bundle the output with the app so users never hash 116k images:

    python tools/build_index_pack.py [output_path]

The pack records which hasher produced its hashes; the importer refuses a pack whose algo
does not match the running app, so incompatible hashes can never be mixed in silently.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardboard.database import Database  # noqa: E402
from cardboard.hashing import HASH_ALGO  # noqa: E402
from cardboard.indexpack import bundled_pack_path, read_pack, write_pack  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else bundled_pack_path()

    db = Database()
    print(f"database : {db.db_path}")

    db_algo = db.get_meta("index_hash_algo")
    entries = [e for e in db.load_index() if e.phash and e.art_phash]
    print(f"entries  : {len(entries):,} fully-hashed rows")
    # Keep tool output ASCII: Windows consoles default to cp1252 and raise on em dashes.
    print(f"db algo  : {db_algo or '(unset - built by the C# app)'}")
    print(f"app algo : {HASH_ALGO}")

    if not entries:
        print("\nNothing to pack — build the index first.")
        return 2

    if db_algo != HASH_ALGO:
        print(f"\nWARNING: this database's hashes were NOT produced by '{HASH_ALGO}'.")
        print("A pack built from it will be REJECTED by the app at import time.")
        print("It is still valid for testing the pack format, but do not ship it.")
        if "--force" not in sys.argv:
            print("\nRe-run with --force to write it anyway.")
            return 1

    built = date.today().isoformat()
    size = write_pack(out, entries, algo=db_algo or HASH_ALGO, built=built)
    print(f"\nwrote    : {out}")
    print(f"size     : {size / (1024 * 1024):.2f} MB  ({size / max(1, len(entries)):.1f} bytes/card)")
    print(f"built    : {built}")

    # Verify the round-trip so a corrupt pack is never shipped.
    pack = read_pack(out)
    ok = (len(pack.entries) == len(entries)
          and pack.built == built
          and all(a.scryfall_id == b.scryfall_id and a.phash == b.phash
                  and a.art_phash == b.art_phash and a.name == b.name
                  and (a.set_code or None) == (b.set_code or None)
                  and (a.collector_number or None) == (b.collector_number or None)
                  for a, b in zip(entries, pack.entries)))
    print(f"verify   : {'round-trip OK' if ok else 'MISMATCH — do not ship'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
