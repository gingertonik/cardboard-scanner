"""Cardboard Scanner — cross-platform Python implementation.

Port of the original Windows/WPF app. The service layer mirrors the C# design:
camera -> card detection -> (OCR name lookup | perceptual/art hash) -> SQLite library.
"""

__version__ = "2.0.0-dev"
