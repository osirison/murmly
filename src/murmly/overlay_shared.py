"""What every overlay renderer must present identically, in one place.

Task 10.4: "Reproduce every visual state, dimension and lifecycle transition
the GTK4 renderer presents, from a single enumeration both read." Two
renderer processes -- `overlay_renderer.py` (GTK4, for Plasma's X11 and
Wayland sessions) and `overlay_renderer_qt.py` (Qt, for Windows) -- draw the
same recording indicator through two different toolkits. Nothing here is
toolkit-specific: no `cairo`, no `gi`, no `PySide6`. That is what lets both
renderer scripts import it regardless of which toolkit is or is not installed
in the interpreter running them, and what lets the test suite exercise it on
a machine with neither toolkit and no display at all (task 18.14).

What lives here, and why each piece has to:

- `RendererVisualState` and `RendererViewState`: the state machine that turns
  the newline-delimited JSON protocol (`validate_message`, `MessageParser`)
  into what should be on screen. This is "the lifecycle transitions" in
  10.4's words -- level and partial text clearing on every exit from
  LISTENING, ERROR overriding whatever came before it and reverting on its
  own timer, SHUTDOWN ending the process. A renderer that reimplemented this
  instead of importing it could drift the day one of those rules changed and
  the other renderer's copy did not.
- The dimension and placement math (`WINDOW_WIDTH`, `panel_width`,
  `select_monitor_index`, `bottom_center_position`, ...): "the dimensions" in
  10.4's words, and the multi-display determinism the `recording-overlay`
  spec's "Multiple displays are connected" scenario requires -- both
  renderers select a monitor and size a transcript panel by the same rules.
- The drawing constants (colours, the microphone glyph's geometry, the level
  bars' multipliers, the thinking animation's phase step, the error glyph):
  "every visual state" is not satisfied by matching enum members if the two
  renderers then paint different pixels for the same state. These are the
  numbers a person would otherwise have to keep two files' worth of
  `cairo`/`QPainter` calls in sync by eye.

Neither renderer script can `import murmly.overlay_shared` as an ordinary
package submodule when it is launched the way `OverlayController` launches
it: as a bare script (`python3 /path/to/overlay_renderer.py ...`), whose only
directory on `sys.path` is its own (`src/murmly/`), not `src/`. Both scripts
carry the same bootstrap for that case -- see the top of either file -- so
`from murmly.overlay_shared import ...` also works there. Tests that import
`murmly.overlay_renderer`/`murmly.overlay_renderer_qt` as ordinary package
modules (where `src/` is already on `sys.path`) never take that branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json


MAX_MESSAGE_BYTES = 1_024
MIN_ERROR_DURATION_MS = 100
MAX_ERROR_DURATION_MS = 10_000
MAX_PARTIAL_CHARS = 200

# --------------------------------------------------------------------------
# Dimensions (task 10.4: "the dimensions")
# --------------------------------------------------------------------------

WINDOW_WIDTH = 156
WINDOW_HEIGHT = 48
MIN_TEXT_SIZE_PX = 8
MAX_TEXT_SIZE_PX = 48
PANEL_MAX_DISPLAY_FRACTION = 0.75
PANEL_HORIZONTAL_PADDING = 12
PANEL_VERTICAL_PADDING = 6
PANEL_GAP_PX = 6


@dataclass(frozen=True, slots=True)
class MonitorGeometry:
    connector: str
    x: int
    y: int
    width: int
    height: int
    scale: int = 1


def select_monitor_index(monitors: list[MonitorGeometry]) -> int | None:
    """Deterministic monitor choice: the one at the origin, else the lowest
    connector name -- what the `recording-overlay` spec's "Multiple displays
    are connected" scenario requires ("selects one display deterministically
    and keeps the overlay on that display").
    """
    for index, monitor in enumerate(monitors):
        if monitor.x <= 0 < monitor.x + monitor.width and monitor.y <= 0 < monitor.y + monitor.height:
            return index
    if not monitors:
        return None
    return min(range(len(monitors)), key=lambda index: monitors[index].connector)


def bottom_center_position(monitor: MonitorGeometry, bottom_margin_px: int) -> tuple[int, int]:
    """Where the indicator's top-left corner goes: bottom-centred with a margin.

    Named for what it computes, not for the display protocol that first used
    it -- `overlay_renderer.py` keeps re-exporting it as `x11_position` for
    its own existing call sites and tests, and the Qt renderer imports it
    under this name.
    """
    return (
        (monitor.x + (monitor.width - WINDOW_WIDTH) // 2) * monitor.scale,
        (monitor.y + monitor.height - WINDOW_HEIGHT - bottom_margin_px) * monitor.scale,
    )


def panel_height(text_size_px: int) -> int:
    return text_size_px + 2 * PANEL_VERTICAL_PADDING


def panel_max_width(monitor: MonitorGeometry) -> int:
    return max(int(monitor.width * PANEL_MAX_DISPLAY_FRACTION), WINDOW_WIDTH)


def panel_width(text_width_px: float, monitor: MonitorGeometry) -> int:
    """Size the transcript panel to its text, bounded by the display fraction."""
    import math

    requested = int(math.ceil(text_width_px)) + 2 * PANEL_HORIZONTAL_PADDING
    return max(min(requested, panel_max_width(monitor)), WINDOW_WIDTH)


def panel_position(
    monitor: MonitorGeometry,
    bottom_margin_px: int,
    width: int,
    height: int,
) -> tuple[int, int]:
    """Place the panel below the recording indicator without moving it.

    The indicator keeps its own margin, so the panel occupies the space
    between the indicator's bottom edge and the display edge, clamped so it
    never leaves the screen.
    """
    indicator_top = monitor.y + monitor.height - WINDOW_HEIGHT - bottom_margin_px
    indicator_bottom = indicator_top + WINDOW_HEIGHT
    below_top = indicator_bottom + PANEL_GAP_PX
    if below_top + height <= monitor.y + monitor.height:
        top = below_top
    else:
        # No room in the margin. Sitting above the indicator keeps the panel
        # visible and adjacent; clamping it downward instead would draw it on
        # top of the indicator, and leaving it below would push it off the
        # display.
        top = max(indicator_top - PANEL_GAP_PX - height, monitor.y)
    return (
        (monitor.x + (monitor.width - width) // 2) * monitor.scale,
        top * monitor.scale,
    )


def truncate_to_width(text: str, measure, available_px: float) -> str:
    """Drop leading characters until the tail fits, matching the encoder's bias.

    Binary searched: measuring one candidate per starting offset is quadratic
    glyph shaping, and this runs on every draw.
    """
    if not text or measure(text) <= available_px:
        return text
    low, high = 1, len(text)
    while low < high:
        middle = (low + high) // 2
        if measure("…" + text[middle:]) <= available_px:
            high = middle
        else:
            low = middle + 1
    candidate = "…" + text[low:]
    return candidate if low < len(text) and measure(candidate) <= available_px else ""


# --------------------------------------------------------------------------
# Visual states and the protocol that drives them (task 10.4: "the states"
# and "the lifecycle transitions")
# --------------------------------------------------------------------------


class RendererVisualState(StrEnum):
    """Every state a renderer can be asked to present.

    A superset of `overlay.OverlayState` (IDLE/LISTENING/THINKING): ERROR is
    not a state the daemon ever announces over the wire -- it announces an
    "error" *message*, timed to revert on its own -- but it is a state a
    renderer must draw, so it belongs in the renderer's own enumeration
    rather than the protocol's.
    """

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    ERROR = "ERROR"


#: The subset of `RendererVisualState` a "state" protocol message may carry.
#: Built from the enum rather than re-typed as a literal set, so a renderer
#: state added above is a value `validate_message` already accepts or
#: deliberately excludes (ERROR, which is never a "state" message's value),
#: not a second place that has to be told about it.
_PROTOCOL_STATE_VALUES = frozenset(RendererVisualState) - {RendererVisualState.ERROR}


def validate_message(message: object) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    if message_type == "state":
        if set(message) != {"type", "value"} or message.get("value") not in _PROTOCOL_STATE_VALUES:
            return None
    elif message_type == "partial":
        value = message.get("value")
        if set(message) != {"type", "value"} or not isinstance(value, str):
            return None
        if len(value) > MAX_PARTIAL_CHARS:
            return None
    elif message_type == "level":
        value = message.get("value")
        if (
            set(message) != {"type", "value"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite(value)
            or not 0.0 <= value <= 1.0
        ):
            return None
    elif message_type == "error":
        duration_ms = message.get("duration_ms")
        if (
            set(message) != {"type", "duration_ms"}
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not MIN_ERROR_DURATION_MS <= duration_ms <= MAX_ERROR_DURATION_MS
        ):
            return None
    elif message_type == "shutdown":
        if set(message) != {"type"}:
            return None
    else:
        return None
    return message


def _is_finite(value: float) -> bool:
    import math

    return math.isfinite(value)


class MessageParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[dict[str, object]]:
        self._buffer.extend(data)
        messages: list[dict[str, object]] = []
        while b"\n" in self._buffer:
            line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            if not line or len(line) + 1 > MAX_MESSAGE_BYTES:
                continue
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            validated = validate_message(decoded)
            if validated is not None:
                messages.append(validated)
        if len(self._buffer) >= MAX_MESSAGE_BYTES:
            self._buffer.clear()
        return messages


@dataclass(slots=True)
class RendererViewState:
    """The one state machine both renderers drive from incoming messages.

    `apply` returns whether the renderer should stop (a "shutdown" message),
    exactly as `overlay_renderer.OverlayApplication._handle_message` and its
    Qt counterpart both use it.
    """

    state: RendererVisualState = RendererVisualState.IDLE
    level: float = 0.0
    partial: str = ""
    error_generation: int = 0

    @property
    def visible(self) -> bool:
        return self.state != RendererVisualState.IDLE

    def apply(self, message: dict[str, object]) -> bool:
        message_type = message["type"]
        if message_type == "state":
            self.state = RendererVisualState(str(message["value"]))
            if self.state != RendererVisualState.LISTENING:
                # Partial text describes audio still being captured. Dropping
                # it here, rather than at each call site, is what keeps it out
                # of the processing and error presentations.
                self.level = 0.0
                self.partial = ""
            return False
        if message_type == "level":
            if self.state == RendererVisualState.LISTENING:
                self.level = float(message["value"])
            return False
        if message_type == "partial":
            if self.state == RendererVisualState.LISTENING:
                self.partial = str(message["value"])
            return False
        if message_type == "error":
            self.state = RendererVisualState.ERROR
            self.level = 0.0
            self.partial = ""
            self.error_generation += 1
            return False
        return message_type == "shutdown"


# --------------------------------------------------------------------------
# Drawing constants (task 10.4: "every visual state ... on every platform").
#
# Colours are (r, g, b) or (r, g, b, a) floats in [0, 1], the range both
# cairo and QPainter/QColor accept. Every other number here is a pixel
# position or size within the WINDOW_WIDTH x WINDOW_HEIGHT indicator surface,
# read by both renderers' drawing code rather than typed twice.
# --------------------------------------------------------------------------

BACKGROUND_RGBA = (0.07, 0.08, 0.09, 0.92)
FOREGROUND_RGB = (0.9, 0.92, 0.94)
MICROPHONE_ACTIVE_RGB = (1.0, 0.27, 0.29)
MICROPHONE_INACTIVE_RGB = (0.88, 0.9, 0.92)
ERROR_RGB = (1.0, 0.3, 0.3)

CORNER_RADIUS_PX = 8.0

# The microphone glyph.
MICROPHONE_LINE_WIDTH = 2.2
MICROPHONE_CENTER_X = 25.0
MICROPHONE_HEAD_CENTER_Y = 20.0
MICROPHONE_HEAD_RADIUS = 5.0
MICROPHONE_BODY_CENTER_Y = 23.0
MICROPHONE_BODY_RADIUS_INNER = 5.0
MICROPHONE_BODY_RADIUS_OUTER = 9.0
MICROPHONE_BODY_LEFT_X = 20.0
MICROPHONE_BODY_RIGHT_X = 30.0
MICROPHONE_STEM_TOP_Y = 32.0
MICROPHONE_STEM_BOTTOM_Y = 36.0

# The level bars.
BAR_MULTIPLIERS = (0.5, 0.72, 0.9, 1.0, 0.82, 0.65, 0.45)
BAR_START_X = 53.0
BAR_SPACING_X = 13.0
BAR_WIDTH = 6.0
BAR_MIN_HEIGHT = 4.0
BAR_AMPLITUDE_HEIGHT = 22.0
BAR_CORNER_RADIUS = 3.0

# The "thinking" animation, and its reduced-motion substitutes.
THINKING_PHASE_STEP = 0.16
THINKING_PHASE_PER_BAR = 0.75
THINKING_FRAME_INTERVAL_MS = 33
STATIC_PROCESSING_DOT_COUNT = 3
STATIC_PROCESSING_DOT_RADIUS = 3.0
STATIC_PROCESSING_DOT_SPACING_X = 18.0
STATIC_PROCESSING_DOT_START_X = 76.0
STATIC_PROCESSING_DOT_Y = 24.0
REDUCED_MOTION_LEVEL_INTERVAL_S = 0.25
REDUCED_MOTION_QUANTUM_STEPS = 3.0

# The error glyph.
ERROR_LINE_WIDTH = 3.0
ERROR_CIRCLE_RADIUS = 12.0
ERROR_CIRCLE_CENTER_Y = 24.0
ERROR_STEM_TOP_Y = 16.0
ERROR_STEM_BOTTOM_Y = 26.0
ERROR_DOT_Y = 31.0
ERROR_DOT_RADIUS = 1.5
