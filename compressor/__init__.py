"""Compressor: one place for real disk savings.

Copyright (C) 2026 Lanntern. Licensed under the GNU General Public License, version 3 only (see LICENSE).

Converts images (JPEG/PNG -> JPEG XL or WebP) and videos (-> AV1 or HEVC)
using well-known command-line encoders, verifies every result, and only then
replaces the original (which goes to the Recycle Bin / Trash so it can be
undone).
"""

__version__ = "0.1.0"
APP_NAME = "Compressor"
