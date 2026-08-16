from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import socket
import threading

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig
from murmly.focus import (
    FocusObserver,
    WindowIdentity,
    create_focus_observer,
    record_target,
    should_deliver,
)
from murmly.integrations import ClipboardPaster
from murmly.overlay import (
    detect_overlay_backend,
    NullOverlayController,
    OverlayController,
    OverlayLifecycle,
    OverlayState,
)
from murmly.stt import FasterWhisperTranscriber


logger = logging.getLogger(__name__)
MAX_COMMAND_BYTES = 4_096
MAX_COMMAND_WORKERS = 8
COMMAND_TIMEOUT_SECONDS = 2.0


@dataclass(slots=True)
class ProcessingResult:
    text: str
    state: str
    delivered: bool = False
    detail: str | None = None


class SpeechSession:
    def __init__(
        self,
        config: MurmlyConfig,
        level_sink=None,
        focus_observer: FocusObserver | None = None,
    ) -> None:
        self._config = config
        self._recorder = SoundDeviceRecorder(config, level_sink=level_sink)
        self._transcriber = FasterWhisperTranscriber(config)
        self._paster: ClipboardPaster | None = None
        self._focus = focus_observer if focus_observer is not None else create_focus_observer()

    @property
    def focus_observer(self) -> FocusObserver:
        return self._focus

    def start_recording(self) -> None:
        self._recorder.start()

    def stop_recording(self) -> bytes:
        return self._recorder.stop()

    def capture_delivery_target(self) -> WindowIdentity | None:
        return record_target(self._focus)

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        text = self._transcriber.transcribe_pcm16(pcm_audio, self._recorder.sample_rate_hz)
        if not text:
            return ProcessingResult(text=text, state="DONE")
        allowed, reason = should_deliver(self._focus, target, self._config.verify_target)
        if allowed:
            self._ensure_paster().copy_and_paste(text)
            return ProcessingResult(text=text, state="DONE", delivered=True)
        logger.warning("Transcript delivery refused: %s", reason)
        self._ensure_paster().copy(text)
        return ProcessingResult(
            text=text,
            state="DONE",
            delivered=False,
            detail="Transcript copied to the clipboard but not pasted.",
        )

    def _ensure_paster(self) -> ClipboardPaster:
        if self._paster is None:
            self._paster = ClipboardPaster(
                restore_clipboard=self._config.restore_clipboard,
                restore_delay_ms=self._config.restore_clipboard_delay_ms,
            )
        return self._paster


