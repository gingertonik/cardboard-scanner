"""Headless GUI checks.

Verifies that data produced on background threads actually reaches the widgets. An earlier
version of the window used ``QTimer.singleShot`` from worker threads, which silently never
fires (no event loop on those threads), so the device list stayed empty, card images never
appeared, and camera-error dialogs never opened. Widget-exists assertions did not catch it;
these do.

Run:  python tools/gui_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never touch the real library.
os.environ["CARDBOARD_DB"] = str(Path(tempfile.gettempdir()) / f"cardboard_gui_{uuid.uuid4().hex}.db")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}", flush=True)
    if not ok:
        failures.append(name)


def pump(app, predicate, timeout: float = 20.0) -> bool:
    """Run the event loop until predicate() is true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from cardboard import camera as camera_mod
    from cardboard.database import Database
    from cardboard.ui.main_window import MainWindow
    from cardboard.ui.theme import STYLESHEET

    # Keep the smoke test offline: no index sync on launch.
    Database().set_meta("auto_index", "0")

    expected = camera_mod.enumerate_devices()
    print(f"library sees {len(expected)} device(s): "
          f"{', '.join(d.name for d in expected) or '(none)'}\n", flush=True)

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    app.processEvents()

    check("window builds", window.windowTitle() == "Cardboard Scanner", window.windowTitle())

    # The device list is populated from a worker thread — the real regression test.
    if expected:
        arrived = pump(app, lambda: window.device_combo.count() > 0)
        listed = [window.device_combo.itemText(i) for i in range(window.device_combo.count())]
        check("device list reaches the combo box (cross-thread signal)",
              arrived and len(listed) == len(expected), f"{len(listed)}/{len(expected)}: {listed}")
        check("device names, not indices", all(not n.isdigit() for n in listed), str(listed))
        check("a device is preselected", window.device_combo.currentData() is not None,
              str(window.device_combo.currentText()))
    else:
        print("SKIP  device list — no cameras on this machine", flush=True)

    check("export formats populated", window.export_combo.count() == 5,
          str(window.export_combo.count()))
    check("conditions populated", window.condition_combo.count() == 5)
    check("languages populated", window.language_combo.count() > 5)
    check("library table has 8 columns", window.table.columnCount() == 8)
    check("edit panel starts disabled", not window.edit_panel.isEnabled())
    check("stop starts disabled", not window.stop_button.isEnabled())
    check("index status reported", bool(window.index_label.text()))

    window.close()
    app.processEvents()

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S): {', '.join(failures)} ===")
        return 1
    print("=== GUI SMOKE TESTS PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
