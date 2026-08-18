from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from murmly.integrations import (
    ClipboardPaster,
    MissingToolError,
    PasteInjection,
    choose_clipboard_copy_command,
    select_paste_injection,
)


def fake_which_factory(*available: str):
    available_set = set(available)

    def fake_which(command: str) -> str | None:
        return f"/usr/bin/{command}" if command in available_set else None

    return fake_which


def probe_factory(*usable: str):
    """Stand in for the tools' own no-op invocations, without running them."""
    usable_set = set(usable)

    def fake_run(command, **_kwargs):
        method = command[0]
        if method in usable_set:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"{method} cannot run here")

    return fake_run


class IntegrationSelectionTests(unittest.TestCase):
    WAYLAND = {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"}
    X11 = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}

    def test_wayland_prefers_wl_copy_and_a_usable_wtype(self) -> None:
        which = fake_which_factory("wl-copy", "wtype", "xdotool")
        self.assertEqual(["wl-copy"], choose_clipboard_copy_command(self.WAYLAND, which))

        injection = select_paste_injection(self.WAYLAND, which, probe_factory("wtype"))

        self.assertTrue(injection.available)
        self.assertEqual("wtype", injection.method)
        self.assertEqual(("wtype", "-M", "ctrl", "v", "-m", "ctrl"), injection.command)

    def test_an_installed_injector_the_session_cannot_run_is_skipped(self) -> None:
        which = fake_which_factory("wl-copy", "wtype", "ydotool")

        injection = select_paste_injection(self.WAYLAND, which, probe_factory("ydotool"))

        self.assertEqual("ydotool", injection.method)
        self.assertEqual(("ydotool", "key", "29:1", "47:1", "47:0", "29:0"), injection.command)

    def test_no_usable_injector_reports_every_reason_and_the_remedy(self) -> None:
        which = fake_which_factory("wl-copy", "wtype", "ydotool")

        injection = select_paste_injection(self.WAYLAND, which, probe_factory())

        self.assertFalse(injection.available)
        self.assertIsNone(injection.command)
        self.assertIn("wtype is installed but cannot inject", injection.reason)
        self.assertIn("ydotool is installed but cannot inject", injection.reason)
        self.assertTrue(any("dnf install ydotool" in line for line in injection.remedy))

    def test_nothing_installed_names_what_to_install(self) -> None:
        injection = select_paste_injection(self.WAYLAND, fake_which_factory("wl-copy"), probe_factory())

        self.assertFalse(injection.available)
        self.assertIn("wtype or ydotool", injection.reason)
        self.assertTrue(injection.remedy)

    def test_an_excluded_injector_is_not_reselected(self) -> None:
        which = fake_which_factory("wl-copy", "wtype", "ydotool")

        injection = select_paste_injection(
            self.WAYLAND,
            which,
            probe_factory("wtype", "ydotool"),
            excluded={"wtype"},
        )

        self.assertEqual("ydotool", injection.method)

    def test_x11_prefers_xclip_and_xdotool_without_probing(self) -> None:
        which = fake_which_factory("xclip", "xdotool", "wl-copy")

        def refuse(*_arguments, **_keywords):
            raise AssertionError("The X11 injector is not probed.")

        self.assertEqual(["xclip", "-selection", "clipboard"], choose_clipboard_copy_command(self.X11, which))

        injection = select_paste_injection(self.X11, which, refuse)

        self.assertEqual("xdotool", injection.method)
        self.assertEqual(("xdotool", "key", "--clearmodifiers", "ctrl+v"), injection.command)

    def test_x11_does_not_select_wl_copy_without_xclip(self) -> None:
        env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}
        with self.assertRaises(MissingToolError):
            choose_clipboard_copy_command(env, fake_which_factory("wl-copy", "xdotool"))


