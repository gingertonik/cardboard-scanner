"""Cardboard Scanner — application entry point.

    python app.py              launch the GUI
    python app.py --selftest   run the headless checks (see selftest.py)
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
