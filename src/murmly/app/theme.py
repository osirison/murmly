"""The app's design tokens: colour, type, spacing and radius, as plain data.

Ported from the Proton Sync design system the canvas is built on (linked from
the change's proposal). No Qt import lives here, so a test on every platform can
assert that each colour has both a dark and a light value. `qt.py` hands this
data to QML through one object.

Colours are named by what they mean, as the design's colour table says:

* `listening-*`: warm (orange). Your voice going in.
* `writing-*`: cool (cyan to blue). Words coming back to you: writing, speaking.
* `needs-you-*`: crimson, as an outline or a faint tint, never a solid fill.
* `note-*`: amber. A note where nothing is at risk.
* everything else is neutral: surfaces, rules, text and buttons.

Left out, because Murmly has no screen for them: the canvas's diff view,
hexagon status mark, folder seam and compact tray-row tokens (`diff-*`, `hex-*`,
`seam*`, `compact-*`, `deletion-kind`), and the shadows, which the operating
system draws around a window.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

SCHEMES = ("dark", "light")
# The canvas leads with the dark theme, and a platform that cannot say which the
# person prefers (Qt reports it as unknown) gets the one the canvas leads with.
DEFAULT_SCHEME = "dark"

FONT_SANS = "Instrument Sans"
FONT_MONO = "IBM Plex Mono"


@dataclass(frozen=True)
class TextStyle:
    """One step of the type scale, in the canvas's own units."""

    size: float  # px
    weight: int  # CSS numeric weight, which is also Qt's
    line_height: float  # a multiple of the size
    letter_spacing_em: float
    family: str  # "sans" or "mono"
    uppercase: bool = False


