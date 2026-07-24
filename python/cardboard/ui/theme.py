"""Dark theme.

Qt does not follow the OS dark mode on Windows either, so the palette is set explicitly
rather than inherited — the same lesson the WPF version taught (system colours rendered
dark-on-dark and were unreadable).
"""

BG = "#1E1E22"
PANEL = "#26262C"
RAISED = "#2E2E36"
BORDER = "#4A4A55"
TEXT = "#E8E8EC"
MUTED = "#8888AA"
ACCENT = "#3D5A80"
OK = "#5AA469"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 12px;
}}
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {MUTED}; font-size: 11px; }}
QLabel#heading {{ font-size: 15px; font-weight: bold; }}
QLabel#matchName {{ font-size: 19px; font-weight: bold; }}
QLabel#video {{ background-color: #000000; border-radius: 6px; }}
QLabel#cardImage {{ background-color: #111111; border-radius: 4px; }}

QFrame#panel {{
    background-color: {PANEL};
    border-radius: 6px;
}}
QFrame#raised {{
    background-color: {RAISED};
    border-radius: 4px;
}}

QPushButton {{
    background-color: #3A3A44;
    color: {TEXT};
    border: none;
    border-radius: 3px;
    padding: 5px 10px;
}}
QPushButton:hover {{ background-color: #4C4C5A; }}
QPushButton:pressed {{ background-color: #55556A; }}
QPushButton:disabled {{ color: #777788; background-color: #303038; }}

QLineEdit {{
    background-color: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 4px 6px;
    selection-background-color: {ACCENT};
}}

QComboBox {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 3px 6px;
    min-height: 20px;
}}
QComboBox:hover {{ border-color: #6A6A7A; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}

QCheckBox {{ color: {TEXT}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {BG};
}}
QCheckBox::indicator:checked {{ background-color: {ACCENT}; border-color: {ACCENT}; }}

QSlider::groove:horizontal {{ height: 4px; background: #444450; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 12px; margin: -5px 0;
    background: {TEXT}; border-radius: 6px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}

QProgressBar {{
    background-color: #333340;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background-color: {OK}; border-radius: 3px; }}

QTableWidget {{
    background-color: {RAISED};
    alternate-background-color: #333340;
    color: {TEXT};
    gridline-color: transparent;
    border: none;
    selection-background-color: {ACCENT};
    selection-color: #FFFFFF;
}}
QHeaderView::section {{
    background-color: #33333C;
    color: {TEXT};
    padding: 5px;
    border: none;
    border-right: 1px solid {BORDER};
    font-weight: 600;
}}
QTableWidget::item:selected {{ background-color: {ACCENT}; color: #FFFFFF; }}

QListWidget {{
    background-color: {BG};
    color: {TEXT};
    border: 1px solid #3A3A44;
    border-radius: 3px;
}}
QListWidget::item:selected {{ background-color: {ACCENT}; color: #FFFFFF; }}
QListWidget::item:hover {{ background-color: #33333C; }}

QStatusBar {{ background-color: {PANEL}; color: {TEXT}; }}
QToolTip {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #4A4A55; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QSplitter::handle {{ background: transparent; }}
"""
