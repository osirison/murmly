from __future__ import annotations

import json
import subprocess
import unittest

from murmly.desktop import (
    ALLOWED_SIGNATURES,
    DesktopQueryError,
    PlasmaShortcuts,
    ShortcutOwner,
    detect_desktop_session,
)
from murmly.overlay import OverlayBackend


def recorded(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """A run_command stub returning one recorded busctl result."""
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


# Captured verbatim from `busctl --user --json=short` on Plasma 6.7.4.
AVAILABLE_TRUE = '{"type":"b","data":[true]}'
AVAILABLE_FALSE = '{"type":"b","data":[false]}'
OWNERS_PANIC = (
    '{"type":"a(ssssssaiai)","data":[[["_launch","panic","net.local.panic.sh.desktop",'
    '"panic","default","Default Context",[218103825],[0]]]]}'
)
OWNERS_NONE = '{"type":"a(ssssssaiai)","data":[[]]}'
OWNERS_TWO = (
    '{"type":"a(ssssssaiai)","data":[[["_launch","murmly","net.local.murmly.desktop",'
    '"murmly","default","Default Context",[268435545],[268435545]],'
    '["_launch","intruder","net.local.intruder.desktop","intruder","default",'
    '"Default Context",[268435545],[268435545]]]]}'
)
KEYS_SINGLE = '{"type":"a(ai)","data":[[[[218103825,0,0,0]]]]}'
KEYS_TWO_ALTERNATES = '{"type":"a(ai)","data":[[[[117440512,0,0,0]],[[285212672,0,0,0]]]]}'
KEYS_EMPTY = '{"type":"a(ai)","data":[[]]}'


class CrashClassGuardTests(unittest.TestCase):
    """kglobalacceld aborts on an inbound key-sequence struct that is not exactly
    four integers long. No Murmly code path may build such a call."""

    def test_allowed_signatures_contain_no_struct(self) -> None:
        for signature in ALLOWED_SIGNATURES:
            self.assertNotIn("(", signature)
            self.assertNotIn("ai", signature)

    def test_struct_signature_is_refused_without_running_anything(self) -> None:
        run = recorded(AVAILABLE_TRUE)
        shortcuts = PlasmaShortcuts(run_command=run)

        with self.assertRaises(DesktopQueryError) as raised:
            shortcuts._call("globalShortcutAvailable", "(ai)s", "1", "268435544", "")

        self.assertIn("scalar", str(raised.exception))
        self.assertEqual([], run.calls, "no command may run for a refused signature")


class AvailabilityTests(unittest.TestCase):
    def test_free_key_reports_available(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(AVAILABLE_TRUE))

        self.assertTrue(shortcuts.is_available(268435544))

    def test_bound_key_reports_unavailable(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(AVAILABLE_FALSE))

        self.assertFalse(shortcuts.is_available(218103825))

    def test_uses_scalar_signature(self) -> None:
        run = recorded(AVAILABLE_TRUE)

        PlasmaShortcuts(run_command=run).is_available(268435544)

        self.assertIn("is", run.calls[0])
        self.assertIn("isGlobalShortcutAvailable", run.calls[0])

    def test_failed_query_raises_rather_than_reporting_free(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(returncode=1, stderr="Connection refused"))

        with self.assertRaises(DesktopQueryError):
            shortcuts.is_available(268435544)


class OwnerTests(unittest.TestCase):
    def test_parses_single_owner(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(OWNERS_PANIC))

        owners = shortcuts.owners_of(218103825)

        self.assertEqual(
            [ShortcutOwner("_launch", "panic", "net.local.panic.sh.desktop", "panic")],
            owners,
        )

    def test_unowned_key_returns_empty(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(OWNERS_NONE))

        self.assertEqual([], shortcuts.owners_of(268435544))

    def test_double_bind_returns_both_owners(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(OWNERS_TWO))

        owners = shortcuts.owners_of(268435545)

        self.assertEqual(2, len(owners))
        self.assertEqual(
            {"net.local.murmly.desktop", "net.local.intruder.desktop"},
            {owner.component_unique for owner in owners},
        )

    def test_label_prefers_friendly_names(self) -> None:
        owner = ShortcutOwner("show-on-mouse-pos", "Show Clipboard Items", "plasmashell", "plasmashell")

        self.assertEqual("plasmashell (Show Clipboard Items)", owner.label)

    def test_label_collapses_duplicate_friendly_names(self) -> None:
        self.assertEqual("panic", ShortcutOwner("_launch", "panic", "net.local.panic.sh.desktop", "panic").label)


class ComponentTests(unittest.TestCase):
    def test_present_component(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded('{"type":"o","data":["/component/x"]}'))

        self.assertTrue(shortcuts.component_exists("x.desktop"))

    def test_absent_component_is_false_not_an_error(self) -> None:
        run = recorded(returncode=1, stderr="Call failed: The component 'x.desktop' doesn't exist.")
        shortcuts = PlasmaShortcuts(run_command=run)

        self.assertFalse(shortcuts.component_exists("x.desktop"))

    def test_other_failure_raises_so_a_poll_cannot_mistake_it_for_absent(self) -> None:
        run = recorded(returncode=1, stderr="Failed to connect to bus")
        shortcuts = PlasmaShortcuts(run_command=run)

        with self.assertRaises(DesktopQueryError):
            shortcuts.component_exists("x.desktop")

    def test_unreachable_daemon_raises(self) -> None:
        def explode(*_args, **_kwargs):
            raise OSError("busctl missing")

        with self.assertRaises(DesktopQueryError) as raised:
            PlasmaShortcuts(run_command=explode).component_exists("x.desktop")

        self.assertIn("busctl missing", str(raised.exception))


class RegisteredKeyTests(unittest.TestCase):
    def test_single_sequence_drops_zero_padding(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(KEYS_SINGLE))

        self.assertEqual([218103825], shortcuts.registered_keys("net.local.panic.sh.desktop"))

    def test_two_alternates(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(KEYS_TWO_ALTERNATES))

        self.assertEqual([117440512, 285212672], shortcuts.registered_keys("x.desktop"))

    def test_unregistered_component_has_no_keys(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded(KEYS_EMPTY))

        self.assertEqual([], shortcuts.registered_keys("x.desktop"))

    def test_malformed_sequence_raises(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded('{"type":"a(ai)","data":[[[]]]}'))

        with self.assertRaises(DesktopQueryError):
            shortcuts.registered_keys("x.desktop")

    def test_unreadable_output_raises(self) -> None:
        shortcuts = PlasmaShortcuts(run_command=recorded("not json"))

        with self.assertRaises(DesktopQueryError):
            shortcuts.registered_keys("x.desktop")


class SessionDetectionTests(unittest.TestCase):
    def test_plasma_x11_is_supported_and_verified(self) -> None:
        session = detect_desktop_session(
            {"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        )

        self.assertTrue(session.supported)
        self.assertTrue(session.verified)
        self.assertEqual(OverlayBackend.X11, session.backend)

    def test_plasma_wayland_is_supported_but_unverified(self) -> None:
        session = detect_desktop_session(
            {
                "XDG_CURRENT_DESKTOP": "KDE",
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
            }
        )

        self.assertTrue(session.supported)
        self.assertFalse(session.verified)
        self.assertIn("unverified", session.detail)

    def test_non_plasma_is_unsupported(self) -> None:
        session = detect_desktop_session(
            {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
        )

        self.assertFalse(session.is_plasma)
        self.assertFalse(session.supported)
        self.assertIn("KDE Plasma", session.detail)

    def test_plasma_without_a_display_is_unsupported(self) -> None:
        session = detect_desktop_session({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11"})

        self.assertTrue(session.is_plasma)
        self.assertFalse(session.supported)
        self.assertIn("no graphical display", session.detail)


class LiveSessionTests(unittest.TestCase):
    """Exercised only inside a real Plasma session; skipped everywhere else."""

    def setUp(self) -> None:
        session = detect_desktop_session()
        if not session.supported:
            self.skipTest("no supported desktop session available")
        self.shortcuts = PlasmaShortcuts()
        try:
            self.shortcuts.component_exists("murmly-probe-does-not-exist.desktop")
        except DesktopQueryError:
            self.skipTest("Plasma shortcut daemon is not reachable")

    def test_absent_component_reports_false(self) -> None:
        try:
            present = self.shortcuts.component_exists("murmly-probe-does-not-exist.desktop")
        except DesktopQueryError as error:
            self.skipTest(f"shortcut daemon became unavailable: {error}")
        self.assertFalse(present)

    def test_availability_and_ownership_agree(self) -> None:
        keycode = 268435544  # Meta+X
        try:
            available = self.shortcuts.is_available(keycode)
            owners = self.shortcuts.owners_of(keycode)
        except DesktopQueryError as error:
            # The daemon can restart under us; an unavailable desktop is a skip,
            # never a failure.
            self.skipTest(f"shortcut daemon became unavailable: {error}")

        self.assertEqual(available, not owners)

    def test_json_shape_matches_the_recorded_fixtures(self) -> None:
        result = subprocess.run(
            [
                "busctl",
                "--user",
                "--json=short",
                "call",
                "org.kde.kglobalaccel",
                "/kglobalaccel",
                "org.kde.KGlobalAccel",
                "isGlobalShortcutAvailable",
                "is",
                "268435544",
                "",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            self.skipTest("shortcut daemon query failed")

        self.assertEqual(set(json.loads(AVAILABLE_TRUE)), set(json.loads(result.stdout)))


if __name__ == "__main__":
    unittest.main()