COLORS: dict[str, dict[str, str]] = {
    # Surfaces and rules
    "surface": {"dark": "#0a0b0d", "light": "#faf8f5"},
    "panel": {"dark": "#0d0e11", "light": "#ffffff"},
    "panel-alt": {"dark": "#0e0f12", "light": "#fdfcfa"},
    "panel-raised": {"dark": "#101216", "light": "#ffffff"},
    "track-inert": {"dark": "#191c21", "light": "#edeae5"},
    "divider": {"dark": "#16181d", "light": "#edeae5"},
    "border-subtle": {"dark": "#1a1d22", "light": "#edeae5"},
    "border-window": {"dark": "#1a1d22", "light": "#e6e3de"},
    "border": {"dark": "#23262d", "light": "#e6e3de"},
    "border-chrome": {"dark": "#23262d", "light": "#e0dcd5"},
    "border-strong": {"dark": "#2e323a", "light": "#e0dcd5"},
    "line-inert": {"dark": "#3e454e", "light": "#c7cbd2"},
    "dot-inert": {"dark": "#2e323a", "light": "#c7cbd2"},

    # Text
    "text": {"dark": "#f2f4f7", "light": "#14161a"},
    "text-bright": {"dark": "#e8ebf0", "light": "#14161a"},
    "text-2": {"dark": "#c9d0da", "light": "#374151"},
    "text-3": {"dark": "#99a2ae", "light": "#4b5563"},
    "text-4": {"dark": "#828b98", "light": "#4b5563"},
    "text-5": {"dark": "#7a8593", "light": "#656c79"},
    "text-label": {"dark": "#757f8f", "light": "#6b7280"},
    "text-disabled": {"dark": "#4e5661", "light": "#b9bec6"},

    # Listening: warm, your voice going in
    "listening-from": {"dark": "#e55b2b", "light": "#b23f14"},
    "listening-to": {"dark": "#ffb84d", "light": "#d97706"},
    "listening-label": {"dark": "#ff9f1c", "light": "#b23f14"},

    # Writing and speaking: cool, words coming back
    "writing-from": {"dark": "#06b6d4", "light": "#0e7490"},
    "writing-to": {"dark": "#3b82f6", "light": "#1d4ed8"},
    "writing-label": {"dark": "#22d3ee", "light": "#0e7490"},

    # A note where nothing is at risk: amber
    "note": {"dark": "#ff9f1c", "light": "#b23f14"},
    "note-band-bg": {"dark": "rgba(255, 159, 28, 0.04)", "light": "rgba(178, 63, 20, 0.03)"},
    "note-band-border": {"dark": "rgba(255, 159, 28, 0.28)", "light": "rgba(178, 63, 20, 0.28)"},

    # Needs you: crimson outline, never filled
    "needs-you": {"dark": "#ff6b6b", "light": "#be123c"},
    "needs-you-text": {"dark": "#ff9c9c", "light": "#be123c"},
    "needs-you-chip-border": {"dark": "rgba(255, 107, 107, 0.35)", "light": "rgba(190, 18, 60, 0.3)"},
    "needs-you-band-bg": {"dark": "rgba(255, 107, 107, 0.05)", "light": "rgba(190, 18, 60, 0.04)"},
    "needs-you-band-border": {"dark": "rgba(255, 107, 107, 0.32)", "light": "rgba(190, 18, 60, 0.3)"},
    "needs-you-card-bg": {"dark": "rgba(255, 107, 107, 0.04)", "light": "rgba(190, 18, 60, 0.03)"},
    "needs-you-card-border": {"dark": "rgba(255, 107, 107, 0.3)", "light": "rgba(190, 18, 60, 0.28)"},
    "needs-you-divider": {"dark": "rgba(255, 107, 107, 0.16)", "light": "rgba(190, 18, 60, 0.14)"},
    "needs-you-btn-border": {"dark": "rgba(255, 107, 107, 0.4)", "light": "rgba(190, 18, 60, 0.38)"},
    "needs-you-btn-bg": {"dark": "rgba(255, 107, 107, 0.1)", "light": "rgba(190, 18, 60, 0.06)"},
    "needs-you-choice-border": {"dark": "rgba(255, 107, 107, 0.4)", "light": "rgba(190, 18, 60, 0.35)"},
    "needs-you-choice-bg": {"dark": "rgba(255, 107, 107, 0.07)", "light": "rgba(190, 18, 60, 0.04)"},

    # Destructive
    "destructive": {"dark": "#ff3b3b", "light": "#dc2626"},
    "destructive-text": {"dark": "#ff9c9c", "light": "#b91c1c"},
    "destructive-border": {"dark": "rgba(255, 59, 59, 0.38)", "light": "rgba(220, 38, 38, 0.4)"},
    "destructive-bg": {"dark": "rgba(255, 59, 59, 0.06)", "light": "rgba(220, 38, 38, 0.04)"},
    "destructive-card-bg": {"dark": "rgba(255, 59, 59, 0.04)", "light": "rgba(220, 38, 38, 0.04)"},
    "destructive-card-border": {"dark": "rgba(255, 59, 59, 0.3)", "light": "rgba(220, 38, 38, 0.32)"},
    "destructive-ring": {"dark": "#6b3a3a", "light": "#d9b8b8"},
    "btn-destructive-disabled-bg": {"dark": "rgba(255, 59, 59, 0.1)", "light": "rgba(220, 38, 38, 0.07)"},
    "destructive-divider": {"dark": "rgba(255, 59, 59, 0.2)", "light": "rgba(220, 38, 38, 0.16)"},
    "destructive-row-bg": {"dark": "rgba(255, 59, 59, 0.05)", "light": "rgba(220, 38, 38, 0.03)"},
    "destructive-btn-border": {"dark": "rgba(255, 59, 59, 0.25)", "light": "rgba(220, 38, 38, 0.2)"},
    "destructive-btn-bg": {"dark": "rgba(255, 59, 59, 0.1)", "light": "rgba(220, 38, 38, 0.07)"},
    "destructive-input-border": {"dark": "rgba(255, 59, 59, 0.35)", "light": "rgba(220, 38, 38, 0.35)"},
    "destructive-input-bg": {"dark": "#0a0b0d", "light": "#ffffff"},
    "destructive-armed-border": {"dark": "rgba(255, 59, 59, 0.5)", "light": "rgba(220, 38, 38, 0.5)"},
    "btn-destructive-bg": {"dark": "#e02d2d", "light": "#dc2626"},
    "btn-destructive-text": {"dark": "#ffffff", "light": "#ffffff"},
    "btn-destructive-disabled-text": {"dark": "#8a5a5a", "light": "#c08a8a"},

    # Buttons
    "btn-primary-bg": {"dark": "#f2f4f7", "light": "#14161a"},
    "btn-primary-text": {"dark": "#0a0b0d", "light": "#faf8f5"},
    "btn-primary-quiet-text": {"dark": "#3e454e", "light": "#b9bec6"},
    "btn-primary-disabled-bg": {"dark": "#2a2e36", "light": "#e0dcd5"},
    "btn-primary-disabled-text": {"dark": "#6d7783", "light": "#9ca3af"},
    "btn-secondary-bg": {"dark": "#101216", "light": "#ffffff"},
    "btn-secondary-bg-alt": {"dark": "#16181d", "light": "#ffffff"},
    "btn-secondary-outline-bg": {"dark": "rgba(0, 0, 0, 0)", "light": "#ffffff"},
    "btn-secondary-border": {"dark": "#23262d", "light": "#d6d2cb"},
    "btn-secondary-text": {"dark": "#c9d0da", "light": "#14161a"},
    "btn-quiet-bg": {"dark": "rgba(0, 0, 0, 0)", "light": "rgba(0, 0, 0, 0)"},
    "btn-quiet-border": {"dark": "#23262d", "light": "#e0dcd5"},
    "btn-quiet-text": {"dark": "#99a2ae", "light": "#4b5563"},
    "btn-unpainted": {"dark": "#000000", "light": "#000000"},
    "btn-primary-choice-border": {"dark": "#2e323a", "light": "#000000"},
    "btn-primary-soft-bg": {"dark": "#101216", "light": "#14161a"},
    "btn-primary-soft-border": {"dark": "#2e323a", "light": "#faf8f5"},
    "btn-primary-soft-text": {"dark": "#e8ebf0", "light": "#faf8f5"},

    # Floating surfaces and focus
    "scrim": {"dark": "rgba(0, 0, 0, 0.5)", "light": "rgba(0, 0, 0, 0.33)"},
    "notify-bg": {"dark": "rgba(18, 20, 25, 0.94)", "light": "rgba(255, 255, 255, 0.96)"},
    "notify-border": {"dark": "rgba(255, 255, 255, 0.11)", "light": "rgba(20, 22, 26, 0.11)"},
    "notify-action-bg": {"dark": "rgba(255, 255, 255, 0.06)", "light": "rgba(20, 22, 26, 0.05)"},
    "notify-action-border": {"dark": "rgba(255, 255, 255, 0.14)", "light": "rgba(20, 22, 26, 0.14)"},
    "notify-action-2-border": {"dark": "rgba(255, 255, 255, 0.1)", "light": "rgba(20, 22, 26, 0.1)"},
    "focus-ring": {"dark": "#3b82f6", "light": "#1d4ed8"},
}

