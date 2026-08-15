from __future__ import annotations

import unittest

from murmly.integrations import MissingToolError, choose_clipboard_copy_command, choose_paste_command


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
