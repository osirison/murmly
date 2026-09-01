"""Persisted record of which hotkeys Murmly has bound and what each is for.

On KDE Plasma and GNOME, the desktop itself holds the binding -- a launcher
file, a `custom-keybindings` entry -- and that is the one source of truth
`doctor` and a fresh session both read. This module exists for the other
case: a platform that registers a hotkey inside Murmly's own process
(Windows' `RegisterHotKey`, section 8; macOS's Carbon `RegisterEventHotKey`,
section 13). Such a registration exists only while the daemon that made it is
running, so nothing external records it the way a launcher file or a dconf
value does, and the daemon has to re-create it at every session start from
something Murmly itself wrote.

`HotkeyRecordStore` is that something: a small file mapping purpose key
(`"window"`, `"session"`) to Murmly's cross-platform hotkey currency -- KDE
portable text, the same string `Hotkey.portable` already produces and every
encoder in `hotkey.py` can parse back via `parse_specification`. It is written
on every successful install regardless of which desktop bound the key, so it
is ready for either in-process backend to read -- but it must never be
*read* on a platform whose binding the desktop itself holds, since a second
copy of that state can drift from the one the desktop actually has.
`platform.hotkey_mechanism_is_in_process` is the guard that decides which
platform that is; `IN_PROCESS_HOTKEY_MECHANISMS` names `windows-hotkey` and
`macos-hotkey`, so `rebind_from_record` below is a tested no-op only on
Plasma, GNOME, and any other Linux desktop.

The contract each in-process backend satisfies to plug into this: the daemon
creates its registrar once at startup and keeps the instance
(`MurmlyDaemon.__init__`'s own `self._hotkey_registrar` dispatch, by resolved
operating system), and that instance exposes
``rebind(bindings: dict[str, str]) -> None`` taking exactly what
`HotkeyRecordStore.read()` returns -- `win_hotkey.WindowsHotkeyRegistrar` and
`mac_hotkey.MacosHotkeyRegistrar` both satisfy it, independently, against
their own platform's encoder.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from murmly.platform import BackendRegistry, PlatformProfile


def default_hotkey_record_path(env: dict[str, str] | None = None) -> Path:
    """Alongside `config.toml`: this is config-adjacent state, not user data."""
    from murmly.config import default_config_path

    return default_config_path(env).parent / "hotkeys.json"


def _write_atomically(path: Path, content: str) -> None:
    """The same shape as `installer.write_atomically`, kept as its own small
    copy rather than imported: `installer.py` already imports from this
    module's sibling concerns (`hotkey.py`, `desktop.py`), and importing
    `installer` from here to save ten lines would be the first cycle in that
    graph.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class HotkeyRecordStore:
    """Which purposes are bound, keyed by `HotkeyPurpose.key`.

    Values are KDE portable text (`Hotkey.portable`) regardless of which
    desktop actually bound the key -- Murmly's one platform-neutral currency,
    re-derivable on any platform via `hotkey.parse_specification`.
    """

    path: Path

    def read(self) -> dict[str, str]:
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}

    def write(self, bindings: dict[str, str]) -> None:
        _write_atomically(self.path, json.dumps(bindings, indent=2, sort_keys=True) + "\n")

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def rebind_from_record(
    profile: "PlatformProfile",
    record: HotkeyRecordStore,
    registrar: object | None,
    registry: "BackendRegistry | None" = None,
    in_process: frozenset[str] | None = None,
) -> str:
    """Re-register every bound hotkey from `record`, where that is needed.

    `registrar` is whatever object the daemon is holding for its in-process
    hotkey backend -- `None` on every platform today, since none registers
    in-process yet; a future Windows or macOS backend is the intended
    populator (see this module's own docstring for the contract it satisfies).

    Returns a one-line report rather than raising: a hotkey rebind must never
    be the reason a daemon fails to start or a command fails to answer.
    """
    from murmly.platform import HOTKEY_REGISTRATION, IN_PROCESS_HOTKEY_MECHANISMS

    active_registry = registry if registry is not None else HOTKEY_REGISTRATION
    active_in_process = in_process if in_process is not None else IN_PROCESS_HOTKEY_MECHANISMS
    mechanism = active_registry.select(profile).mechanism

    if mechanism not in active_in_process:
        return "Hotkeys on this platform are held by the desktop, not the daemon; nothing to rebind."

    bindings = record.read()
    if not bindings:
        return "No hotkey is recorded as bound; nothing to rebind."

    rebind = getattr(registrar, "rebind", None)
    if rebind is None:
        return f"No running {mechanism!r} hotkey registrar to rebind through."

    rebind(bindings)
    count = len(bindings)
    return f"Rebound {count} hotkey{'s' if count != 1 else ''} from the record."
