# Cardboard Scanner — Python port (in progress)

Cross-platform rewrite of the Windows/WPF app, targeting **Windows, macOS, and Linux**.
The original C# app remains in `../src` and is unaffected.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Scaffold, venv, dependencies | ✅ done |
| 1 | Headless core + self-test parity | ✅ **all 11 checks pass** |
| 2 | Camera capture, focus, per-OS device names | ✅ core done (verified on Windows) |
| 3 | PySide6 GUI to feature parity | ✅ built (smoke-tested headless) |
| 4 | Packaging (PyInstaller) + CI for 3 OSes | ✅ Windows verified; mac/Linux build in CI, untested |
| 5 | Cutover: docs, releases | ⏳ |

The whole app is ported: models, database, hashing, detection, OCR, Scryfall client, hybrid
matcher, index builder, export, camera, and the PySide6 GUI.

```bash
.venv/Scripts/python app.py
```

## Bundled index — first run takes seconds, not hours

`cardboard/data/index-pack.cbix` ships a pre-built hash index of **116,037 cards in
3.83 MB** (34.6 bytes/card). On first run it is imported in ~1.5 s instead of downloading
and hashing 116k images. After that, only cards printed since the pack was built are
fetched, via a Scryfall `date>=` query — a few hundred cards rather than the 558 MB bulk
file.

Verified end to end: importing the pack and matching real card images with OCR disabled
identifies them from the artwork alone (Sol Ring and Pinnacle Monk at Hamming distance 0),
including across image sizes, since the index is built from "small" images.

Regenerate the pack at release time:

```bash
python tools/build_full_index.py            # one-time full build into a scratch DB
```

```bash
CARDBOARD_DB=<scratch.db> python tools/build_index_pack.py cardboard/data/index-pack.cbix
```

Set `CARDBOARD_DB` to point the app at a different database (handy for testing).

## Setup

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

(On macOS/Linux use `.venv/bin/python` instead of `.venv/Scripts/python`.)

## Run the self-test

Verifies the whole non-UI pipeline — database, hashing, detection, OCR, Scryfall
lookup/search/printings, the hybrid matcher, finish-based dedup, export formats, and
camera device naming:

```bash
.venv/Scripts/python selftest.py
```

## Packaging

```bash
python -m pip install -r requirements-dev.txt
```

```bash
pyinstaller packaging/cardboard.spec --noconfirm
```

Produces a single ~141 MB executable on Windows/Linux and a `Cardboard Scanner.app` bundle
on macOS (a bundle is required there — a bare binary cannot request camera permission).
PyInstaller cannot cross-compile, so [`build-python.yml`](../.github/workflows/build-python.yml)
builds all three on their own runners and attaches them to a `v*` tag release.

CI runs the self-test twice — from source *and* against the frozen build. That second run
matters: it is what caught OCR silently failing because Pillow had been excluded from the
bundle (RapidOCR imports Pillow internally even though this app never does). A frozen build
must report `PASS OCR` and `PASS Bundled index pack`, not `SKIP`.

Both builds are **unsigned**, so first launch needs a nudge: Windows SmartScreen →
*More info ▸ Run anyway*; macOS Gatekeeper → right-click ▸ *Open*.

## Platform notes

| Concern | Windows | macOS | Linux |
|---|---|---|---|
| Device names | `pygrabber` (DirectShow) | `system_profiler` | sysfs `/sys/class/video4linux` |
| Capture backend | `CAP_DSHOW` | `CAP_AVFOUNDATION` | `CAP_V4L2` |
| Focus control | ✅ driver-dependent | ⚠️ limited AVFoundation property support | ✅ via V4L2 |
| Native camera dialog | ✅ | ✗ | ✗ |

OCR uses **RapidOCR (ONNX)** rather than the Windows-only `Windows.Media.Ocr`; it bundles
its own models, so there is no external Tesseract install.

## Important: hashes are not interchangeable with the C# version

The C# app hashed images with CoenM.ImageHash. Reproducing its exact 64-bit output in
Python proved impossible — it resizes with ImageSharp's bicubic resampler, which yields
different 64×64 pixels than any OpenCV filter. Measured drift against real stored hashes
was ~23 bits (random is 32), i.e. structurally different rather than a tunable difference.

Consequences:

- **Your library (`collection`) is fully preserved and shared** — that schema is unchanged,
  so this opens the existing `cardscanner.db` directly.
- **The Python index lives in its own table** (`match_index_py`). If both apps shared one
  index table each would overwrite the other's hashes and quietly break its matching, so
  the C# app's `match_index` is left untouched and both keep working during the migration.
- No user-visible rebuild is needed anyway, because the bundled pack ships pre-hashed.
- `meta['index_hash_algo']` records which implementation built the index (`py-v1` here);
  the pack importer *refuses* a pack from a different hasher rather than trusting it.

Reproduce the measurement yourself:

```bash
.venv/Scripts/python tools/hash_parity.py 6
```