class MurmlyDaemon:
    def __init__(
        self,
        config: MurmlyConfig,
        session: SpeechSession | None = None,
        overlay: OverlayLifecycle | None = None,
    ) -> None:
        self._config = config
        self._overlay = overlay or self._create_overlay(config)
        self._session = session or SpeechSession(config, level_sink=self._publish_level)
        self._state = "IDLE"
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._server: socket.socket | None = None
        self._worker_slots = threading.BoundedSemaphore(MAX_COMMAND_WORKERS)
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def serve_forever(self) -> None:
        self._config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.socket_path.unlink(missing_ok=True)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                self._server = server
                server.bind(str(self._config.socket_path))
                server.listen()
                server.settimeout(0.2)
                while not self._shutdown.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if self._shutdown.is_set():
                            break
                        raise
                    self._dispatch_connection(connection)
        finally:
            self._server = None
            self._config.socket_path.unlink(missing_ok=True)
            self._close_overlay()

    def shutdown(self) -> None:
        self._shutdown.set()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self._config.socket_path.unlink(missing_ok=True)
        self._close_overlay()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(COMMAND_TIMEOUT_SECONDS)
                response = self._handle_connection(connection)
                try:
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except (BrokenPipeError, OSError):
                    if not self._shutdown.is_set():
                        raise
        except (json.JSONDecodeError, socket.timeout, UnicodeDecodeError, ValueError):
            pass
        except OSError as error:
            if not self._shutdown.is_set():
                logger.warning("Command connection failed: %s", error)
        finally:
            with self._connections_lock:
                self._connections.discard(connection)
            self._worker_slots.release()

    def _dispatch_connection(self, connection: socket.socket) -> bool:
        if not self._worker_slots.acquire(blocking=False):
            connection.close()
            return False
        started = False
        try:
            with self._connections_lock:
                if self._shutdown.is_set():
                    return False
                self._connections.add(connection)
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    name="murmly-command",
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    self._connections.discard(connection)
                    raise
                started = True
            return True
        except RuntimeError as error:
            logger.warning("Unable to start command worker: %s", error)
            return False
        finally:
            if not started:
                connection.close()
                self._worker_slots.release()

    def _handle_connection(self, connection: socket.socket) -> dict[str, object]:
        payload = bytearray()
        while not payload.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            if len(payload) + len(chunk) > MAX_COMMAND_BYTES:
                return {"ok": False, "error": "Command exceeds the 4096-byte limit."}
            payload.extend(chunk)
        command = json.loads(payload.decode("utf-8") or "{}")
        return self.handle_command(str(command.get("command", "")))

    def handle_command(self, command: str) -> dict[str, object]:
        if command == "status":
            return {"ok": True, "state": self.state}
        if command != "toggle":
            return {"ok": False, "error": f"Unsupported command: {command}"}

        with self._lock:
            if self._state == "IDLE":
                try:
                    self._session.start_recording()
                except Exception as error:
                    self._publish_error()
                    return {"ok": False, "state": "IDLE", "error": str(error)}
                self._state = "LISTENING"
                state = self._state
                self._publish_state(OverlayState.LISTENING)
                return {"ok": True, "state": state}
            if self._state != "LISTENING":
                return {"ok": False, "state": self._state, "error": "Daemon is busy."}
            try:
                pcm_audio = self._session.stop_recording()
            except Exception as error:
                self._state = "IDLE"
                self._publish_error()
                return {"ok": False, "state": "IDLE", "error": str(error)}
            target = self._session.capture_delivery_target()
            self._state = "THINKING"
        self._publish_state(OverlayState.THINKING)

        try:
            result = self._session.process_recording(pcm_audio, target)
            if result.detail is None:
                self._publish_state(OverlayState.IDLE)
            else:
                self._publish_error()
            response: dict[str, object] = {
                "ok": True,
                "state": result.state,
                "text": result.text,
                "delivered": result.delivered,
            }
            if result.detail is not None:
                response["detail"] = result.detail
            return response
        except Exception as error:
            self._publish_error()
            return {"ok": False, "state": "IDLE", "error": str(error)}
        finally:
            with self._lock:
                self._state = "IDLE"

    @staticmethod
    def _create_overlay(config: MurmlyConfig) -> OverlayLifecycle:
        if not config.overlay_enabled:
            return NullOverlayController()
        backend = detect_overlay_backend()
        if backend is None:
            return NullOverlayController("Overlay requires KDE Plasma on X11 or Wayland.")
        return OverlayController(
            bottom_margin_px=config.overlay_bottom_margin_px,
            reduced_motion=config.overlay_reduced_motion,
            backend=backend,
        )

    def _publish_state(self, state: OverlayState) -> None:
        try:
            self._overlay.publish_state(state)
        except Exception as error:
            logger.warning("Overlay state update failed: %s", error)

    def _publish_level(self, level: float) -> None:
        try:
            self._overlay.publish_level(level)
        except Exception as error:
            logger.warning("Overlay level update failed: %s", error)

    def _publish_error(self) -> None:
        try:
            self._overlay.publish_error()
        except Exception as error:
            logger.warning("Overlay error update failed: %s", error)

    def _close_overlay(self) -> None:
        try:
            self._overlay.close()
        except Exception as error:
            logger.warning("Overlay shutdown failed: %s", error)


def send_command(socket_path: str, command: str) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            payload += chunk
    if not payload:
        raise RuntimeError("Murmly daemon closed the connection before responding.")
    return json.loads(payload.decode("utf-8"))
