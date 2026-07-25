"""Collection export — port of the C# Services/CollectionExporter.cs.

Moxfield conventions verified against its documented collection-CSV importer: columns are
matched by name, Foil is "foil"/blank, and condition/language use full names.
"""

from __future__ import annotations

import csv
import io
from enum import Enum
from typing import Iterable, Sequence

from .models import LANGUAGES, ScannedCard


class ExportFormat(str, Enum):
    PLAIN_TEXT_LIST = "PlainTextList"
    MOXFIELD_TEXT = "MoxfieldText"
    MOXFIELD_CSV = "MoxfieldCsv"
    ARCHIDEKT_CSV = "ArchidektCsv"
    GENERIC_CSV = "GenericCsv"


#: (format, label) in the order shown in the UI.
ALL_FORMATS: Sequence[tuple[ExportFormat, str]] = (
    (ExportFormat.MOXFIELD_TEXT, "Moxfield / Arena — deck text"),
    (ExportFormat.MOXFIELD_CSV, "Moxfield — collection CSV"),
    (ExportFormat.ARCHIDEKT_CSV, "Archidekt — collection CSV"),
    (ExportFormat.PLAIN_TEXT_LIST, "Plain deck list (universal)"),
    (ExportFormat.GENERIC_CSV, "Generic CSV / spreadsheet"),
)

_MOXFIELD_CONDITION = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}

_LANGUAGE_NAMES = dict(LANGUAGES) | {
    "zhs": "Simplified Chinese",
    "zht": "Traditional Chinese",
}


def file_info_for(fmt: ExportFormat) -> tuple[str, str]:
    return {
        ExportFormat.MOXFIELD_TEXT: (".txt", "library-moxfield.txt"),
        ExportFormat.MOXFIELD_CSV: (".csv", "library-moxfield.csv"),
        ExportFormat.ARCHIDEKT_CSV: (".csv", "library-archidekt.csv"),
        ExportFormat.GENERIC_CSV: (".csv", "library.csv"),
    }.get(fmt, (".txt", "library.txt"))


def export(cards: Iterable[ScannedCard], fmt: ExportFormat) -> str:
    cards = list(cards)
    if fmt is ExportFormat.MOXFIELD_TEXT:
        return _moxfield_text(cards)
    if fmt is ExportFormat.MOXFIELD_CSV:
        return _moxfield_csv(cards)
    if fmt is ExportFormat.ARCHIDEKT_CSV:
        return _archidekt_csv(cards)
    if fmt is ExportFormat.GENERIC_CSV:
        return _generic_csv(cards)
    return _plain_text(cards)


# ---------------- text formats ----------------

def _plain_text(cards: Sequence[ScannedCard]) -> str:
    return "".join(f"{c.quantity} {c.name}\n" for c in cards)


def _moxfield_text(cards: Sequence[ScannedCard]) -> str:
    """"1 Lightning Bolt (2X2) 117 *F*" — accepted by Moxfield and MTG Arena."""
    lines = []
    for c in cards:
        line = f"{c.quantity} {c.name}"
        if c.set_code and c.collector_number:
            line += f" ({c.set_code.upper()}) {c.collector_number}"
        if c.foil:
            line += " *F*"
        lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------- CSV formats ----------------

def _price(value) -> str:
    return f"{value:.2f}" if value is not None else ""


def _write_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _moxfield_csv(cards: Sequence[ScannedCard]) -> str:
    return _write_csv(
        ["Count", "Name", "Edition", "Condition", "Language", "Foil",
         "Collector Number", "Tag", "Purchase Price"],
        [[
            str(c.quantity),
            c.name,
            (c.set_code or "").lower(),
            _MOXFIELD_CONDITION.get(c.condition.upper(), "Near Mint"),
            _LANGUAGE_NAMES.get(c.language, "English"),
            "foil" if c.foil else "",
            c.collector_number or "",
            "",
            _price(c.effective_price),
        ] for c in cards],
    )


def _archidekt_csv(cards: Sequence[ScannedCard]) -> str:
    # Archidekt matches columns by header and can key on Scryfall ID for the exact printing.
    return _write_csv(
        ["Quantity", "Name", "Finish", "Condition", "Language", "Edition Code",
         "Collector Number", "Scryfall ID", "Purchase Price"],
        [[
            str(c.quantity),
            c.name,
            "Foil" if c.foil else "Normal",
            c.condition,
            _LANGUAGE_NAMES.get(c.language, "English"),
            (c.set_code or "").lower(),
            c.collector_number or "",
            c.scryfall_id,
            _price(c.effective_price),
        ] for c in cards],
    )


def _generic_csv(cards: Sequence[ScannedCard]) -> str:
    return _write_csv(
        ["Count", "Name", "Set Code", "Set Name", "Collector Number", "Rarity", "Foil",
         "Condition", "Language", "Price USD", "Type", "Scryfall ID", "Scryfall URI", "Scanned At"],
        [[
            str(c.quantity),
            c.name,
            (c.set_code or "").upper(),
            c.set_name or "",
            c.collector_number or "",
            c.rarity or "",
            "true" if c.foil else "false",
            c.condition,
            c.language,
            _price(c.effective_price),
            c.type_line or "",
            c.scryfall_id,
            c.scryfall_uri or "",
            c.scanned_at.strftime("%Y-%m-%d"),
        ] for c in cards],
    )
