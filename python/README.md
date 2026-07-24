# Cardboard Scanner — Python port (in progress)

Cross-platform rewrite of the Windows/WPF app, targeting **Windows, macOS, and Linux**.
The original C# app remains in `../src` and is unaffected.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 0 | Scaffold, venv, dependencies | ✅ done |
| 1 | Headless core + self-test parity | ✅ **all 11 checks pass** |
| 2 | Camera capture, focus, per-OS device names | ✅ core done (verified on Windows) |
| 3 | PySide6 GUI to feature parity | ⏳ next |
| 4 | Packaging (PyInstaller) + CI for 3 OSes | ⏳ |
| 5 | Cutover: docs, releases | ⏳ |

Everything except the GUI is ported: models, SQLite database, perceptual + art-crop
hashing, card detection, OCR, Scryfall client, hybrid matcher, index builder, and export.

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

- **Your library (`collection`) is fully preserved** — the schema is unchanged, so this
  opens the existing `cardscanner.db` directly.
- **The image index must be rebuilt** by the Python app. Index rows are derived data, so
  nothing irreplaceable is lost, but expect a one-time rebuild.
- `meta['index_hash_algo']` records which implementation built the index (`py-v1` here),
  so the two versions can never silently mix incompatible hashes.

Reproduce the measurement yourself:

```bash
.venv/Scripts/python tools/hash_parity.py 6
```
