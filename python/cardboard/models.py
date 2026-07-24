"""Data models — mirrors the C# Models/CardModels.cs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

CONDITIONS = ("NM", "LP", "MP", "HP", "DMG")

# Most common MTG print languages (Scryfall codes).
LANGUAGES = (
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("ru", "Russian"),
    ("zhs", "Chinese (Simplified)"),
    ("zht", "Chinese (Traditional)"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


@dataclass
class CardIndexEntry:
    """One printing in the local perceptual-hash match index."""

    scryfall_id: str
    name: str
    oracle_id: str = ""
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    image_uri: Optional[str] = None
    #: 64-bit perceptual hash of the whole card image.
    phash: int = 0
    #: 64-bit perceptual hash of the art window (0 if not computed yet).
    art_phash: int = 0


@dataclass
class ScannedCard:
    """A card the user has scanned and confirmed into their library."""

    scryfall_id: str
    name: str
    id: int = 0
    set_code: Optional[str] = None
    set_name: Optional[str] = None
    collector_number: Optional[str] = None
    rarity: Optional[str] = None
    mana_cost: Optional[str] = None
    type_line: Optional[str] = None
    price_usd: Optional[float] = None
    price_usd_foil: Optional[float] = None
    image_uri: Optional[str] = None
    scryfall_uri: Optional[str] = None

    # Per-copy attributes that affect value and deckbuilding-site imports.
    foil: bool = False
    condition: str = "NM"
    language: str = "en"

    quantity: int = 1
    scanned_at: datetime = field(default_factory=_now)

    @property
    def effective_price(self) -> Optional[float]:
        """The price that applies to this copy given its finish."""
        if self.foil:
            return self.price_usd_foil if self.price_usd_foil is not None else self.price_usd
        return self.price_usd

    @property
    def printing_label(self) -> str:
        """"Card Name (SET) 123" — the printing identity shown in the UI."""
        parts = [self.name]
        if self.set_code:
            parts.append(f"({self.set_code.upper()})")
        if self.collector_number:
            parts.append(self.collector_number)
        label = " ".join(parts)
        return f"{label} · Foil" if self.foil else label


class MatchMethod(str, Enum):
    NONE = "None"
    OCR_NAME_LOOKUP = "OcrNameLookup"
    PERCEPTUAL_HASH = "PerceptualHash"
    HYBRID_CONFIRMED = "HybridConfirmed"


@dataclass
class MatchResult:
    """The result of trying to identify a detected card image."""

    success: bool = False
    method: MatchMethod = MatchMethod.NONE
    #: 0..1 confidence estimate.
    confidence: float = 0.0
    card: Optional[ScannedCard] = None
    #: Raw text OCR read from the title strip, for diagnostics.
    ocr_text: Optional[str] = None
    notes: Optional[str] = None

    @staticmethod
    def fail(ocr_text: Optional[str] = None, notes: Optional[str] = None) -> "MatchResult":
        return MatchResult(success=False, method=MatchMethod.NONE, confidence=0.0,
                           ocr_text=ocr_text, notes=notes)
