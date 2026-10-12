"""What the tray icon and its panel show and decide: plain Python, no Qt.

Kept free of Qt so every CI job tests it. `tray_qt.py` is the thin Qt layer that
puts this on a `QSystemTrayIcon` and a small window.

The four quick switches write through Settings: each one names the Settings
control it stands for, and `change_for` turns the person's click into the value
that control's own mapping (`settings.to_value`) expects. Nothing here writes a
file, so a switch cannot save differently from the page it mirrors.

Nothing here knows what a window is, so nothing here can take focus.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from murmly.app import home, settings

# How long after the panel hid itself (it lost focus because the person clicked
# elsewhere) a click on the icon is taken as the click that did it, and so does
# not show the panel again. Clicking the icon of an open panel hides it first.
REOPEN_GUARD_S = 0.4

# The gap between the icon and the panel, and the panel's width.
PANEL_GAP = 8
PANEL_WIDTH = 360


@dataclass(frozen=True, slots=True)
class Switch:
    """One quick switch: the Settings control it writes and the words beside it."""

    id: str
    control: str
    name: str


# Live transcription, auto-transcribe, assistant announcements, quiet hours.
SWITCHES: tuple[Switch, ...] = (
    Switch("live", "live", "Words as you speak"),
    Switch("finish", "finish", "Stop when I pause"),
    Switch("announce", "speak", "Speak when my assistant finishes"),
    Switch("quiet", "quiet", "Quiet hours"),
)
SWITCH_BY_ID: dict[str, Switch] = {switch.id: switch for switch in SWITCHES}

# What switching auto-transcribe on writes: the choice that ends a recording
# at a pause. Switching it off writes "off", which is pressing the keys again.
FINISH_ON = "stop"
FINISH_OFF = "off"


class UnknownSwitch(ValueError):
    """A switch id the panel does not have."""


def is_on(switch_id: str, values: Mapping[str, object]) -> bool:
    """Whether a switch shows as on, from the Settings view's `values` (by control id).

    Auto-transcribe is on for both of the choices that end on a pause ("stop"
    and "continuous"); only "off" is off.
    """
    switch = SWITCH_BY_ID.get(switch_id)
    if switch is None:
        raise UnknownSwitch(f"{switch_id} is not a switch on the tray panel.")
    value = values.get(switch.control)
    if switch_id == "finish":
        return isinstance(value, str) and value != FINISH_OFF
    return value is True


def change_for(switch_id: str, values: Mapping[str, object]) -> tuple[str, object]:
    """The Settings control and the value to report for a click on `switch_id`.

    The click wants the opposite of what the switch shows now. The result goes
    to `SettingsController.change`, so it is written, validated, refused and
    restarted exactly as the same change on the Settings page would be.
    """
    switch = SWITCH_BY_ID.get(switch_id)
    if switch is None:
        raise UnknownSwitch(f"{switch_id} is not a switch on the tray panel.")
    wanted = not is_on(switch_id, values)
    if switch_id == "finish":
        return switch.control, FINISH_ON if wanted else FINISH_OFF
    return switch.control, wanted


def _note(switch_id: str, values: Mapping[str, object], view: Mapping[str, object]) -> str:
    on = is_on(switch_id, values)
    if switch_id == "live":
        return "shown in the indicator" if on else "off"
    if switch_id == "finish":
        if values.get("finish") == "continuous":
            return "types each sentence"
        if on:
            label = view.get("pauseLabel")
            return f"after {label} of quiet" if label else "when you pause"
        return "press the keys again"
    if switch_id == "announce":
        return "reads replies aloud" if on else "silent"
    window = values.get("quiet-from"), values.get("quiet-to")
    if on and all(isinstance(end, str) for end in window):
        return f"{window[0]} to {window[1]}"
    return "off"


def switches_view(settings_view: Mapping[str, object]) -> dict[str, object]:
    """The four switches for QML, from the Settings controller's `view`.

    `loaded` is False until the file has been read; `disabled` while it cannot be
    written (it does not parse, or cannot be read). `message` is a refusal or a
    failure in the writer's own words, and `status` the line about the restart.
    """
    loaded = settings_view.get("loaded") is True
    values = settings_view.get("values")
    values = values if isinstance(values, Mapping) else {}
    rows = []
    for switch in SWITCHES:
        rows.append(
            {
                "id": switch.id,
                "name": switch.name,
                "note": _note(switch.id, values, settings_view) if loaded else "",
                "on": is_on(switch.id, values) if loaded else False,
            }
        )
    return {
        "loaded": loaded,
        "disabled": settings_view.get("disabled") is True or not loaded,
        "message": str(settings_view.get("message") or ""),
        "status": str(settings_view.get("status") or ""),
        "switches": rows,
    }


# ---------------------------------------------------------------------------
# The icon
# ---------------------------------------------------------------------------

# What the icon can say. It is one of the five presentations Home has, or
# NEUTRAL: nothing is asking the daemon while no window or panel is visible, so
# a state remembered from the last time would be a guess shown as fact.
NEUTRAL = "neutral"
ICON_STATES = (*home.PRESENTATIONS, NEUTRAL)

# The mark, the same bubble with three voice bars the window's header draws.
MARK_PATH = (
    "M8,2H24A6,6 0 0 1 30,8V17A6,6 0 0 1 24,23H14.6C14.6,26.1 13.1,28.4 9.6,29.8"
    "C10.9,27.9 11.2,25.6 11.2,23H8A6,6 0 0 1 2,17V8A6,6 0 0 1 8,2Z"
    "M9.3,7.2H9.3A2,2 0 0 1 11.3,9.2V15.8A2,2 0 0 1 9.3,17.8H9.3A2,2 0 0 1 7.3,15.8V9.2A2,2 0 0 1 9.3,7.2Z"
    "M16.9,5.2H16.9A2,2 0 0 1 18.9,7.2V17.8A2,2 0 0 1 16.9,19.8H16.9A2,2 0 0 1 14.9,17.8V7.2A2,2 0 0 1 16.9,5.2Z"
    "M23.6,4.2H23.6A1.1,1.1 0 0 1 24.7,5.3V19.7A1.1,1.1 0 0 1 23.6,20.8H23.6A1.1,1.1 0 0 1 22.5,19.7V5.3A1.1,1.1 0 0 1 23.6,4.2Z"
)

# The icon's colours come from the theme's own tokens, so a colour means here
# what it means in the window: warm is your voice going in, cool is words coming
# back, a crimson ring is something that needs you.
_MARK_TOKEN = "textBright"
_DIM_TOKEN = "lineInert"
_DOT = {
    home.LISTENING: ("listeningLabel", False),
    home.WRITING: ("writingLabel", False),
    home.NEEDS_YOU: ("needsYou", True),
    home.NOT_RUNNING: ("needsYou", True),
}


def icon_presentation(view: Mapping[str, object], polling: bool) -> str:
    """Which icon the app shows for Home's `view`.

    Neutral when nothing is polling, or Home has not heard from the daemon yet.
    """
    state = view.get("state")
    if not polling or not isinstance(state, str) or state not in home.PRESENTATIONS:
        return NEUTRAL
    return state


def tooltip(presentation: str, view: Mapping[str, object]) -> str:
    """The text beside the icon: the state in Home's own words, or just the name."""
    chip = view.get("chip")
    if presentation == NEUTRAL or not isinstance(chip, str) or not chip:
        return "murmly"
    return f"murmly: {chip}"


