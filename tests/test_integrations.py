from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from murmly.integrations import (
    ClipboardPaster,
    MissingToolError,
    choose_clipboard_copy_command,
    choose_paste_command,
)


def fake_which_factory(*available: str):
    available_set = set(available)

    def fake_which(command: str) -> str | None:
        return f"/usr/bin/{command}" if command in available_set else None

    return fake_which


class IntegrationSelectionTests(unittest.TestCase):
    def test_wayland_prefers_wl_copy_and_wtype(self) -> None:
        env = {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"}
        which = fake_which_factory("wl-copy", "wtype", "xdotool")
        self.assertEqual(["wl-copy"], choose_clipboard_copy_command(env, which))
        self.assertEqual(["wtype", "-M", "ctrl", "v", "-m", "ctrl"], choose_paste_command(env, which))

    def test_x11_prefers_xclip_and_xdotool(self) -> None:
        env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}
        which = fake_which_factory("xclip", "xdotool", "wl-copy")
        self.assertEqual(["xclip", "-selection", "clipboard"], choose_clipboard_copy_command(env, which))
        self.assertEqual(["xdotool", "key", "--clearmodifiers", "ctrl+v"], choose_paste_command(env, which))

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
