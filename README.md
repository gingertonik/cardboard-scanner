# Cardboard Scanner

A desktop app for **Windows, macOS, and Linux** that watches a live video device (webcam,
capture card, document camera), detects a Magic: The Gathering card in frame, identifies it,
cross-matches it against the [Scryfall](https://scryfall.com) database, and files it into a
local library of everything you've scanned.

Matching is **hybrid**:

1. **OCR the title** — the card's name is read from the top strip, then looked up on
   Scryfall via fuzzy name search. No separate OCR install is needed.
2. **Perceptual image hash** — the card is hashed and compared against a local index of
   Scryfall images, as a fallback when text is unreadable (worn cards, glare, foils,
   non-English printings) and to *confirm* an OCR hit. Two hashes are compared per card: the
   **whole card** and an **art-crop** (just the illustration window). The art hash largely
   ignores the title bar, borders, and foil treatment, so it can still identify a foil whose
   whole-card hash is thrown off by glare.

A name lookup that the image hash also agrees with is reported as **HybridConfirmed** with
high confidence.

The library is a full collection manager: you can correct the exact **printing**, mark
**foil / condition / language**, search **Scryfall manually** to add or fix a card, edit
quantities, delete rows, and **export** to formats that import cleanly into Moxfield,
Archidekt, and spreadsheets.

---

## Download

Grab your platform's build from the **[latest release](../../releases/latest)**. Each one is
self-contained — the runtime, OpenCV, the OCR models, and a pre-built card index are all
bundled, so **nothing needs to be installed**.

| Platform | Download | Run it |
|----------|----------|--------|
| **Windows** 10/11 | `CardboardScanner-windows.zip` | Unzip, run `CardboardScanner.exe` |
| **macOS** 11+ | `CardboardScanner-macos.zip` | Unzip, move `Cardboard Scanner.app` to Applications |
| **Linux** (x64) | `CardboardScanner-linux.tar.gz` | `tar -xzf …` then `./CardboardScanner` |

The builds are **unsigned**, so the first launch needs a nudge:

- **Windows** SmartScreen says *"Windows protected your PC"* → **More info ▸ Run anyway**.
- **macOS** Gatekeeper refuses an unidentified developer → **right-click ▸ Open**, then allow
  camera access when prompted.

**First launch is fast.** A pre-built hash index of ~116,000 cards ships inside the app and
loads in about a second, so image matching works immediately. The app then fetches only cards
printed since that index was built — a few hundred cards, not a 550 MB download. Untick
**Auto-update index** to skip even that.

---

## Requirements

**To run a release build:** nothing but the OS and a video device. Internet access is used
for Scryfall lookups (card details, prices, printings) and for topping up the index.

**To run from source:** Python 3.12+ (see [`python/`](python/README.md)). The legacy Windows
version needs the [.NET 8 SDK](https://dotnet.microsoft.com/download) instead.

---

## Two versions

This repo contains the cross-platform rewrite and the original Windows app:

| | [`python/`](python/README.md) — **current** | [`src/`](src/) — legacy |
|---|---|---|
| Platforms | Windows, macOS, Linux | Windows only |
| Stack | Python + PySide6 (Qt) | C# + WPF |
| OCR | RapidOCR (bundled ONNX models) | `Windows.Media.Ocr` |
| Card index | **ships pre-built** (~116k cards) | built on first run (hours) |

The C# version still works and is kept for reference; new work happens in `python/`.

### Migrating from the Windows (C#) version

- **Your library carries over automatically.** Both versions read the same
  `collection` table in the same database file, so scanned cards, quantities, finishes, and
  conditions are all there on first launch.
- **The image index does not carry over, and does not need to.** The two versions hash
  images differently, so the Python app keeps its index in a separate table
  (`match_index_py`) and ships one pre-built. Nothing is lost and the C# app keeps working —
  see [`python/README.md`](python/README.md#important-hashes-are-not-interchangeable-with-the-c-version)
  for why.
- You can run either version, in any order, without breaking the other.

---

## Build & run from source

The cross-platform app lives in [`python/`](python/README.md):

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r python/requirements.txt
```

```bash
.venv/Scripts/python python/app.py
```

Headless verification of the whole non-UI pipeline (database, hashing, detection, OCR,
Scryfall, matcher, exports, index pack, camera naming):

```bash
.venv/Scripts/python python/selftest.py
```

Packaging for all three platforms is automated in
[`.github/workflows/build-python.yml`](.github/workflows/build-python.yml); see
[`python/README.md`](python/README.md#packaging) for local builds.

<details>
<summary>Legacy Windows (C#) build commands</summary>

```bash
dotnet run --project src/CardScanner/CardScanner.csproj -c Release
```

Headless self-test (writes `%TEMP%\cardscanner_selftest.log`):

```bash
CardScanner.exe --selftest
```

Single self-contained exe (~110 MB):

```bash
dotnet publish src/CardScanner/CardScanner.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true
```

</details>

---

## Using the app

1. **Select a device** from the dropdown — it lists cameras by name (press **Refresh** to
   re-scan).
2. Press **▶ Start**. The live feed appears on the left.
3. Hold a card up to the camera, filling a good portion of the frame, reasonably flat and
   well lit. A detected card is warped upright and identified; the best match shows on the
   right with its Scryfall image, set/rarity/price, and a confidence bar.
4. With **Auto-add matches** ticked, a card that stays matched with ≥80% confidence across
   consecutive frames is automatically added to your library. Otherwise press
   **＋ Add to library**. Scanning the same printing again increments its quantity.
5. Your library is listed bottom-right and persists between runs.

### Tips for good scans

- Fill the frame with the card and keep it flat (parallel to the lens).
- Even, glare-free lighting helps OCR a lot — angle the card slightly to kill reflections
  on foils.
- A plain, dark, contrasting background makes card-edge detection more reliable.
- **Focus:** webcams often hunt or lock onto the background at close range. Use the focus
  controls under the feed — hit **Refocus**, or turn **Auto** off and set the manual slider
  so the card is sharp (best for a fixed overhead setup). **Camera settings…** opens your
  webcam driver's own dialog if the in-app controls don't take effect. Note that **Zoom is
  digital**, so it magnifies any blur — getting the card larger in the real frame beats
  zooming in.

---

## Getting the printing right (foil / condition / language)

OCR + name lookup returns Scryfall's *default* printing — but a card's value and how it
imports into Moxfield/Archidekt depend on the **exact printing**. The current-match panel
lets you fix this before adding:

- **Printing** dropdown — every printing of the matched card (newest first, pulled live
  from Scryfall). Pick the set/collector number you actually own.
- **Foil** checkbox, **Cond** (NM/LP/MP/HP/DMG), and **Lang** (language) — these apply to
  the copy you add. They're **sticky**: set them once and every subsequent add keeps them,
  so scanning a stack of NM English non-foils is one setting.

A copy is uniquely a *printing + finish + condition + language*. Adding an identical copy
increments its quantity; a different finish/condition/language becomes its own row.

## Adding cards manually (Scryfall search)

Not every card scans cleanly (heavy wear, weird lighting, tokens). Use the **Manual search**
box: type a name (or any Scryfall query), press Enter, pick a result — it loads into the
current-match panel with its printing list, where you set foil/condition/language and press
**＋ Add to library**. This is also how you correct a misread: search the right card and add
it instead.

## Managing your library

- **Filter** box — live-filters the grid by name, set, or type.
- **Sort** — click any column header.
- **Edit selected row** (panel below the grid) — **＋ / －** adjust quantity, and the
  **Foil / Cond / Lang** controls edit that copy in place. Changes save automatically.
- **🗑 Delete row** removes the entry entirely.
- The summary shows unique rows, total cards, and total value (foil-aware, from the last
  known Scryfall prices).

---

## Exporting your collection

Pick a format in the **Export** bar under the library, then **Export to file…** or **Copy**
(to clipboard). Export respects the current filter, so you can export just a subset by
filtering first.

| Format | Use it for |
|--------|------------|
| **Moxfield / Arena — deck text** | Paste into Moxfield's or MTG Arena's import box. Lines look like `1 Lightning Bolt (2X2) 117 *F*` (`*F*` marks foil). |
| **Moxfield — collection CSV** | Moxfield ▸ Collection ▸ Import ▸ CSV. Columns are matched by name (`Count, Name, Edition, Condition, Language, Foil, Collector Number, …`); foil is `foil`/blank, condition/language use full names. |
| **Archidekt — collection CSV** | Archidekt collection importer. Includes the **Scryfall ID** column — set the importer's last option to *Scryfall ID* so it picks the exact printing instead of guessing. |
| **Plain deck list** | Universal `1 Card Name` text that pastes into almost any site. |
| **Generic CSV / spreadsheet** | A rich CSV (count, name, set, collector #, foil, condition, language, price, type, Scryfall ID + URL) for Excel/Sheets or Deckbox-style importers. |

**Importing into Moxfield:** Collection ▸ *Import* ▸ choose *CSV* and paste/upload the
Moxfield CSV, or use *Text* and paste the deck-text export.
**Importing into Archidekt:** open a Collection ▸ *Import* ▸ upload the Archidekt CSV, and
when prompted map the printing key to **Scryfall ID** for exact matches.

---

## The image index (perceptual-hash matching)

Each card contributes two hashes: the whole card and its art crop.

### It ships pre-built

A pack of **~116,000 cards in 3.83 MB** is bundled inside the app and imported in about a
second on first launch, so image matching works right away. Nothing is downloaded to get
there.

### Automatic updates

With **Auto-update index** ticked (default), the app keeps the index current on its own. On
launch it imports the bundled pack if needed, then asks Scryfall for cards printed since the
pack was built (a `date>=` search — typically a few hundred cards) and hashes just those. It
re-checks about once a week.

> This replaces what the legacy version did, which was to download Scryfall's 558 MB bulk
> file *just to discover what was missing*. The date query returns the same answer for a
> fraction of the bandwidth.

**Full rebuild** re-hashes every printing from the bulk data. It exists for completeness —
you should not normally need it, since it takes far longer and produces the same result as the
bundled pack plus a top-up. It downloads the bulk file to disk first, then hashes images
concurrently, and is **resumable**: interrupt it and re-run to continue where it stopped.

### If a card is mis-identified or unmatched

- A **partial index** is the usual culprit: if the exact card hasn't been hashed yet, the
  image matcher has nothing correct to match against. The matcher now **refuses to guess** —
  it requires a close hash match that clearly beats the runner-up, and otherwise reports
  *"no confident match"* with the reason, rather than returning a confident-but-wrong
  nearest neighbour. Finish the index build (or use **Manual search** to add the card).
- **Lighting matters:** dim light and foil glare defeat the title OCR. The app enhances the
  title strip (contrast equalization + thresholding) before reading, but even lighting with
  minimal glare, the card filling the frame (use **Zoom**), and a flat angle make a big
  difference.

---

## Where data is stored

Everything lives in a single SQLite database:

| Platform | Location |
|----------|----------|
| Windows | `%LOCALAPPDATA%\CardScanner\cardscanner.db` |
| macOS | `~/Library/Application Support/CardboardScanner/cardscanner.db` |
| Linux | `~/.local/share/cardboard-scanner/cardscanner.db` |

- `collection` — your scanned library (name, set, collector number, rarity, mana cost,
  type, USD + foil price snapshot, **foil / condition / language**, quantity, timestamp,
  Scryfall links). Shared by both versions.
- `match_index_py` / `match_index` — the perceptual-hash index. The two versions hash
  differently, so each keeps its own table and neither disturbs the other.
- `meta` — index bookkeeping (which hasher built it, how current it is, settings).

Older databases are migrated automatically (the foil/condition/language/foil-price columns
are added in place; existing rows default to non-foil / NM / English).

Set `CARDBOARD_DB` to use a different database file. Delete the file to reset everything;
back it up to preserve your library — the index rebuilds itself, your library does not.

---

## How it works (architecture)

```
Camera (OpenCV) ─► CardDetector ─► warped upright 488x680 card
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
                 OcrService                  PerceptualHasher
          (contrast-enhanced title     (whole-card + art-crop 64-bit
           strip, several passes)       hashes vs. the local index)
                     │                               │
                     └────────► CardMatcher ◄────────┘
                                    │  hybrid: name lookup, hash
                                    │  fallback, hash confirmation,
                                    │  margin test to refuse guesses
                                    ▼
                              ScryfallClient
                    (fuzzy name / by-id / search / printings,
                     bulk data, images — rate limited)
                                    │
                                    ▼
                            Database (SQLite)
                       collection + index + meta
```

Both trees share this structure. Python files are under `python/cardboard/`, the legacy C#
equivalents under `src/CardScanner/Services/`:

| Module | Responsibility |
|--------|----------------|
| `camera` | Frame capture on a background thread; per-OS device names; focus control |
| `detector` | Contour/quad detection + perspective warp to a flat 488×680 card |
| `ocr` | Reads the card name from the title strip, trying several enhanced variants |
| `hashing` | Whole-card and art-crop pHash + Hamming-distance comparison |
| `scryfall` | Rate-limited API: fuzzy name, by-id, search, printings, bulk data, images |
| `index_builder` | Imports the bundled pack, incremental top-ups, and full rebuilds |
| `indexpack` | Read/write the compact shipped index pack *(Python only)* |
| `matcher` | Hybrid identification, confidence scoring, and the margin test |
| `exporter` | Moxfield / Archidekt / plain / generic CSV serialisation |
| `database` | SQLite storage for the library and the index, with schema migration |
| `ui/main_window` | Live feed, match panel, manual search, editable library, export |

### Scryfall etiquette

The client sends an identifying `User-Agent`, accepts JSON, and spaces API requests
~100 ms apart, per Scryfall's [API guidelines](https://scryfall.com/docs/api). Shipping a
pre-built index means normal use makes almost no bulk requests at all. Scryfall data and
images are © Wizards of the Coast / Scryfall and used per their terms; the bundled pack
contains only non-reversible 64-bit fingerprints and card identifiers, never artwork.

---

## Tuning

Detection and matching thresholds are constants you can adjust in code (Python names shown;
the C# properties match):

- `CardDetector.min_area_fraction` — how much of the frame a card must fill to be considered.
- `CardMatcher.phash_accept_distance` — max Hamming distance to accept a pure image-hash match.
- `CardMatcher.phash_margin_requirement` — how far the best match must beat the runner-up.
- `CardMatcher.phash_confirm_distance` — max distance for the image to *confirm* an OCR name.
- `PROCESS_INTERVAL` in `ui/main_window.py` — how often frames are analysed.
- The auto-add confidence threshold (`0.80`) and frame-stability requirement in `_on_match`.
- `hashing.ART_X0/Y0/X1/Y1` — the art window used for the art-crop hash. Changing these
  invalidates an existing index.

---

## Troubleshooting

- **"No video devices found."** Press **Refresh**. Close other apps using the camera
  (Discord/Teams/Zoom/Camera app).
- **"Camera unavailable" when starting.** Another application holds the camera. Close it, or
  pick a different device. On Windows also check Settings ▸ Privacy ▸ Camera ▸ *"Let desktop
  apps access your camera"*; on macOS, allow camera access on first launch (System Settings ▸
  Privacy & Security ▸ Camera).
- **Blurry when zoomed.** Zoom is *digital*, so it magnifies blur. Use the focus controls —
  **Refocus**, or turn **Auto** off and set the manual slider. Getting the card larger in the
  real frame beats zooming.
- **Poor OCR reads.** Improve lighting and reduce glare; hold the card flat and filling the
  frame. Image-hash matching backs OCR up regardless.
- **Wrong or missing match.** The matcher refuses to guess when the nearest indexed image is
  ambiguous, and says why in the status bar. Use **Manual search** to add the card, and the
  **Printing** dropdown to pick the exact set.
- **macOS: "app is damaged" or won't open.** The build is unsigned — right-click ▸ **Open**
  rather than double-clicking. If Gatekeeper still refuses:
  `xattr -d com.apple.quarantine "/Applications/Cardboard Scanner.app"`.
- **Linux: fails to start with a Qt/xcb error.** Install the usual Qt runtime libraries:
  `sudo apt install libgl1 libegl1 libxkbcommon-x11-0 libdbus-1-3 libxcb-cursor0`.

---

## License

Cardboard Scanner is free software under the **[GNU General Public License v3.0](LICENSE)**.
You may use, study, modify, and redistribute it; distributed modified versions must also be
released under the GPL, with source.

The released binaries bundle third-party components under their own licenses — most notably
**Qt via PySide6 under the LGPL-3.0**, which grants you the right to swap in your own Qt build
and relink. See **[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)** for the full list and for
how that right is satisfied here.

Magic: The Gathering is © Wizards of the Coast; this is unofficial Fan Content, not approved
or endorsed by Wizards. Card data comes from [Scryfall](https://scryfall.com) under their
terms — the bundled index holds only non-reversible hashes and identifiers, never artwork.
