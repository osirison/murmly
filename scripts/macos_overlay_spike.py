#!/usr/bin/env python3
"""Task 15.1: does Qt's own window flags give macOS a non-activating,
click-through, all-Spaces-visible panel?

design.md's macOS overlay risk. `overlay_renderer_qt.py`'s window already
requests `Qt.WindowTransparentForInput` and `Qt.WindowDoesNotAcceptFocus` --
the same flags on every platform it runs on -- and reads the real `NSWindow`
back afterwards (`missing_property_for_macos_window`) to confirm whether
those flags actually reached AppKit rather than assuming it. That read-back
proves `level`/`ignoresMouseEvents`/`canBecomeKeyWindow` without needing a
person present (`MacosWindowReadbackRuntimeIntegrationTests` in
`tests/test_overlay_renderer_qt.py` runs it in CI, when a Mac runner has
PySide6 installed). What it cannot prove is the thing task 15.1 is actually
asking: whether a real mouse click passes through the window into whatever
is behind it, and whether the window ever visibly takes focus when clicked --
neither is observable without a screen and a person to click it, on any CI
runner, headless or not.

This script puts up the exact window shape the real renderer uses, prints
the same read-back the renderer prints when refusing to show a bad window,
and tells you what to try.

WHAT TO RUN
============

    uv sync --extra overlay
    uv run --no-sync python3 scripts/macos_overlay_spike.py

A small labelled square appears near the top-left of the screen and stays for
15 seconds (`--seconds` to change that). While it is up:

1. Click through it. Put a window edge or a Finder icon under where the
   square is, then click there. If the click reaches that window/icon and not
   the square, pointer input passes through -- `ignoresMouseEvents` (printed
   below) should already say `True`, but this is what confirms it means what
   it says.
2. Try to focus it. Click directly on the square. If focus visibly moves to
   it (a highlighted title bar, the previously-focused app losing its own
   highlight, Cmd+backtick cycling to it) then it *is* taking focus
   regardless of what `canBecomeKeyWindow` printed.
3. Switch Spaces (Control+Left/Right Arrow, or swipe) while it is up. If the
   square disappears on the new Space, task 15.2's `NSPanel` route is what
   adds `collectionBehavior`'s all-Spaces bit -- Qt's own flags have no
   equivalent, so this is expected to fail even when 1 and 2 both pass, and
   is not one of the spec's three required properties (see this script's own
   read-back section) -- it only affects whether the overlay follows you
   across a Space switch.

If 1 and 2 both hold, task 15.1's spike answers "yes, Qt's flags are enough"
and 15.2's `NSPanel` fallback is not needed. If either fails, it is needed --
`missing_property_for_macos_window`'s read-back below should already have
said so; if it says nothing is missing but a real click still reaches or
focuses the window, that is a gap in the read-back itself, worth filing
before writing the fallback.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=15.0, help="How long to keep the window up.")
    args = parser.parse_args(argv)

    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication, QLabel
    except ModuleNotFoundError as error:
        print(f"PySide6 is not installed: {error}", file=sys.stderr)
        print("Run `uv sync --extra overlay` first.", file=sys.stderr)
        return 1

    # Imported after the PySide6 check above: this module raises at import
    # time on a machine with no `libobjc` (any non-macOS one), and reports
    # nothing worth printing until PySide6 is confirmed present anyway.
    from murmly.overlay_renderer_qt import (
        _real_macos_window_properties,
        missing_property_for_macos_window,
    )

    application = QApplication.instance() or QApplication([sys.argv[0]])
    window = QLabel("murmly overlay spike\n(click through me)")
    window.setStyleSheet("background-color: red; color: white; padding: 12px; font-weight: bold;")
    window.setWindowFlags(
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.Tool
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
        | Qt.WindowType.WindowTransparentForInput
    )
    window.move(40, 40)
    window.show()

    native_id = int(window.winId())
    if native_id == 0:
        print("Qt did not realize a native NSView -- nothing to read back.", file=sys.stderr)
        return 1

    try:
        properties = _real_macos_window_properties(native_id)
    except Exception as error:  # noqa: BLE001 - this is a diagnostic script
        print(f"Reading the native NSWindow back failed: {error!r}", file=sys.stderr)
        return 1

    missing = missing_property_for_macos_window(properties)
    print(f"level={properties.level}")
    print(f"ignoresMouseEvents={properties.ignores_mouse_events}")
    print(f"canBecomeKeyWindow={properties.can_become_key_window}")
    print(f"missing (per the read-back alone): {missing!r}")
    print()
    print(f"Window is up for {args.seconds:.0f} seconds -- try the three things in this script's own docstring.")

    QTimer.singleShot(int(args.seconds * 1000), application.quit)
    application.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
