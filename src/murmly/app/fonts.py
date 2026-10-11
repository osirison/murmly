"""The fonts the app ships, and how it registers them with Qt.

Instrument Sans for anything a person reads, IBM Plex Mono for anything the
machine owns. Both are SIL OFL fonts, latin subsets kept as files in this
package (see `fonts/README.md` for their sources) and loaded from there, so
the app renders the same with no network and no font installed on the system.
"""

from __future__ import annotations

from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# Only the weights the design's type scale uses: 400, 500 and 600 for the sans,
# 400 and 600 for the mono.
FONT_FILES = (
    "InstrumentSans-Regular.ttf",
    "InstrumentSans-Medium.ttf",
    "InstrumentSans-SemiBold.ttf",
    "IBMPlexMono-Regular.ttf",
    "IBMPlexMono-SemiBold.ttf",
)

# Each font's licence, in the repository's `licenses/` directory, by the font
# file prefix before the weight.
LICENSE_FILES = {
    "InstrumentSans": "instrument-sans-ofl.txt",
    "IBMPlexMono": "ibm-plex-mono-ofl.txt",
}


def font_paths() -> list[Path]:
    return [FONTS_DIR / name for name in FONT_FILES]


def load_fonts() -> list[Path]:
    """Register every bundled font with Qt; return the files Qt refused.

    Needs a Qt application to exist already. An empty list means all loaded.
    """
    from PySide6.QtGui import QFontDatabase

    return [
        path
        for path in font_paths()
        if QFontDatabase.addApplicationFont(str(path)) < 0
    ]
