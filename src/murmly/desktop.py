"""Read-only queries against the KDE Plasma global shortcut daemon.

Everything here is a getter. Murmly registers a hotkey by writing a launcher
file and letting Plasma discover it, never by calling a shortcut setter; see
``docs/agent-notes/plasma-global-shortcut-binding.md`` and ``design.md`` for why
the setter path registers a shortcut that never fires.

Only signatures made of plain scalars are permitted. ``kglobalacceld``
demarshals an inbound key-sequence struct with four unconditional reads, so any
``(ai)`` argument whose length is not exactly four reads past the end and aborts
the daemon. :data:`ALLOWED_SIGNATURES` enforces that no such call can be built.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
import subprocess

from murmly.overlay import OverlayBackend, detect_overlay_backend, is_plasma_desktop


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

BUS_NAME = "org.kde.kglobalaccel"
OBJECT_PATH = "/kglobalaccel"
INTERFACE = "org.kde.KGlobalAccel"
LAUNCH_ACTION = "_launch"
QUERY_TIMEOUT_SECONDS = 5.0

#: Argument signatures Murmly is allowed to send. Scalars only, by design: an
#: inbound "(ai)" struct crashes kglobalacceld unless it holds exactly four
#: integers, and Murmly never needs one.
ALLOWED_SIGNATURES = frozenset({"", "i", "is", "s", "as"})

#: The session types on which the binding mechanism has been verified
#: end-to-end. Others still work through the same code path, but Murmly reports
#: them as unverified rather than claiming them.
VERIFIED_SESSION_TYPES = frozenset({OverlayBackend.X11})


class DesktopQueryError(RuntimeError):
    """A desktop query could not be answered.

    Distinct from a query that answered "not present": callers must not treat a
    failed lookup as evidence that a hotkey is free.
    """


@dataclass(frozen=True, slots=True)
class ShortcutOwner:
    action_unique: str
    action_friendly: str
    component_unique: str
    component_friendly: str

    @property
    def label(self) -> str:
        """A human-readable owner name for a refusal message."""
        friendly = self.component_friendly or self.component_unique
        if self.action_friendly and self.action_friendly != friendly:
            return f"{friendly} ({self.action_friendly})"
        return friendly


@dataclass(frozen=True, slots=True)
class DesktopSession:
    """What Murmly can do about hotkeys in the current session."""

    is_plasma: bool
    session_type: str
    backend: OverlayBackend | None
    supported: bool
    verified: bool
    detail: str


def detect_desktop_session(environment: dict[str, str] | None = None) -> DesktopSession:
    source = environment if environment is not None else os.environ
    session_type = source.get("XDG_SESSION_TYPE", "").casefold() or "unknown"
    plasma = is_plasma_desktop(source)
    backend = detect_overlay_backend(source)

    if not plasma:
        return DesktopSession(
            is_plasma=False,
            session_type=session_type,
            backend=None,
            supported=False,
            verified=False,
            detail="Hotkey registration requires KDE Plasma.",
        )
    if backend is None:
        return DesktopSession(
            is_plasma=True,
            session_type=session_type,
            backend=None,
            supported=False,
            verified=False,
            detail="KDE Plasma was detected but no graphical display was found in the environment.",
        )

    verified = backend in VERIFIED_SESSION_TYPES
    detail = (
        f"KDE Plasma on {backend.value}."
        if verified
        else (
            f"KDE Plasma on {backend.value}. Hotkey registration is unverified on this "
            "session type; Murmly will register the hotkey and then check whether it took effect."
        )
    )
    return DesktopSession(
        is_plasma=True,
        session_type=session_type,
        backend=backend,
        supported=True,
        verified=verified,
        detail=detail,
    )


class PlasmaShortcuts:
    """Read-only view of the running Plasma shortcut registry."""

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        timeout: float = QUERY_TIMEOUT_SECONDS,
        busctl: str = "busctl",
    ) -> None:
        self._run_command = run_command
        self._timeout = timeout
        self._busctl = busctl

    def is_available(self, keycode: int) -> bool:
        """Whether ``keycode`` is free for a new owner.

        A hotkey already held by Murmly reports as unavailable, so callers must
        check ownership before treating this as a conflict.
        """
        payload = self._call("isGlobalShortcutAvailable", "is", str(keycode), "")
        value = payload["data"][0]
        if not isinstance(value, bool):
            raise DesktopQueryError("Shortcut availability query returned a non-boolean result.")
        return value

    def owners_of(self, keycode: int) -> list[ShortcutOwner]:
        """Every registered owner of ``keycode``.

        More than one owner means a silent double-bind: Plasma refcounts the
        grab and delivers the key to the lowest serial, so the newcomer never
        fires.
        """
        payload = self._call("getGlobalShortcutsByKey", "i", str(keycode))
        rows = payload["data"][0]
        owners: list[ShortcutOwner] = []
        for row in rows:
            if len(row) < 4:
                raise DesktopQueryError("Shortcut owner query returned a malformed row.")
            owners.append(
                ShortcutOwner(
                    action_unique=str(row[0]),
                    action_friendly=str(row[1]),
                    component_unique=str(row[2]),
                    component_friendly=str(row[3]),
                )
            )
        return owners

    def component_exists(self, component: str) -> bool:
        """Whether Plasma has built a shortcut component for ``component``.

        An absent component is reported as ``False``; a query that could not be
        answered raises, so a poll never mistakes a broken bus for "gone".
        """
        result = self._run(["call", BUS_NAME, OBJECT_PATH, INTERFACE, "getComponent", "s", component])
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").strip()
        if "doesn't exist" in stderr or "does not exist" in stderr:
            return False
        raise DesktopQueryError(f"Unable to query component {component!r}: {stderr or 'unknown error'}")

    def registered_keys(self, component: str) -> list[int]:
        """The key codes Plasma currently has registered for ``component``.

        Reading a key sequence back is safe; only *sending* one is not.
        """
        payload = self._call(
            "shortcutKeys",
            "as",
            "4",
            component,
            LAUNCH_ACTION,
            "",
            "",
        )
        sequences = payload["data"][0]
        keys: list[int] = []
        for sequence in sequences:
            # Each sequence is a struct holding one array of four key
            # combinations, zero-padded for the slots a shortcut does not use.
            if not sequence or not isinstance(sequence[0], list):
                raise DesktopQueryError("Registered key query returned a malformed sequence.")
            keys.extend(int(value) for value in sequence[0] if int(value) != 0)
        return keys

    def _call(self, method: str, signature: str, *arguments: str) -> dict[str, object]:
        if signature not in ALLOWED_SIGNATURES:
            raise DesktopQueryError(
                f"Refusing to call {method} with signature {signature!r}. Murmly sends only "
                "scalar arguments; a key-sequence struct aborts the Plasma shortcut daemon."
            )
        result = self._run(
            ["call", BUS_NAME, OBJECT_PATH, INTERFACE, method, signature, *arguments]
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise DesktopQueryError(f"{method} failed: {stderr or 'unknown error'}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DesktopQueryError(f"{method} returned unreadable output: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
            raise DesktopQueryError(f"{method} returned an unexpected payload.")
        return payload

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [self._busctl, "--user", "--json=short", *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopQueryError(f"Unable to reach the Plasma shortcut daemon: {error}") from error
