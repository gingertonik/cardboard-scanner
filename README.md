# MTG Card Scanner

A Windows desktop app that watches a live video device (webcam, capture card, document
camera), detects a Magic: The Gathering card in frame, identifies it, cross-matches it
against the [Scryfall](https://scryfall.com) database, and files it into a local library
of everything you've scanned.

Matching is **hybrid**:

1. **OCR the title** — the card's name is read from the top strip using the built-in
   Windows OCR engine (no Tesseract binaries to install), then looked up on Scryfall via
   fuzzy name search.
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

Grab **`CardScanner.exe`** from the **[latest release](../../releases/latest)** and run it —
it's a single self-contained file (the .NET runtime and all native libraries are bundled in),
so **nothing needs to be installed**. Windows 10/11 only.

- On first run, Windows SmartScreen may say *"Windows protected your PC"* because the file
  isn't code-signed — click **More info ▸ Run anyway**.
- **First launch downloads the card index.** In the background it fetches Scryfall's full
  card data (~550 MB) and hashes every card image so it can match cards by picture. This is a
  one-time download that runs while you use the app — you can start scanning by name (OCR)
  immediately; image-hash matching improves as it fills in. It re-syncs new cards about once a
  week. You can turn this off with the **Auto-update index** checkbox, or trigger it manually.

---

## Requirements

- Windows 10 (build 19041 / 2004) or Windows 11
- [.NET 8 SDK](https://dotnet.microsoft.com/download) (or newer) — the project targets
  `net8.0-windows10.0.19041.0`
- A connected video device (USB webcam, capture card, etc.)
- Internet access for Scryfall lookups and to build the image index

Everything else (OpenCV, image hashing, SQLite, OCR) comes from NuGet or Windows itself.

> **Just want to run it?** Grab `CardScanner.exe` from the
> [Releases](../../releases) page — it's a single self-contained file with the .NET runtime
> and all native libraries bundled in, so **no installs are needed**. Windows 10/11 only.
> On first launch, Windows SmartScreen may say *"Windows protected your PC"* because the
> download is unsigned — click **More info ▸ Run anyway**.

---

## Build & run

```bash
dotnet build src/CardScanner/CardScanner.csproj -c Release
```

Then launch the app:

```bash
dotnet run --project src/CardScanner/CardScanner.csproj -c Release
```

or run the built executable at
`src/CardScanner/bin/Release/net8.0-windows10.0.19041.0/win-x64/CardScanner.exe`.

### Headless self-test

The non-UI pipeline (database, hashing, card detection, OCR, Scryfall lookup/search/
printings, hybrid matcher, finish-based dedup, and all export formats) can be verified
without opening the window:

```bash
CardScanner.exe --selftest
```

Results print to the console and are written to `%TEMP%\cardscanner_selftest.log`.

### Publishing a standalone exe

To produce the single self-contained `CardScanner.exe` that runs with no prerequisites:

```bash
dotnet publish src/CardScanner/CardScanner.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true
```

The exe (~110 MB) lands in
`src/CardScanner/bin/Release/net8.0-windows10.0.19041.0/win-x64/publish/`. Upload that file
as a GitHub Release asset so others can download and run it directly.

---

## Using the app

1. **Select a device** from the dropdown (press **Refresh** to re-scan device indices).
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

### Automatic updates

With **Auto-update index** ticked (default, in the toolbar), the app maintains the index for
you: on first launch it builds the **full** index in the background (you can scan while it
works), and on later launches it re-syncs only if the index is more than a week old,
downloading the current card list and hashing **only cards that are new or missing an art
hash** — everything already indexed is skipped. Untick it to manage the index manually with
the buttons below. Each card contributes a whole-card hash and an art-crop hash.

> The weekly re-sync still downloads the bulk *list* (to see what's new) even though it only
> fetches images for missing cards — that's the only way to diff against Scryfall. Manual
> builds are always available and are resumable.

Out of the box the app can also match by **name via OCR + Scryfall** with no local index.
To (re)build the local image index manually:

- **Build image index** — Scryfall's *unique-artwork* set (~265 MB, ~50k cards). One entry
  per distinct illustration; enough to identify *what* a card is. **Recommended.**
- **Build FULL index** — *every* printing (~558 MB, ~100k cards). Larger/slower, but lets
  the image hash distinguish specific set printings.

The build:

1. **downloads the bulk file to a temp file first** (a single bounded transfer), then
2. parses it from disk and **hashes card images with several concurrent downloads**.

It is **resumable** — an intact downloaded file is reused, and cards already in the index
are skipped, so you can cancel and re-run. Progress (MB downloaded, then cards hashed)
shows in the status bar.

> **Time:** the image-hashing pass is the slow part — roughly 15–30 minutes for
> unique-artwork and longer for the full set, depending on your connection. It's a one-time
> job; later runs just fill in new cards. **The count climbs into the tens of thousands —
> if it stops at only a few thousand, the build was interrupted; just run it again to
> resume.**

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

```
%LOCALAPPDATA%\CardScanner\cardscanner.db
```

- `collection` — your scanned library (name, set, collector number, rarity, mana cost,
  type, USD + foil price snapshot, **foil / condition / language**, quantity, timestamp,
  Scryfall links).
- `match_index` — the perceptual-hash index built from Scryfall bulk data.

Older databases are migrated automatically (the foil/condition/language/foil-price columns
are added in place; existing rows default to non-foil / NM / English).

Delete the file to reset everything; back it up to preserve your library.

---

## How it works (architecture)

```
Camera (OpenCvSharp) ─► CardDetector ─► warped upright card
                                          │
                        ┌─────────────────┼───────────────────┐
                        ▼                                     ▼
                  OcrService                          PerceptualHasher
             (Windows.Media.Ocr on           (64-bit pHash vs. local index,
              the title strip)                 Hamming-distance search)
                        │                                     │
                        └──────────────► CardMatcher ◄────────┘
                                            │  (hybrid: name lookup,
                                            │   hash fallback, hash confirm)
                                            ▼
                                      ScryfallClient
                                   (fuzzy name / by-id,
                                    rate-limited)
                                            │
                                            ▼
                                    Database (SQLite)
                                   collection + match_index
```

| File | Responsibility |
|------|----------------|
| `Services/CameraService.cs`   | Background frame capture from the video device |
| `Services/CardDetector.cs`    | Contour/quad detection + perspective warp to a flat 488×680 card |
| `Services/OcrService.cs`      | Reads the card name from the title strip (Windows OCR) |
| `Services/PerceptualHasher.cs`| pHash compute + Hamming-distance comparison |
| `Services/ScryfallClient.cs`  | Rate-limited Scryfall API (fuzzy name, by-id, **search**, **printings**, bulk data, images) |
| `Services/IndexBuilder.cs`    | Builds/updates the local pHash index from Scryfall bulk data |
| `Services/CardMatcher.cs`     | Hybrid identification logic and confidence scoring |
| `Services/CollectionExporter.cs` | Serializes the library to Moxfield / Archidekt / plain / generic CSV formats |
| `Services/Database.cs`        | SQLite storage for the library and the index (with schema migration) |
| `MainViewModel.cs` / `MainWindow.xaml` | WPF UI: live feed, current-match + printing/finish pickers, manual search, editable library, export |

### Scryfall etiquette

The client sends an identifying `User-Agent`, accepts JSON, and spaces API requests
~100 ms apart, per Scryfall's [API guidelines](https://scryfall.com/docs/api). Card images
are downloaded from Scryfall's CDN with an added politeness delay during index builds.
Scryfall data and images are © Wizards of the Coast / Scryfall and used per their terms.

---

## Tuning

Detection and matching thresholds are constants you can adjust in code:

- `CardDetector.MinAreaFraction` — how much of the frame a card must fill to be considered.
- `CardMatcher.PhashAcceptDistance` — max Hamming distance to accept a pure image-hash match.
- `CardMatcher.PhashConfirmDistance` — max distance for the image to *confirm* an OCR name.
- `MainViewModel.ProcessIntervalMs` — how often frames are analyzed.
- The auto-add confidence threshold (`0.80`) and frame-stability requirement in
  `MainViewModel.OnMatch`.

---

## Troubleshooting

- **"No video devices found."** Press **Refresh**. Close other apps using the camera
  (Teams/Zoom/Camera app). Try different device indices.
- **Feed is black / won't start.** Another application may hold the camera exclusively, or
  Windows camera privacy settings block desktop apps (Settings ▸ Privacy ▸ Camera ▸ "Let
  desktop apps access your camera").
- **Poor OCR reads.** Improve lighting, reduce glare, fill more of the frame, hold the card
  flatter. Build the image index so hash matching can back up OCR.
- **Wrong printing identified.** OCR name lookup returns Scryfall's default printing. Build
  the **FULL** index to distinguish printings by image.
