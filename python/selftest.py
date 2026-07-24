"""Headless verification of the non-UI pipeline — port of the C# SelfTest.cs.

Mirrors the Windows app's checks so the two implementations can be compared directly.
Network-dependent checks are reported as SKIP when offline, not as failures.

Run:  python selftest.py
Exit code 0 = all core checks passed.
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cardboard import camera  # noqa: E402
from cardboard.database import Database  # noqa: E402
from cardboard.detector import CARD_HEIGHT, CARD_WIDTH, CardDetector  # noqa: E402
from cardboard.exporter import ExportFormat, export  # noqa: E402
from cardboard.hashing import (  # noqa: E402
    crop_art, decode_image, hamming_distance, hash_full_and_art, phash,
)
from cardboard.matcher import CardMatcher  # noqa: E402
from cardboard.models import ScannedCard  # noqa: E402
from cardboard.ocr import OcrService  # noqa: E402
from cardboard.scryfall import ScryfallClient  # noqa: E402

failures = 0
lines: list[str] = []


def _log(text: str) -> None:
    lines.append(text)
    print(text, flush=True)


def _pass(name: str, extra: str | None = None) -> None:
    _log(f"PASS  {name}" + (f"  - {extra}" if extra else ""))


def _skip(name: str, why: str) -> None:
    _log(f"SKIP  {name}  - {why}")


def _fail(name: str, why: str) -> None:
    global failures
    failures += 1
    _log(f"FAIL  {name}  - {why}")


def _tmp_db() -> Path:
    return Path(tempfile.gettempdir()) / f"cardboard_test_{uuid.uuid4().hex}.db"


def main() -> int:
    _log("=== Cardboard Scanner self-test (Python) ===")

    # 1. Database round-trip
    try:
        path = _tmp_db()
        db = Database(path)
        card = ScannedCard(scryfall_id="test-id-1", name="Test Card", set_code="tst", price_usd=1.23)
        db.add_or_increment(card)
        db.add_or_increment(ScannedCard(scryfall_id="test-id-1", name="Test Card",
                                        set_code="tst", price_usd=1.23))
        rows = db.get_collection()
        if len(rows) == 1 and rows[0].quantity == 2 and rows[0].name == "Test Card":
            _pass("Database round-trip", f"qty={rows[0].quantity}")
        else:
            _fail("Database round-trip", f"unexpected state: count={len(rows)}")
        path.unlink(missing_ok=True)
    except Exception as e:
        _fail("Database round-trip", f"{type(e).__name__}: {e}")

    # 2. Perceptual hashing + 3. art-crop hash
    try:
        a = np.full((200, 140, 3), 255, dtype=np.uint8)
        cv2.rectangle(a, (20, 20), (120, 80), (0, 0, 0), -1)
        b = a.copy()
        c = np.full((200, 140, 3), 255, dtype=np.uint8)
        cv2.circle(c, (70, 100), 50, (0, 0, 0), -1)

        ha, hb, hc = phash(a), phash(b), phash(c)
        same, diff = hamming_distance(ha, hb), hamming_distance(ha, hc)
        if same == 0 and diff > same:
            _pass("Perceptual hashing", f"identical dist={same}, different dist={diff}")
        else:
            _fail("Perceptual hashing", f"identical dist={same}, different dist={diff}")

        full_a, art_a = hash_full_and_art(a)
        _, art_b = hash_full_and_art(b)
        _, art_c = hash_full_and_art(c)
        art_same, art_diff = hamming_distance(art_a, art_b), hamming_distance(art_a, art_c)
        if full_a == ha and art_a != 0 and art_same == 0 and art_diff > art_same:
            _pass("Art-crop hash", f"art identical dist={art_same}, different dist={art_diff}")
        else:
            _fail("Art-crop hash",
                  f"fullMatch={full_a == ha}, artA={art_a}, same={art_same}, diff={art_diff}")
    except Exception as e:
        _fail("Perceptual hashing", f"{type(e).__name__}: {e}")

    # 4. Card detection + perspective warp
    try:
        frame = np.full((700, 900, 3), 20, dtype=np.uint8)
        quad = np.array([[300, 130], [600, 150], [590, 560], [310, 540]], dtype=np.int32)
        cv2.fillConvexPoly(frame, quad, (235, 235, 235))
        cv2.polylines(frame, [quad], True, (120, 120, 120), 3)

        det = CardDetector().detect(frame)
        if det.found and det.warped is not None and det.warped.shape[:2] == (CARD_HEIGHT, CARD_WIDTH):
            _pass("Card detection + warp", f"areaFraction={det.area_fraction:.2f}")
        else:
            shape = "null" if det.warped is None else f"{det.warped.shape[1]}x{det.warped.shape[0]}"
            _fail("Card detection + warp", f"found={det.found}, warped={shape}")
    except Exception as e:
        _fail("Card detection + warp", f"{type(e).__name__}: {e}")

    # 5. OCR on a rendered title strip
    ocr_read = ""
    try:
        ocr = OcrService()
        if not ocr.available:
            _skip("OCR", "no OCR engine available")
        else:
            card_img = np.full((CARD_HEIGHT, CARD_WIDTH, 3), 240, dtype=np.uint8)
            cv2.putText(card_img, "Lightning Bolt", (20, 52), cv2.FONT_HERSHEY_DUPLEX,
                        0.9, (10, 10, 10), 2, cv2.LINE_AA)
            ocr_read = ocr.read_title(card_img)

            # Regression guard: a blank strip forces every enhancement variant to run.
            blank = np.full((CARD_HEIGHT, CARD_WIDTH, 3), 90, dtype=np.uint8)
            ocr.read_title(blank)

            if ocr_read and any(ch.isalpha() for ch in ocr_read):
                _pass("OCR", f'read="{ocr_read}" (blank-strip path ok)')
            else:
                _fail("OCR", f'read nothing usable ("{ocr_read}")')
    except Exception as e:
        _fail("OCR", f"{type(e).__name__}: {e}")

    # 6. Scryfall fuzzy lookup (network)
    online = None
    try:
        with ScryfallClient() as scry:
            online = scry.get_by_fuzzy_name("Llanowar Elves")
        if online and "llanowar" in online.name.lower():
            _pass("Scryfall fuzzy lookup", f"{online.name} [{online.set_code}] ${online.price_usd}")
        elif online is None:
            _skip("Scryfall fuzzy lookup", "no result (offline or API unreachable)")
        else:
            _fail("Scryfall fuzzy lookup", f"unexpected: {online.name}")
    except Exception as e:
        _skip("Scryfall fuzzy lookup", f"network error: {e}")

    # 7. Hybrid matcher (uses Scryfall)
    try:
        if online is None:
            _skip("Hybrid matcher", "requires Scryfall network access")
        else:
            path = _tmp_db()
            db = Database(path)
            with ScryfallClient() as scry:
                matcher = CardMatcher(db, scry)
                matcher.reload_index()
                blank_card = np.full((CARD_HEIGHT, CARD_WIDTH, 3), 240, dtype=np.uint8)
                res = matcher.identify(blank_card, "Llanowar Elves")
            if res.success and res.card and "llanowar" in res.card.name.lower():
                _pass("Hybrid matcher",
                      f"{res.method.value} conf={res.confidence:.2f} -> {res.card.name}")
            else:
                _fail("Hybrid matcher", f"success={res.success} notes={res.notes}")
            path.unlink(missing_ok=True)
    except Exception as e:
        _fail("Hybrid matcher", f"{type(e).__name__}: {e}")

    # 8. Finish/condition dedup
    try:
        path = _tmp_db()
        db = Database(path)

        def sol_ring(foil: bool) -> ScannedCard:
            return ScannedCard(scryfall_id="sid-x", name="Sol Ring", set_code="cmr",
                               collector_number="472", foil=foil, condition="NM", language="en")

        db.add_or_increment(sol_ring(False))
        db.add_or_increment(sol_ring(False))  # -> non-foil qty 2
        db.add_or_increment(sol_ring(True))   # -> separate foil row qty 1
        rows = db.get_collection()
        non_foil = next((r for r in rows if not r.foil), None)
        foil = next((r for r in rows if r.foil), None)
        if len(rows) == 2 and non_foil and non_foil.quantity == 2 and foil and foil.quantity == 1:
            _pass("Finish-based dedup", "non-foil x2 + foil x1 as separate rows")
        else:
            _fail("Finish-based dedup", f"rows={len(rows)}")
        path.unlink(missing_ok=True)
    except Exception as e:
        _fail("Finish-based dedup", f"{type(e).__name__}: {e}")

    # 9. Export formats
    try:
        cards = [ScannedCard(
            scryfall_id="abc-123", name="Lightning Bolt", set_code="2x2", collector_number="117",
            foil=True, condition="LP", language="ja", quantity=2,
            price_usd=1.00, price_usd_foil=3.00,
        )]
        mox = export(cards, ExportFormat.MOXFIELD_TEXT)
        mox_csv = export(cards, ExportFormat.MOXFIELD_CSV)
        arch = export(cards, ExportFormat.ARCHIDEKT_CSV)
        plain = export(cards, ExportFormat.PLAIN_TEXT_LIST)

        ok_text = "2 Lightning Bolt (2X2) 117 *F*" in mox
        ok_mox = ("Count,Name,Edition,Condition,Language,Foil" in mox_csv
                  and "Lightly Played" in mox_csv and "Japanese" in mox_csv
                  and ",foil," in mox_csv and "2x2" in mox_csv)
        ok_arch = "Scryfall ID" in arch and "abc-123" in arch and "Foil" in arch
        ok_plain = plain.strip() == "2 Lightning Bolt"

        if ok_text and ok_mox and ok_arch and ok_plain:
            _pass("Export formats", "Moxfield text/CSV, Archidekt CSV, plain list")
        else:
            _fail("Export formats",
                  f"text={ok_text}, moxCsv={ok_mox}, arch={ok_arch}, plain={ok_plain}")
    except Exception as e:
        _fail("Export formats", f"{type(e).__name__}: {e}")

    # 10. Scryfall search + printings (network)
    try:
        if online is None:
            _skip("Scryfall search/printings", "requires network access")
        else:
            with ScryfallClient() as scry:
                found = scry.search("Llanowar Elves")
                prints = scry.get_printings("Llanowar Elves")
            ok_search = any("llanowar" in c.name.lower() for c in found)
            ok_prints = len(prints) > 1
            if ok_search and ok_prints:
                _pass("Scryfall search/printings", f"search={len(found)}, printings={len(prints)}")
            else:
                _fail("Scryfall search/printings",
                      f"search={len(found)}(match={ok_search}), printings={len(prints)}")
    except Exception as e:
        _skip("Scryfall search/printings", f"network error: {e}")

    # 11. Video device enumeration by name
    try:
        devices = camera.enumerate_devices()
        if not devices:
            _skip("Video device names", "no video devices present")
        elif all(d.name.strip() for d in devices):
            _pass("Video device names", ", ".join(f"[{d.index}] {d.name}" for d in devices))
        else:
            _fail("Video device names", "some devices returned empty names")
    except Exception as e:
        _fail("Video device names", f"{type(e).__name__}: {e}")

    _log(f"=== {'ALL CORE TESTS PASSED' if failures == 0 else f'{failures} FAILURE(S)'} ===")

    log_path = Path(tempfile.gettempdir()) / "cardboard_selftest.log"
    try:
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n(log written to {log_path})")
    except OSError:
        pass
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
