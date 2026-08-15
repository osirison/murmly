from __future__ import annotations

from collections.abc import Callable
import os
from shutil import which as shutil_which
import subprocess
import time


Which = Callable[[str], str | None]


class MissingToolError(RuntimeError):
    pass


def is_wayland_session(env: dict[str, str] | None = None) -> bool:
    environment = env or os.environ
    session_type = environment.get("XDG_SESSION_TYPE", "").lower()
    return bool(environment.get("WAYLAND_DISPLAY")) or session_type == "wayland"


def choose_clipboard_copy_command(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
) -> list[str]:
    if is_wayland_session(env):
        if which("wl-copy"):
            return ["wl-copy"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard"]
    elif which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if is_wayland_session(env):
        raise MissingToolError("No Wayland clipboard command found; install wl-clipboard or xclip.")
    raise MissingToolError("No X11 clipboard command found; install xclip.")


def choose_clipboard_read_command(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
) -> list[str] | None:
    if is_wayland_session(env):
        if which("wl-paste"):
            return ["wl-paste", "--no-newline"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-o"]
    elif which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    return None


def choose_paste_command(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
) -> list[str]:
    if is_wayland_session(env):
        if which("wtype"):
            return ["wtype", "-M", "ctrl", "v", "-m", "ctrl"]
        if which("ydotool"):
            return ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]
    elif which("xdotool"):
        return ["xdotool", "key", "--clearmodifiers", "ctrl+v"]
    if is_wayland_session(env):
        raise MissingToolError("No Wayland paste injector found; install wtype or ydotool.")
    raise MissingToolError("No X11 paste injector found; install xdotool.")


class ClipboardPaster:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        which: Which = shutil_which,
        restore_clipboard: bool = True,
        restore_delay_ms: int = 200,
    ) -> None:
        self._env = env or os.environ
        self._copy_command = choose_clipboard_copy_command(self._env, which)
        self._read_command = choose_clipboard_read_command(self._env, which)
        self._paste_command = choose_paste_command(self._env, which)
        self._restore_clipboard = restore_clipboard
        self._restore_delay_ms = restore_delay_ms

    def copy_and_paste(self, text: str) -> None:
        previous = self._read_clipboard() if self._restore_clipboard else None
        self.copy(text)
        self._run(self._paste_command)
        if previous is not None:
            time.sleep(self._restore_delay_ms / 1000)
            self.copy(previous)

    def copy(self, text: str) -> None:
        self._run(self._copy_command, text)

    def _read_clipboard(self) -> str | None:
        if not self._read_command:
            return None
        result = subprocess.run(
            self._read_command,
            check=True,
            capture_output=True,
            text=True,
            env=self._env,
        )
        return result.stdout

    def _run(self, command: list[str], stdin_text: str | None = None) -> None:
        subprocess.run(
            command,
            input=stdin_text,
            text=True,
            check=True,
            env=self._env,
        )