TYPE: dict[str, TextStyle] = {
    # Display
    "numeral-42": TextStyle(42.0, 600, 1, -0.035, "sans", False),
    "numeral-38": TextStyle(38.0, 600, 1, -0.035, "sans", False),
    "headline-32": TextStyle(32.0, 600, 1.05, -0.03, "sans", False),
    "headline-30": TextStyle(30.0, 600, 1.05, -0.03, "sans", False),
    "headline-28": TextStyle(28.0, 600, 1.1, -0.025, "sans", False),
    "title-26": TextStyle(26.0, 600, 1.1, -0.025, "sans", False),
    "headline-22": TextStyle(22.0, 600, 1.1, -0.025, "sans", False),
    "numeral-20": TextStyle(20.0, 600, 1, -0.03, "sans", False),
    "title-18": TextStyle(18.0, 600, 1.2, -0.02, "sans", False),
    # Text
    "headline-17": TextStyle(17.0, 600, 1.3, 0.0, "sans", False),
    "title-16": TextStyle(16.0, 600, 1.3, 0.0, "sans", False),
    "title-14": TextStyle(14.0, 600, 1.4, 0.0, "sans", False),
    "band-title": TextStyle(13.5, 600, 1.4, 0.0, "sans", False),
    "body-lead": TextStyle(13.5, 400, 1.6, 0.0, "sans", False),
    "row": TextStyle(13.0, 500, 1.45, 0.0, "sans", False),
    "button": TextStyle(12.5, 500, 1.45, 0.0, "sans", False),
    "body": TextStyle(12.5, 400, 1.55, 0.0, "sans", False),
    "helper": TextStyle(12.0, 400, 1.5, 0.0, "sans", False),
    # Labels
    "label": TextStyle(10.0, 600, 1.4, 0.16, "sans", True),
    "label-compact": TextStyle(9.5, 600, 1.4, 0.14, "sans", True),
    # Mono
    "mono-numeral": TextStyle(24.0, 600, 1, 0.0, "mono", False),
    "mono-file": TextStyle(15.0, 400, 1.4, 0.0, "mono", False),
    "mono-meta": TextStyle(11.5, 400, 1.45, 0.0, "mono", False),
    "mono-caption": TextStyle(11.0, 400, 1.45, 0.0, "mono", False),
    "mono-footnote": TextStyle(10.5, 400, 1.45, 0.0, "mono", False),
}

