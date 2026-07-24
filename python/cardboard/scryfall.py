"""Scryfall API client — port of the C# Services/ScryfallClient.cs.

Follows Scryfall's guidelines: identifying User-Agent, Accept header, and ~100 ms between
API requests. Bulk downloads stream to disk with no read timeout.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests

from .models import ScannedCard

API_BASE = "https://api.scryfall.com"
USER_AGENT = "CardboardScanner/2.0 (+local MTG collection tool)"


def _image_url(card: dict, size: str = "normal") -> Optional[str]:
    """Preferred image URL, walking card faces for double-faced cards."""
    uris = card.get("image_uris") or {}
    if size in uris:
        return uris[size]
    if "normal" in uris:
        return uris["normal"]
    faces = card.get("card_faces") or []
    if faces:
        face_uris = faces[0].get("image_uris") or {}
        return face_uris.get(size) or face_uris.get("normal")
    return None


def _price(card: dict, key: str) -> Optional[float]:
    raw = (card.get("prices") or {}).get(key)
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_scanned_card(card: dict) -> ScannedCard:
    """Map a Scryfall card object onto our model."""
    mana_cost = card.get("mana_cost")
    if not mana_cost and card.get("card_faces"):
        mana_cost = card["card_faces"][0].get("mana_cost")
    return ScannedCard(
        scryfall_id=card.get("id", ""),
        name=card.get("name", ""),
        set_code=card.get("set"),
        set_name=card.get("set_name"),
        collector_number=card.get("collector_number"),
        rarity=card.get("rarity"),
        mana_cost=mana_cost,
        type_line=card.get("type_line"),
        price_usd=_price(card, "usd"),
        price_usd_foil=_price(card, "usd_foil"),
        image_uri=_image_url(card, "normal"),
        scryfall_uri=card.get("scryfall_uri"),
    )


class ScryfallClient:
    def __init__(self, min_interval: float = 0.1) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_request = 0.0

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json;q=0.9,*/*;q=0.8",
        })

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "ScryfallClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    # ---------------- card lookups ----------------

    def get_by_fuzzy_name(self, name: str, timeout: float = 30) -> Optional[ScannedCard]:
        """GET /cards/named?fuzzy=... Returns None on no/ambiguous match."""
        if not name or not name.strip():
            return None
        self._throttle()
        try:
            r = self._session.get(f"{API_BASE}/cards/named", params={"fuzzy": name}, timeout=timeout)
        except requests.RequestException:
            return None
        if r.status_code in (400, 404) or not r.ok:
            return None
        return to_scanned_card(r.json())

    def get_by_id(self, scryfall_id: str, timeout: float = 30) -> Optional[ScannedCard]:
        if not scryfall_id:
            return None
        self._throttle()
        try:
            r = self._session.get(f"{API_BASE}/cards/{scryfall_id}", timeout=timeout)
        except requests.RequestException:
            return None
        return to_scanned_card(r.json()) if r.ok else None

    def search(self, query: str, timeout: float = 30) -> list[ScannedCard]:
        """General card search for the manual "add card" box."""
        if not query or not query.strip():
            return []
        return self._run_search(
            f"{API_BASE}/cards/search",
            {"unique": "cards", "order": "name", "q": query},
            timeout,
        )

    def get_printings(self, exact_name: str, timeout: float = 30) -> list[ScannedCard]:
        """All printings of a card by exact name (newest first)."""
        if not exact_name or not exact_name.strip():
            return []
        return self._run_search(
            f"{API_BASE}/cards/search",
            {"unique": "prints", "order": "released", "dir": "desc", "q": f'!"{exact_name}"'},
            timeout,
        )

    def _run_search(self, url: str, params: Optional[dict], timeout: float) -> list[ScannedCard]:
        results: list[ScannedCard] = []
        # Follow pagination a few pages at most (175 cards/page is ample here).
        for _ in range(4):
            if not url:
                break
            self._throttle()
            try:
                r = self._session.get(url, params=params, timeout=timeout)
            except requests.RequestException:
                break
            if r.status_code == 404 or not r.ok:  # no cards matched
                break
            payload = r.json()
            results.extend(to_scanned_card(c) for c in payload.get("data", []))
            url = payload.get("next_page") if payload.get("has_more") else None
            params = None  # next_page already carries the query
        return results

    # ---------------- bulk data + images ----------------

    def get_bulk_data_info(self, bulk_type: str, timeout: float = 30) -> Optional[dict]:
        """Locate a bulk-data descriptor, e.g. "unique_artwork" or "default_cards"."""
        self._throttle()
        try:
            r = self._session.get(f"{API_BASE}/bulk-data", timeout=timeout)
        except requests.RequestException:
            return None
        if not r.ok:
            return None
        for item in r.json().get("data", []):
            if item.get("type") == bulk_type:
                return item
        return None

    def download_bulk_to_file(
        self,
        uri: str,
        dest: Path,
        on_bytes: Optional[Callable[[int], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Stream a bulk-data file to disk. Returns False if cancelled."""
        with self._session.get(uri, stream=True, timeout=(30, None)) as r:
            r.raise_for_status()
            total = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if should_cancel and should_cancel():
                        return False
                    if not chunk:
                        continue
                    fh.write(chunk)
                    total += len(chunk)
                    if on_bytes:
                        on_bytes(total)
        return True

    def download_image(self, image_uri: str, timeout: float = 60) -> Optional[bytes]:
        """Fetch a single card image's bytes from the Scryfall image CDN."""
        try:
            r = self._session.get(image_uri, timeout=timeout)
        except requests.RequestException:
            return None
        return r.content if r.ok else None
