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
import threading
import time
from typing import Protocol


logger = logging.getLogger(__name__)


MAX_MESSAGE_BYTES = 1_024
MIN_ERROR_DURATION_MS = 100
MAX_ERROR_DURATION_MS = 10_000
MAX_PARTIAL_CHARS = 200
SYSTEM_PYTHON = Path("/usr/bin/python3")
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


class OverlayState(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"


class OverlayBackend(StrEnum):
    X11 = "x11"
    WAYLAND = "wayland"


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
    while collapsed and _partial_message_bytes(collapsed) > MAX_MESSAGE_BYTES:
        collapsed = collapsed[1:]
    return collapsed


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
    desktop = f"{source.get('XDG_CURRENT_DESKTOP', '')}:{source.get('XDG_SESSION_DESKTOP', '')}"
    return any(name in desktop.casefold() for name in ("kde", "plasma"))


def detect_overlay_backend(environment: dict[str, str] | None = None) -> OverlayBackend | None:
    source = environment if environment is not None else os.environ
    if not is_plasma_desktop(source):
        return None
    session_type = source.get("XDG_SESSION_TYPE", "").casefold()
    if session_type == "wayland":
        return OverlayBackend.WAYLAND if source.get("WAYLAND_DISPLAY") else None
    if session_type == "x11":
        return OverlayBackend.X11 if source.get("DISPLAY") else None
    if not session_type and source.get("WAYLAND_DISPLAY"):
        return OverlayBackend.WAYLAND
    if not session_type and source.get("DISPLAY"):
        return OverlayBackend.X11
    return None


def renderer_environment(
    backend: OverlayBackend,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    source = environment if environment is not None else os.environ
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
        self._helper_path = (helper_path or Path(__file__).with_name("overlay_renderer.py")).resolve()
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
            # Queued with state changes rather than replacing the latest like a
            # level, so a partial can never overtake the transition that clears it.
            self._enqueue_control(encode_overlay_message({"type": "partial", "value": text}))
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
            command = [
                str(SYSTEM_PYTHON),
                str(self._helper_path),
                "--fd",
                str(child_transport.fileno()),
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
            process = self._popen_factory(
                command,
                close_fds=True,
                env=renderer_environment(self._backend),
                pass_fds=(child_transport.fileno(),),
                shell=False,
                stdin=subprocess.DEVNULL,
            )
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