def icon_svg(presentation: str, colors: Mapping[str, str]) -> str:
    """The icon as an SVG document, in the colours of `colors` (a theme palette, by its QML names).

    A state shows as a dot at the lower right: filled for listening (warm) and
    writing (cool), a ring for needs-you and not-running (crimson, never filled).
    Not running also dims the mark. Neutral and ready are the mark alone.
    """
    dim = presentation == home.NOT_RUNNING
    mark = colors[_DIM_TOKEN if dim else _MARK_TOKEN]
    parts = [
        '<svg width="32" height="32" viewBox="0 0 32 32">',
        f'<path fill="{mark}" fill-rule="evenodd" d="{MARK_PATH}"/>',
    ]
    dot = _DOT.get(presentation)
    if dot is not None:
        token, ring = dot
        color = colors[token]
        if ring:
            parts.append(f'<circle cx="25" cy="25" r="5" fill="none" stroke="{color}" stroke-width="3"/>')
        else:
            parts.append(f'<circle cx="25" cy="25" r="5.5" fill="{color}"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# The panel's place and when it shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, max(low, high)))


def place_panel(
    anchor: Rect | None,
    cursor: tuple[int, int],
    size: tuple[int, int],
    available: Rect,
    gap: int = PANEL_GAP,
) -> tuple[int, int]:
    """Where the panel's top-left goes, next to the icon and on the screen.

    `anchor` is the icon's rectangle, which some desktops cannot give (Linux
    often reports an empty one); the cursor, where the person just clicked, then
    stands in for it. The panel sits on the side of the icon that faces the
    middle of the screen, which is above a bottom taskbar and below a top one,
    and is kept inside `available`, the screen without its taskbars.
    """
    width, height = size
    if anchor is None or anchor.empty:
        anchor = Rect(cursor[0], cursor[1], 0, 0)
    centre_x = anchor.x + anchor.width // 2
    centre_y = anchor.y + anchor.height // 2
    screen_x = available.x + available.width // 2
    screen_y = available.y + available.height // 2
    # An icon in a side taskbar puts the panel beside it, not above or below.
    beside = abs(centre_x - screen_x) * available.height > abs(centre_y - screen_y) * available.width
    if beside:
        x = anchor.right + gap if centre_x < screen_x else anchor.x - width - gap
        y = centre_y - height // 2
    else:
        x = centre_x - width // 2
        y = anchor.bottom + gap if centre_y < screen_y else anchor.y - height - gap
    x = _clamp(x, available.x, available.right - width)
    y = _clamp(y, available.y, available.bottom - height)
    return x, y


class PanelToggle:
    """Decides whether a click on the icon shows the panel or hides it.

    A panel that hides when it loses focus has already hidden by the time the
    click that took its focus reaches the icon, so that click would show it
    again. `hid_itself` is told when it hides that way; a click within
    `REOPEN_GUARD_S` of it only leaves it hidden.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, guard: float = REOPEN_GUARD_S) -> None:
        self._clock = clock
        self._guard = guard
        self._hid_at: float | None = None

    def hid_itself(self) -> None:
        self._hid_at = self._clock()

    def should_show(self, visible: bool) -> bool:
        """True when a click on the icon should show the panel now."""
        if visible:
            return False
        if self._hid_at is not None and self._clock() - self._hid_at < self._guard:
            self._hid_at = None
            return False
        return True


# ---------------------------------------------------------------------------
# Closing the window
# ---------------------------------------------------------------------------


def quits_when_window_closes(tray_found: bool) -> bool:
    """With no tray, closing the window ends the app, so nothing is left running unseen.

    With a tray the app stays in it, where the icon opens the window again.
    Either way the daemon is separate and keeps running.
    """
    return not tray_found
