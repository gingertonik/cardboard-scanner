"""Cardboard Scanner — application entry point.

    python app.py              launch the GUI
    python app.py --selftest   run the headless checks (see selftest.py)

Copyright (C) 2026 Cardboard Scanner contributors.

This program is free software: you can redistribute it and/or modify it under the terms of
the GNU General Public License as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU General Public License for more details: <https://www.gnu.org/licenses/>.

Uses Qt via PySide6 under the LGPL-3.0 — see THIRD-PARTY-NOTICES.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    if "--selftest" in sys.argv:
        import selftest
        return selftest.main()

    from PySide6.QtWidgets import QApplication

    from cardboard.ui.main_window import MainWindow
    from cardboard.ui.theme import STYLESHEET

    app = QApplication(sys.argv)
    app.setApplicationName("Cardboard Scanner")
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
