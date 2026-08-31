from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Protocol

from murmly.platform import Desktop, OperatingSystem, PlatformProfile, resolve_platform


logger = logging.getLogger(__name__)


MAX_MESSAGE_BYTES = 1_024
MIN_ERROR_DURATION_MS = 100
MAX_ERROR_DURATION_MS = 10_000
MAX_PARTIAL_CHARS = 200
PARTIAL_MESSAGE_PREFIX = b'{"type":"partial"'
#: PyGObject and GTK4 are distribution packages, not wheels -- there is no
#: `pip install pygobject` that works the way `pip install PySide6` does -- so
#: the renderer that imports them has to run under the interpreter those
#: packages were actually installed into, which is the system one, not
#: whatever interpreter Murmly's own daemon happens to be running under (a
#: `uv`-managed virtualenv on every machine this ships to today). A renderer
#: added later for a platform whose toolkit *is* a wheel (see
#: `renderer_python` below) is not bound by this and should run under the
#: project's own interpreter instead.
GTK4_RENDERER_PYTHON = Path("/usr/bin/python3")
#: Kept as an alias -- not a second name for a second thing -- because nothing
#: today has more than one renderer, and every caller that predates
#: `renderer_python` still reads plainly as "the interpreter the overlay
#: renderer needs".
SYSTEM_PYTHON = GTK4_RENDERER_PYTHON
COMMON_RENDERER_ENVIRONMENT_KEYS = {
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CURRENT_DESKTOP",
    "XDG_DATA_DIRS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_DESKTOP",
    "XDG_SESSION_TYPE",
}
#: What the Windows Qt renderer's environment is built from instead of
#: `COMMON_RENDERER_ENVIRONMENT_KEYS`, which is entirely Linux/XDG/D-Bus
#: vocabulary. `SYSTEMROOT` is not optional: Winsock, and much of Win32
#: through it, refuses to initialize without it, so a Windows child launched
#: with an env dict that omits it fails before it ever reaches the renderer's
#: own code -- a different failure than "the visual runtime is unavailable"
#: and one `renderer_environment` must not cause.
WINDOWS_RENDERER_ENVIRONMENT_KEYS = {
    "APPDATA",
    "COMSPEC",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


class OverlayState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"


class OverlayBackend(StrEnum):
    X11 = "x11"
    WAYLAND = "wayland"
    #: Windows' overlay renderer (task 10): a Qt process presenting the same
    #: newline-delimited JSON protocol and the same `overlay_shared` states as
    #: the GTK4 one. Named for the platform, not the toolkit, because the
    #: macOS renderer this design also calls for (section 15, not built here)
    #: may end up on PyObjC rather than Qt -- design.md's own recorded risk --
    #: and a member spelled `qt` would misdescribe that renderer too.
    WINDOWS = "windows"


#: The Qt renderer script `renderer_script` returns for `OverlayBackend.WINDOWS`.
QT_RENDERER_SCRIPT_NAME = "overlay_renderer_qt.py"
#: The GTK4 renderer script every other backend returns.
GTK4_RENDERER_SCRIPT_NAME = "overlay_renderer.py"


def renderer_python(backend: OverlayBackend) -> Path:
    """The interpreter the renderer chosen for `backend` needs to run under.

    Asked per backend rather than read off one constant, because the two
    Linux backends share one answer for the reason recorded on
    `GTK4_RENDERER_PYTHON` -- both launch the GTK4 renderer, only the display
    protocol underneath it differs -- while Windows answers differently:
    PySide6 is a wheel, so its renderer runs under the interpreter Murmly's
    own daemon is already running under (`sys.executable`), never the system
    one.
    """
    if backend is OverlayBackend.WINDOWS:
        return Path(sys.executable)
    return GTK4_RENDERER_PYTHON


def renderer_script(backend: OverlayBackend) -> Path:
    """The renderer script `OverlayController` launches for `backend`.

    Alongside `renderer_python`: before task 10 every backend launched the
    same file (`overlay_renderer.py`), so `OverlayController` hardcoded it.
    Now the file also depends on the backend, so this is the second half of
    the same seam, read from the same place `renderer_python` is (this
    module and `cli.overlay_diagnostics`), rather than the file name being
    guessed again wherever a renderer gets launched.
    """
    name = QT_RENDERER_SCRIPT_NAME if backend is OverlayBackend.WINDOWS else GTK4_RENDERER_SCRIPT_NAME
    return Path(__file__).with_name(name)


class LevelSink(Protocol):
    def __call__(self, level: float) -> None: ...


@dataclass(frozen=True, slots=True)
class OverlayHealth:
    available: bool
    detail: str | None = None


class OverlayLifecycle(Protocol):
    @property
    def health(self) -> OverlayHealth: ...

    def publish_state(self, state: OverlayState) -> None: ...

    def publish_level(self, level: float) -> None: ...

    def publish_partial(self, text: str) -> None: ...

    def publish_error(self, duration_ms: int = 2_000) -> None: ...

    def close(self) -> None: ...


def bound_partial_text(text: str) -> str:
    """Collapse a partial transcript to something the overlay protocol can carry.

    The tail is kept rather than the head: the newest speech is what the user is
    checking against what they just said.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_PARTIAL_CHARS:
        collapsed = collapsed[-MAX_PARTIAL_CHARS:]
    if not collapsed or _partial_message_bytes(collapsed) <= MAX_MESSAGE_BYTES:
        return collapsed
    # Binary search the longest tail that fits, rather than re-encoding the whole
    # message once per stripped character on the live worker's thread.
    low, high = 0, len(collapsed)
    while low < high:
        middle = (low + high) // 2
        if _partial_message_bytes(collapsed[middle:]) <= MAX_MESSAGE_BYTES:
            high = middle
        else:
            low = middle + 1
    return collapsed[low:]


def _partial_message_bytes(text: str) -> int:
    encoded = json.dumps(
        {"type": "partial", "value": text},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(encoded) + 1


def encode_overlay_message(message: dict[str, object]) -> bytes:
    message_type = message.get("type")
    if message_type == "partial":
        value = message.get("value")
        if set(message) != {"type", "value"} or not isinstance(value, str):
            raise ValueError("Invalid overlay partial message.")
        message = {"type": "partial", "value": bound_partial_text(value)}
    elif message_type == "state":
        if set(message) != {"type", "value"} or message.get("value") not in OverlayState:
            raise ValueError("Invalid overlay state message.")
    elif message_type == "level":
        value = message.get("value")
        if (
            set(message) != {"type", "value"}
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise ValueError("Invalid overlay level message.")
    elif message_type == "error":
        duration_ms = message.get("duration_ms")
        if (
            set(message) != {"type", "duration_ms"}
            or isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not MIN_ERROR_DURATION_MS <= duration_ms <= MAX_ERROR_DURATION_MS
        ):
            raise ValueError("Invalid overlay error message.")
    elif message_type == "shutdown":
        if set(message) != {"type"}:
            raise ValueError("Invalid overlay shutdown message.")
    else:
        raise ValueError("Unknown overlay message type.")

    encoded = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("Overlay message is too large.")
    return encoded


def is_plasma_desktop(environment: dict[str, str] | None = None) -> bool:
    source = environment if environment is not None else os.environ
    return resolve_platform(source).desktop is Desktop.PLASMA


def overlay_backend_for_profile(profile: PlatformProfile) -> OverlayBackend | None:
    """The pure `profile -> backend` mapping `detect_overlay_backend` delegates to.

    Split out because `PlatformProfile.operating_system` comes from
    `sys.platform` inside `resolve_platform`, which is not one of the keys a
    caller can steer through `detect_overlay_backend`'s `environment`
    parameter -- so a test running on Linux can never make
    `detect_overlay_backend(...)` answer `OverlayBackend.WINDOWS` by supplying
    an environment dict alone. Exercising the Windows branch means
    constructing a `PlatformProfile` directly and calling this function, the
    same shape `operating_system_for` already gives the OS mapping itself
    (`platform/__init__.py`).
    """
    if profile.operating_system is OperatingSystem.WINDOWS:
        # Every interactive Windows session has a desktop to draw on; whether
        # it can host a layered, click-through, non-activating surface is
        # what the Qt renderer's own `--check` answers (task 10.5), the same
        # split kept below between "Plasma is here" and "this session's
        # display is usable".
        return OverlayBackend.WINDOWS
    if profile.desktop is not Desktop.PLASMA:
        return None
    if profile.session_type == "wayland":
        return OverlayBackend.WAYLAND if profile.wayland_display else None
    if profile.session_type == "x11":
        return OverlayBackend.X11 if profile.x11_display else None
    if not profile.session_type and profile.wayland_display:
        return OverlayBackend.WAYLAND
    if not profile.session_type and profile.x11_display:
        return OverlayBackend.X11
    return None


def detect_overlay_backend(environment: dict[str, str] | None = None) -> OverlayBackend | None:
    source = environment if environment is not None else os.environ
    return overlay_backend_for_profile(resolve_platform(source))


def renderer_environment(
    backend: OverlayBackend,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    source = environment if environment is not None else os.environ
    if backend is OverlayBackend.WINDOWS:
        # None of the GTK4/X11/Wayland vocabulary above applies here, and
        # `GDK_BACKEND`/`PYTHONNOUSERSITE` are GTK-renderer concerns the Qt
        # renderer has no use for. `SYSTEMROOT` and the rest are what Winsock
        # and Win32 need present to initialize at all -- see
        # `WINDOWS_RENDERER_ENVIRONMENT_KEYS`.
        return {key: source[key] for key in WINDOWS_RENDERER_ENVIRONMENT_KEYS if key in source}
    keys = set(COMMON_RENDERER_ENVIRONMENT_KEYS)
    if backend is OverlayBackend.WAYLAND:
        keys.add("WAYLAND_DISPLAY")
    else:
        keys.update({"DISPLAY", "XAUTHORITY"})
    sanitized = {key: source[key] for key in keys if key in source}
    sanitized["GDK_BACKEND"] = backend.value
    sanitized["PYTHONNOUSERSITE"] = "1"
    return sanitized


class OverlayController:
    def __init__(
        self,
        *,
        bottom_margin_px: int,
        reduced_motion: bool,
        text_size_px: int = 13,
        transcript_panel: bool = False,
        backend: OverlayBackend | None = None,
        helper_path: Path | None = None,
        popen_factory: Callable[..., object] = subprocess.Popen,
        socket_pair_factory: Callable[[], tuple[object, object]] = socket.socketpair,
        clock: Callable[[], float] = time.monotonic,
        restart_delays: tuple[float, ...] = (0.0, 1.0, 5.0),
        autostart: bool = True,
    ) -> None:
        self._bottom_margin_px = bottom_margin_px
        self._reduced_motion = reduced_motion
        self._text_size_px = text_size_px
        self._transcript_panel = transcript_panel
        self._backend = backend or detect_overlay_backend()
        self._helper_path = (
            helper_path or renderer_script(self._backend if self._backend is not None else OverlayBackend.X11)
        ).resolve()
        self._popen_factory = popen_factory
        self._socket_pair_factory = socket_pair_factory
        self._clock = clock
        self._restart_delays = restart_delays
        self._condition = threading.Condition()
        self._health_lock = threading.Lock()
        self._transport_lock = threading.Lock()
        self._control_messages: deque[bytes] = deque()
        self._latest_level: bytes | None = None
        self._last_level_sent_at = float("-inf")
        self._closed = False
        self._attempts = 0
        self._next_attempt_at = 0.0
        self._health = OverlayHealth(available=False, detail="Overlay has not started.")
        self._process: object | None = None
        self._transport: object | None = None
        self._thread = threading.Thread(target=self._run, name="murmly-overlay", daemon=True)
        if autostart:
            self.start()

    @property
    def health(self) -> OverlayHealth:
        with self._health_lock:
            return self._health

    def start(self) -> None:
        try:
            if self._backend is None:
                self._set_health(False, "Overlay requires KDE Plasma on X11 or Wayland.")
            elif not self._thread.is_alive() and not self._closed:
                self._thread.start()
        except Exception as error:
            self._set_health(False, f"Unable to start overlay controller: {error}")

    def publish_state(self, state: OverlayState) -> None:
        try:
            self._enqueue_control(encode_overlay_message({"type": "state", "value": state.value}))
        except Exception as error:
            self._set_health(False, f"Unable to queue overlay state: {error}")

    def publish_level(self, level: float) -> None:
        try:
            encoded = encode_overlay_message({"type": "level", "value": level})
            with self._condition:
                if self._closed:
                    return
                self._latest_level = encoded
                self._condition.notify()
        except Exception as error:
            self._set_health(False, f"Unable to queue overlay level: {error}")

    def publish_partial(self, text: str) -> None:
        try:
            encoded = encode_overlay_message({"type": "partial", "value": text})
            with self._condition:
                if self._closed:
                    return
                # Latest-wins, but still in the queue: only the newest partial is
                # worth showing, and appending every one lets a stalled renderer
                # build an unbounded backlog and then replay stale speech. Dropping
                # superseded partials in place keeps ordering against the state
                # changes that clear them, which a separate slot would lose.
                superseded = [
                    message
                    for message in self._control_messages
                    if message.startswith(PARTIAL_MESSAGE_PREFIX)
                ]
                for message in superseded:
                    self._control_messages.remove(message)
                self._control_messages.append(encoded)
                self._condition.notify()
        except Exception as error:
            self._set_health(False, f"Unable to queue overlay partial: {error}")

    def publish_error(self, duration_ms: int = 2_000) -> None:
        try:
            encoded = encode_overlay_message({"type": "error", "duration_ms": duration_ms})
            self._enqueue_control(encoded)
        except Exception as error:
            self._set_health(False, f"Unable to queue overlay error: {error}")

    def close(self) -> None:
        try:
            with self._condition:
                if self._closed:
                    return
                self._closed = True
                self._control_messages.append(encode_overlay_message({"type": "shutdown"}))
                self._condition.notify_all()
            if self._thread.is_alive():
                self._thread.join(timeout=0.5)
            if self._thread.is_alive():
                self._close_transport()
                self._thread.join(timeout=0.5)
            self._terminate_process()
        except Exception as error:
            self._set_health(False, f"Unable to close overlay cleanly: {error}")

    def _enqueue_control(self, encoded: bytes) -> None:
        with self._condition:
            if self._closed:
                return
            self._control_messages.append(encoded)
            self._condition.notify()

    def _run(self) -> None:
        self._launch_renderer()
        while True:
            encoded, stop_after_send = self._next_message()
            if encoded is None:
                return
            message = json.loads(encoded)
            is_listening = message.get("type") == "state" and message.get("value") == "LISTENING"
            if is_listening and not self._ensure_renderer_for_listening():
                continue
            sent = self._send(encoded)
            if is_listening and not sent and self._ensure_renderer_for_listening():
                self._send(encoded)
            if stop_after_send:
                self._close_transport()
                return

    def _next_message(self) -> tuple[bytes | None, bool]:
        level_interval = 1.0 / 30.0
        with self._condition:
            while True:
                if self._control_messages:
                    encoded = self._control_messages.popleft()
                    return encoded, json.loads(encoded).get("type") == "shutdown"
                if self._closed:
                    return None, True
                now = self._clock()
                if self._latest_level is not None and now - self._last_level_sent_at >= level_interval:
                    encoded = self._latest_level
                    self._latest_level = None
                    self._last_level_sent_at = now
                    return encoded, False
                timeout = None
                if self._latest_level is not None:
                    timeout = max(level_interval - (now - self._last_level_sent_at), 0.0)
                self._condition.wait(timeout=timeout)

    def _launch_renderer(self) -> bool:
        if self._backend is None:
            return False
        if self._transport is not None or self._attempts >= len(self._restart_delays):
            return self._transport is not None
        now = self._clock()
        if now < self._next_attempt_at:
            return False
        self._attempts += 1
        parent_transport = None
        child_transport = None
        try:
            parent_transport, child_transport = self._socket_pair_factory()
            if self._backend is OverlayBackend.WINDOWS:
                process = self._spawn_windows_renderer(child_transport)
            else:
                process = self._spawn_posix_renderer(child_transport)
            child_transport.close()
            with self._transport_lock:
                self._process = process
                self._transport = parent_transport
            self._set_health(True, None)
            return True
        except Exception as error:
            if parent_transport is not None:
                parent_transport.close()
            if child_transport is not None:
                child_transport.close()
            self._set_health(False, f"Unable to launch overlay renderer: {error}")
            self._schedule_next_attempt()
            return False

    def _renderer_path_argument(self, path: Path) -> str:
        """Render `path` the way this backend's real launch would.

        `.as_posix()` for the POSIX renderers (X11/Wayland): they are launched
        only on Linux in real use, so their command line is a POSIX path
        regardless of which host's flavour `Path` would otherwise render -- a
        Windows runner exercising this same Linux-only launch logic would
        otherwise get backslashes here. Windows takes `str(path)` instead,
        because there its command line genuinely is rendered by a
        `WindowsPath` on the host that actually launches it.
        """
        if self._backend is OverlayBackend.WINDOWS:
            return str(path)
        return path.as_posix()

    def _renderer_command(self, fd_arguments: list[str]) -> list[str]:
        command = [
            self._renderer_path_argument(renderer_python(self._backend)),
            self._renderer_path_argument(self._helper_path),
            *fd_arguments,
            "--bottom-margin-px",
            str(self._bottom_margin_px),
            "--text-size-px",
            str(self._text_size_px),
            "--backend",
            self._backend.value,
        ]
        if self._reduced_motion:
            command.append("--reduced-motion")
        if self._transcript_panel:
            command.append("--transcript-panel")
        return command

    def _spawn_posix_renderer(self, child_transport: object) -> object:
        command = self._renderer_command(["--fd", str(child_transport.fileno())])
        return self._popen_factory(
            command,
            close_fds=True,
            env=renderer_environment(self._backend),
            pass_fds=(child_transport.fileno(),),
            shell=False,
            stdin=subprocess.DEVNULL,
        )

    def _spawn_windows_renderer(self, child_transport: object) -> object:
        """Hand the child its half of the socketpair over stdin, `share()`d.

        `pass_fds` is POSIX-only -- `subprocess` raises on Windows if it is
        given a non-empty value -- and a `socket.socketpair()` socket has no
        POSIX file descriptor a Windows child could inherit that way even if
        it did not. `socket.socket.share(pid)` is Windows' own replacement,
        and it needs the *target* process's id, which exists only once
        `Popen` has already started it. So the child is spawned first, with
        its stdin piped; the parent then shares `child_transport` for that
        specific process id and writes the resulting bytes down the pipe,
        closing it once they are sent. `overlay_renderer_qt.py`'s
        `--fd-share-stdin` reads them with `sys.stdin.buffer.read()` and
        reconstructs the socket with `socket.fromshare`.

        Unverified on real Windows, unlike `_spawn_posix_renderer`: whether a
        child spawned this way actually receives a usable socket, and whether
        `.share()`/`.fromshare()` round-trip the way this assumes, can only be
        confirmed there. See task 10.1.
        """
        command = self._renderer_command(["--fd-share-stdin"])
        process = self._popen_factory(
            command,
            env=renderer_environment(self._backend),
            shell=False,
            stdin=subprocess.PIPE,
        )
        try:
            share_data = child_transport.share(process.pid)
            process.stdin.write(share_data)
            process.stdin.close()
        except Exception:
            try:
                process.terminate()
            except Exception:  # noqa: BLE001 - the original failure is what gets reported
                pass
            raise
        return process

    def _send(self, encoded: bytes) -> bool:
        with self._transport_lock:
            transport = self._transport
            process = self._process
        if transport is None:
            return False
        try:
            if process is not None and process.poll() is not None:
                raise BrokenPipeError("Overlay renderer exited.")
            transport.sendall(encoded)
            return True
        except (BrokenPipeError, OSError) as error:
            self._set_health(False, str(error))
            self._close_transport()
            self._terminate_process()
            self._schedule_next_attempt()
            return False

    def _ensure_renderer_for_listening(self) -> bool:
        while not self._closed:
            if self._transport is not None:
                return True
            if self._launch_renderer():
                return True
            if self._attempts >= len(self._restart_delays):
                return False
            with self._condition:
                if self._listening_is_superseded():
                    return False
                timeout = max(self._next_attempt_at - self._clock(), 0.0)
                self._condition.wait(timeout=timeout)
                if self._listening_is_superseded():
                    return False
        return False

    def _listening_is_superseded(self) -> bool:
        for encoded in self._control_messages:
            message = json.loads(encoded)
            if message.get("type") in {"error", "shutdown"}:
                return True
            if message.get("type") == "state" and message.get("value") != "LISTENING":
                return True
        return False

    def _schedule_next_attempt(self) -> None:
        if self._attempts < len(self._restart_delays):
            self._next_attempt_at = self._clock() + self._restart_delays[self._attempts]

    def _close_transport(self) -> None:
        with self._transport_lock:
            transport = self._transport
            self._transport = None
        if transport is not None:
            try:
                transport.close()
            except OSError:
                pass

    def _terminate_process(self) -> None:
        with self._transport_lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass

    def _set_health(self, available: bool, detail: str | None) -> None:
        with self._health_lock:
            previous = self._health
            current = OverlayHealth(available=available, detail=detail)
            self._health = current
        if not available and current != previous:
            logger.warning("Overlay unavailable: %s", detail)


class NullOverlayController:
    def __init__(self, detail: str = "Overlay is disabled.") -> None:
        self._health = OverlayHealth(available=False, detail=detail)

    @property
    def health(self) -> OverlayHealth:
        return self._health

    def publish_state(self, state: OverlayState) -> None:
        del state

    def publish_level(self, level: float) -> None:
        del level

    def publish_partial(self, text: str) -> None:
        del text

    def publish_error(self, duration_ms: int = 2_000) -> None:
        del duration_ms

    def close(self) -> None:
        pass