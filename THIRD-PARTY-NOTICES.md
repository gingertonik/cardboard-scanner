# Third-party notices

Cardboard Scanner itself is licensed under **GPL-3.0** (see [LICENSE](LICENSE)). The released
binaries bundle the third-party components below, which remain under their own licenses.

## Qt / PySide6 — LGPL-3.0 (important)

The application uses **PySide6** and **shiboken6** (Qt for Python) under the
**GNU Lesser General Public License v3.0**. Full text: [LICENSES/LGPL-3.0.txt](LICENSES/LGPL-3.0.txt)
(the LGPL is a set of additional permissions on top of the GPL-3.0 in [LICENSE](LICENSE)).

The LGPL requires that recipients be able to modify the Qt libraries and relink the
application against their modified version. That is satisfied here:

- The complete source of this application is published at this repository.
- The Qt libraries are not modified — stock PySide6 wheels are installed from PyPI.
- Anyone can install a different PySide6 version and rebuild the app with
  `pyinstaller packaging/cardboard.spec` (see [python/README.md](python/README.md#packaging)),
  producing a binary linked against their own Qt build.

Qt is © The Qt Company Ltd. and contributors. Sources: <https://download.qt.io/>.

## Bundled in the released binaries

| Component | License | Used for |
|-----------|---------|----------|
| PySide6 / shiboken6 | LGPL-3.0-only | GUI toolkit (see above) |
| OpenCV (`opencv-python`) | Apache-2.0 | Camera capture, card detection, image ops |
| NumPy | BSD-3-Clause | Array maths behind hashing and detection |
| Requests | Apache-2.0 | Scryfall HTTP client |
| Pillow | MIT-CMU (HPND) | Imaging support required by RapidOCR |
| RapidOCR (`rapidocr-onnxruntime`) | Apache-2.0 | Card-title OCR |
| ONNX Runtime | MIT | Runs the OCR models |
| PyYAML | MIT | RapidOCR configuration |
| Shapely | BSD-3-Clause | RapidOCR text-region geometry |
| pyclipper | MIT (wraps Clipper, BSL-1.0) | RapidOCR polygon clipping |
| pygrabber | MIT | Windows DirectShow device names |
| comtypes | MIT | COM bindings used by pygrabber |

The OCR models shipped inside RapidOCR are derived from PaddleOCR and are Apache-2.0.

**Build-time only, not distributed:** PyInstaller is GPL-2.0 *with* the standard bootloader
exception, which explicitly permits packaging applications under any license. It does not
affect the licensing of the produced binaries.

## Legacy Windows (C#) app

The original `src/` app is retained for reference and is not part of current releases. It
depends on OpenCvSharp4 (Apache-2.0), CoenM.ImageSharp.ImageHash (MIT), Microsoft.Data.Sqlite
(MIT), SixLabors.ImageSharp (Six Labors Split License — free under Apache-2.0 terms for
open-source projects such as this one), and DirectShowLib.Standard. If you resume
distributing that build, confirm the current terms of those packages first.

## Card data and imagery

- Card names, set codes, and collector numbers come from **Scryfall** and are used under
  [Scryfall's terms](https://scryfall.com/docs/api). This project is not affiliated with or
  endorsed by Scryfall.
- Magic: The Gathering is © **Wizards of the Coast**. This is unofficial Fan Content permitted
  under the [Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
  Not approved or endorsed by Wizards.
- The bundled index contains **no artwork** — only non-reversible 64-bit perceptual
  fingerprints alongside card identifiers. Card images are fetched from Scryfall on demand and
  are never redistributed.
