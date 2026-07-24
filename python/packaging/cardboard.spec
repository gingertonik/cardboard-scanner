# PyInstaller spec for Cardboard Scanner.
#
# Build (from the python/ directory):
#     pyinstaller packaging/cardboard.spec --noconfirm
#
# Produces a single self-contained executable on Windows/Linux and a .app bundle on macOS.
# PyInstaller cannot cross-compile, so each OS must build on its own runner (see
# .github/workflows/build-python.yml).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parent          # the python/ directory
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

# The pre-built hash index ships with the app so first run costs seconds, not hours.
datas = [(str(ROOT / "cardboard" / "data" / "index-pack.cbix"), "cardboard/data")]

# RapidOCR keeps its ONNX models and config as package data; without these OCR silently
# degrades to "unavailable" in a frozen build.
datas += collect_data_files("rapidocr_onnxruntime")
binaries = collect_dynamic_libs("onnxruntime")

hiddenimports = [
    "rapidocr_onnxruntime",       # imported lazily inside OcrService
    "onnxruntime",
    "selftest",                   # reachable via `app.py --selftest`
]
if IS_WINDOWS:
    hiddenimports += ["pygrabber", "pygrabber.dshow_graph", "comtypes"]

# Qt ships far more than this app uses; excluding the unused modules roughly halves the
# bundle. Only QtCore/QtGui/QtWidgets are needed.
excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "test",
    # NOTE: do not exclude PIL — RapidOCR imports Pillow internally, and dropping it makes
    # OCR fail at runtime with ModuleNotFoundError while the app still starts.
    "matplotlib", "scipy", "pandas", "setuptools", "pip",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSensors",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
    "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

if IS_MAC:
    # macOS wants an .app bundle; a bare one-file binary cannot request camera access.
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="CardboardScanner",
        console=False,
        argv_emulation=False,
    )
    coll = COLLECT(
        exe, a.binaries, a.datas,
        strip=False, upx=False, name="CardboardScanner",
    )
    app = BUNDLE(
        coll,
        name="Cardboard Scanner.app",
        bundle_identifier="com.cardboardscanner.app",
        info_plist={
            # Required, or macOS kills the app the moment it opens a camera.
            "NSCameraUsageDescription":
                "Cardboard Scanner uses your camera to scan Magic: The Gathering cards.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleShortVersionString": "2.0.0",
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="CardboardScanner",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,          # UPX often trips antivirus heuristics; not worth the size win
        runtime_tmpdir=None,
        console=False,      # GUI app: no console window
        disable_windowed_traceback=False,
    )