SPACING: dict[str, int] = {
    "space-6": 6,
    "space-8": 8,
    "space-10": 10,
    "space-12": 12,
    "space-14": 14,
    "space-16": 16,
    "space-18": 18,
    "space-20": 20,
    "space-22": 22,
    "space-24": 24,
    "space-26": 26,
    "space-30": 30,
    "space-32": 32,
    "space-34": 34,
    "space-40": 40,
}

RADII: dict[str, int] = {
    "r-5": 5,
    "r-6": 6,
    "r-7": 7,
    "r-8": 8,
    "r-9": 9,
    "r-10": 10,
    "r-11": 11,
    "r-12": 12,
    "r-13": 13,
    "r-14": 14,
    "r-16": 16,
    "r-pill": 99,
}

LAYOUT: dict[str, int] = {
    "w-window": 1040,
    "h-window": 764,
    "h-header": 52,
    "w-compact": 360,
}


def camel(name: str) -> str:
    """`panel-alt` as `panelAlt`, `text-2` as `text2`: the names QML sees."""
    first, *rest = name.split("-")
    return first + "".join(part.capitalize() for part in rest)


_RGBA = re.compile(r"rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\s*\)")


def qml_color(css: str) -> str:
    """A token's CSS colour as a string QML reads: `#rrggbb` or `#aarrggbb`."""
    if css.startswith("#"):
        return css
    match = _RGBA.fullmatch(css)
    if match is None:
        raise ValueError(f"not a colour the theme understands: {css!r}")
    red, green, blue = (int(group) for group in match.group(1, 2, 3))
    alpha = round(float(match.group(4)) * 255)
    return f"#{alpha:02x}{red:02x}{green:02x}{blue:02x}"


def palette(scheme: str) -> dict[str, str]:
    """Every colour token for `scheme`, keyed by its QML name."""
    if scheme not in SCHEMES:
        raise ValueError(f"unknown colour scheme: {scheme!r}")
    return {camel(name): qml_color(values[scheme]) for name, values in COLORS.items()}


def text_spec(name: str) -> dict[str, object]:
    """A type step as QML needs it: whole pixels, spacing in pixels, a family name.

    Qt Quick takes font sizes in whole pixels, so the canvas's half-pixel sizes
    (12.5, 13.5, 10.5, 11.5) round half up.
    """
    style = TYPE[name]
    return {
        "pixelSize": math.floor(style.size + 0.5),
        "weight": style.weight,
        "lineHeight": style.line_height,
        "letterSpacing": round(style.letter_spacing_em * style.size, 3),
        "family": FONT_SANS if style.family == "sans" else FONT_MONO,
        "uppercase": style.uppercase,
    }


def typography() -> dict[str, dict[str, object]]:
    return {camel(name): text_spec(name) for name in TYPE}


def spacing() -> dict[str, int]:
    return {camel(name): value for name, value in SPACING.items()}


def radii() -> dict[str, int]:
    return {camel(name): value for name, value in RADII.items()}
