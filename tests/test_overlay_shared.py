"""Task 18.14: both overlay renderers handle the same protocol messages and
present the same enumerated states, without a display.

`overlay_renderer` (GTK4) and `overlay_renderer_qt` (Qt) both import their
visual-state machine, protocol parser, and dimension/drawing constants from
`overlay_shared` rather than defining their own -- see that module's
docstring for why task 10.4 requires this. So the test that matters most
here is not behavioural (that is already covered, once, by
`test_overlay_renderer.py`'s `RendererViewState`/`MessageParser` tests and by
`test_overlay_renderer_qt.py`'s pure-function tests): it is that both
renderer modules are reading the *same objects*, not equal-but-separate
copies that could silently drift the day one file changes and the other
does not. `assertIs` is what makes that a structural guarantee rather than a
value comparison two coincidentally-identical implementations would also
pass.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from murmly import overlay_shared
from murmly import overlay_renderer
from murmly import overlay_renderer_qt


#: Every dimension/drawing constant a renderer's presentation depends on.
#: Listed once here rather than guessed per-name at each call site, so
#: adding a new one to `overlay_shared` and forgetting to import it into one
#: renderer is caught by iterating this list, not by remembering to add a
#: matching assertion by hand.
_SHARED_NAMES = (
    "WINDOW_WIDTH",
    "WINDOW_HEIGHT",
    "MIN_TEXT_SIZE_PX",
    "MAX_TEXT_SIZE_PX",
    "PANEL_HORIZONTAL_PADDING",
    "BACKGROUND_RGBA",
    "FOREGROUND_RGB",
    "MICROPHONE_ACTIVE_RGB",
    "MICROPHONE_INACTIVE_RGB",
    "ERROR_RGB",
    "CORNER_RADIUS_PX",
    "BAR_MULTIPLIERS",
    "BAR_START_X",
    "BAR_SPACING_X",
    "BAR_WIDTH",
    "BAR_MIN_HEIGHT",
    "BAR_AMPLITUDE_HEIGHT",
    "BAR_CORNER_RADIUS",
    "THINKING_PHASE_STEP",
    "THINKING_PHASE_PER_BAR",
    "THINKING_FRAME_INTERVAL_MS",
    "REDUCED_MOTION_LEVEL_INTERVAL_S",
    "REDUCED_MOTION_QUANTUM_STEPS",
    "MessageParser",
    "MonitorGeometry",
    "RendererViewState",
    "RendererVisualState",
    "select_monitor_index",
    "panel_height",
    "panel_max_width",
    "panel_width",
    "panel_position",
    "truncate_to_width",
)


class SharedIdentityTests(unittest.TestCase):
    def test_the_gtk4_renderer_reads_the_shared_objects_rather_than_copies(self) -> None:
        for name in _SHARED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(overlay_renderer, name), name)
                self.assertIs(
                    getattr(overlay_shared, name),
                    getattr(overlay_renderer, name),
                    f"overlay_renderer.{name} is not overlay_shared.{name}",
                )

    def test_the_qt_renderer_reads_the_shared_objects_rather_than_copies(self) -> None:
        for name in _SHARED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(overlay_renderer_qt, name), name)
                self.assertIs(
                    getattr(overlay_shared, name),
                    getattr(overlay_renderer_qt, name),
                    f"overlay_renderer_qt.{name} is not overlay_shared.{name}",
                )

    def test_both_renderers_read_the_same_bottom_center_placement_function(self) -> None:
        self.assertIs(overlay_shared.bottom_center_position, overlay_renderer.x11_position)
        self.assertIs(overlay_shared.bottom_center_position, overlay_renderer_qt.bottom_center_position)


class SharedProtocolAndStateTests(unittest.TestCase):
    """The behaviour both renderers get for free from sharing one state
    machine, driven once here against `overlay_shared` directly -- since
    `SharedIdentityTests` already proves both renderer modules are this
    exact class, exercising it a second time through each module's own name
    would test object identity twice, not new behaviour."""

    def test_the_same_message_sequence_produces_the_same_states_and_dimensions(self) -> None:
        parser = overlay_shared.MessageParser()
        view = overlay_shared.RendererViewState()

        raw = (
            b'{"type":"state","value":"LISTENING"}\n'
            b'{"type":"level","value":0.42}\n'
            b'{"type":"partial","value":"testing one two"}\n'
            b'{"type":"state","value":"THINKING"}\n'
            b'{"type":"error","duration_ms":250}\n'
        )
        for message in parser.feed(raw):
            view.apply(message)

        self.assertEqual(overlay_shared.RendererVisualState.ERROR, view.state)
        self.assertEqual(0.0, view.level)
        self.assertEqual("", view.partial)
        self.assertEqual(1, view.error_generation)
        # The dimensions every visual state is drawn inside of: a fixed
        # window size neither renderer may compute independently.
        self.assertEqual(156, overlay_shared.WINDOW_WIDTH)
        self.assertEqual(48, overlay_shared.WINDOW_HEIGHT)

    def test_shutdown_is_the_one_message_both_renderers_must_stop_on(self) -> None:
        view = overlay_shared.RendererViewState()
        self.assertFalse(view.apply({"type": "state", "value": "LISTENING"}))
        self.assertTrue(view.apply({"type": "shutdown"}))


class NoRendererHandlesAStateTheOtherIgnoresTests(unittest.TestCase):
    """Task 10.4's own failure mode, caught structurally rather than by
    example: `SharedIdentityTests` proves both renderers read the same
    `RendererVisualState` object, which rules out the two files disagreeing
    on what the members *are*. It does not rule out one file's drawing or
    transition code silently never branching on a member the other file
    does -- that happened for real: the Qt renderer painted
    `RendererVisualState.ERROR` but never scheduled the revert-to-IDLE timer
    `overlay_renderer.py`'s `_hide_error` gives it, so on Windows the error
    glyph never went away. This scans each renderer's own source for every
    `RendererVisualState.<MEMBER>` reference and asserts the two files
    mention the same set -- not proof a member is handled *correctly*, but a
    tripwire that fires the moment one renderer's code stops mentioning a
    state the other still does, which a member added to the enum without a
    matching reference in both files would trip immediately.
    """

    _MEMBER_PATTERN = re.compile(r"RendererVisualState\.([A-Z_]+)")

    @classmethod
    def _referenced_members(cls, module: object) -> set[str]:
        source = Path(module.__file__).read_text(encoding="utf-8")
        return set(cls._MEMBER_PATTERN.findall(source))

    def test_every_enum_member_is_referenced_by_name(self) -> None:
        all_members = {member.name for member in overlay_shared.RendererVisualState}
        gtk4_referenced = self._referenced_members(overlay_renderer)
        qt_referenced = self._referenced_members(overlay_renderer_qt)

        self.assertEqual(all_members, gtk4_referenced, "overlay_renderer.py")
        self.assertEqual(all_members, qt_referenced, "overlay_renderer_qt.py")


if __name__ == "__main__":
    unittest.main()