class ClipboardRestoreTests(unittest.TestCase):
    ENV = {"XDG_SESSION_TYPE": "x11"}

    def _paster(self, **kwargs) -> ClipboardPaster:
        return ClipboardPaster(env=dict(self.ENV), which=lambda name: f"/usr/bin/{name}", **kwargs)

    def _capture(self):
        calls: list[tuple[tuple, str | None]] = []
        sleeps: list[float] = []

        def fake_run(command, **kwargs):
            calls.append((tuple(command), kwargs.get("input")))
            return subprocess.CompletedProcess(command, 0, stdout="OLD-CLIPBOARD", stderr="")

        return calls, sleeps, fake_run

    def test_delivery_restores_previous_clipboard_after_the_configured_interval(self) -> None:
        calls, sleeps, fake_run = self._capture()
        paster = self._paster(restore_clipboard=True, restore_delay_ms=500)

        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ):
            paster.copy_and_paste("transcript")

        inputs = [text for _command, text in calls if text is not None]
        self.assertEqual(["transcript", "OLD-CLIPBOARD"], inputs)
        self.assertEqual([0.5], sleeps)

    def test_restoration_disabled_never_reads_or_restores(self) -> None:
        calls, sleeps, fake_run = self._capture()
        paster = self._paster(restore_clipboard=False, restore_delay_ms=500)

        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ):
            paster.copy_and_paste("transcript")

        self.assertEqual(["transcript"], [text for _command, text in calls if text is not None])
        self.assertNotIn(("xclip", "-selection", "clipboard", "-o"), [c for c, _ in calls])
        self.assertEqual([], sleeps)

    def test_copy_alone_never_restores_so_a_refused_transcript_survives(self) -> None:
        calls, sleeps, fake_run = self._capture()
        paster = self._paster(restore_clipboard=True, restore_delay_ms=500)

        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ):
            paster.copy("transcript")

        self.assertEqual(["transcript"], [text for _command, text in calls if text is not None])
        self.assertEqual([], sleeps)

    def test_zero_delay_does_not_raise(self) -> None:
        calls, sleeps, fake_run = self._capture()
        paster = self._paster(restore_clipboard=True, restore_delay_ms=0)

        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ):
            paster.copy_and_paste("transcript")

        self.assertEqual([0.0], sleeps)


class UnavailableInjectorTests(unittest.TestCase):
    WAYLAND = {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}

    def _paster(self, injection: PasteInjection, **kwargs) -> ClipboardPaster:
        with patch("murmly.integrations.select_paste_injection", return_value=injection):
            return ClipboardPaster(
                env=dict(self.WAYLAND),
                which=lambda name: f"/usr/bin/{name}",
                **kwargs,
            )

    def test_a_session_without_an_injector_still_copies(self) -> None:
        calls: list[tuple[tuple, str | None]] = []

        def fake_run(command, **kwargs):
            calls.append((tuple(command), kwargs.get("input")))
            return subprocess.CompletedProcess(command, 0, stdout="OLD-CLIPBOARD", stderr="")

        paster = self._paster(
            PasteInjection(None, None, reason="No Wayland paste injector is installed."),
            restore_clipboard=True,
            restore_delay_ms=500,
        )
        sleeps: list[float] = []
        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ):
            outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("No Wayland paste injector", outcome.reason)
        # Copied, never restored over: the transcript is all the user has left.
        self.assertEqual(["transcript"], [text for _command, text in calls if text is not None])
        self.assertEqual([], sleeps)

    def test_an_injector_failing_mid_delivery_leaves_the_transcript_on_the_clipboard(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(command, **kwargs):
            calls.append(tuple(command))
            if command[0] == "wtype":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout="OLD-CLIPBOARD", stderr="")

        paster = self._paster(
            PasteInjection("wtype", ("wtype", "-M", "ctrl", "v", "-m", "ctrl")),
            restore_clipboard=True,
            restore_delay_ms=500,
        )
        sleeps: list[float] = []
        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.time.sleep", sleeps.append
        ), patch(
            "murmly.integrations.select_paste_injection",
            return_value=PasteInjection(None, None, reason="wtype failed earlier in this session"),
        ):
            outcome = paster.copy_and_paste("transcript")

        self.assertFalse(outcome.injected)
        self.assertIn("wtype failed to inject the paste", outcome.reason)
        self.assertEqual([], sleeps)
        self.assertNotIn(("wl-copy",), calls[2:])

    def test_a_failed_injector_is_not_used_again_this_session(self) -> None:
        def fake_run(command, **_kwargs):
            if command[0] == "wtype":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, stdout="OLD", stderr="")

        paster = self._paster(
            PasteInjection("wtype", ("wtype", "-M", "ctrl", "v", "-m", "ctrl")),
            restore_clipboard=False,
        )
        replacement = PasteInjection("ydotool", ("ydotool", "key", "29:1", "47:1", "47:0", "29:0"))
        with patch("murmly.integrations.subprocess.run", fake_run), patch(
            "murmly.integrations.select_paste_injection", return_value=replacement
        ) as reselect:
            paster.copy_and_paste("transcript")

        self.assertEqual("ydotool", paster.injection.method)
        self.assertEqual({"wtype"}, reselect.call_args.kwargs["excluded"])
