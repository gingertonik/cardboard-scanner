"""One-time full index build, producing Python-hashed rows for a shippable pack.

Writes to a SEPARATE database by default so the C# app's working index is left alone
(its CoenM hashes are not interchangeable with ours). Resumable: re-run to continue.

    python tools/build_full_index.py [--db PATH] [--bulk default_cards|unique_artwork]

Then package the result:

    python tools/build_index_pack.py cardboard/data/index-pack.cbix
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardboard.database import Database, default_db_path  # noqa: E402
from cardboard.index_builder import IndexBuilder, IndexProgress  # noqa: E402
from cardboard.scryfall import ScryfallClient  # noqa: E402


def default_build_db() -> Path:
    """Scratch database for building the shippable index."""
    return default_db_path().parent.parent / "CardboardScanner" / "index-build.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a full Python-hashed match index.")
    parser.add_argument("--db", type=Path, default=default_build_db())
    parser.add_argument("--bulk", default="default_cards",
                        choices=["default_cards", "unique_artwork"])
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    db = Database(args.db)
    print(f"build db    : {args.db}", flush=True)
    print(f"bulk type   : {args.bulk}", flush=True)
    print(f"concurrency : {args.concurrency}", flush=True)
    print(f"already have: {db.index_count():,} rows", flush=True)
    print(f"pid         : {os.getpid()}\n", flush=True)

    started = time.monotonic()
    last_report = 0.0

    def on_progress(p: IndexProgress) -> None:
        nonlocal last_report
        now = time.monotonic()
        # Throttle routine updates; always show milestones.
        if not p.done and p.message is None and now - last_report < 15:
            return
        last_report = now
        elapsed = int(now - started)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        stamp = f"[{hours:02d}:{mins:02d}:{secs:02d}]"
        detail = p.message or (f"processed {p.processed:,} | added {p.added:,} "
                               f"| skipped {p.skipped:,}")
        rate = f" | {p.added / max(1, now - started) * 60:.0f} cards/min" if p.added else ""
        print(f"{stamp} {detail}{rate}", flush=True)

    with ScryfallClient() as scryfall:
        builder = IndexBuilder(db, scryfall)
        builder.image_concurrency = args.concurrency
        try:
            builder.build(args.bulk, on_progress)
        except KeyboardInterrupt:
            print("\nInterrupted - progress is saved, re-run to resume.", flush=True)
            return 130
        except Exception as e:
            print(f"\nBuild failed: {type(e).__name__}: {e}", flush=True)
            print("Progress is saved; re-run to resume.", flush=True)
            return 1

    total = db.index_count()
    print(f"\nDone. Index now holds {total:,} rows.", flush=True)
    print(f"Next: python tools/build_index_pack.py cardboard/data/index-pack.cbix", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
