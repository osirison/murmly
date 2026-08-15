from __future__ import annotations

from dataclasses import dataclass
import json
import socket
import threading

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig
from murmly.integrations import ClipboardPaster
from murmly.stt import FasterWhisperTranscriber


@dataclass(slots=True)
class ProcessingResult:
    text: str
    state: str


class SpeechSession:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._recorder = SoundDeviceRecorder(config)
        self._transcriber = FasterWhisperTranscriber(config)
        self._paster: ClipboardPaster | None = None

    def start_recording(self) -> None:
        self._recorder.start()

    def stop_and_process(self) -> ProcessingResult:
        pcm_audio = self._recorder.stop()
        text = self._transcriber.transcribe_pcm16(pcm_audio, self._recorder.sample_rate_hz)
        if text:
            self._ensure_paster().copy_and_paste(text)
        return ProcessingResult(text=text, state="DONE")

    def _ensure_paster(self) -> ClipboardPaster:
        if self._paster is None:
            self._paster = ClipboardPaster(
                restore_clipboard=self._config.restore_clipboard,
                restore_delay_ms=self._config.restore_clipboard_delay_ms,
            )
        return self._paster


class MurmlyDaemon:
    def __init__(self, config: MurmlyConfig, session: SpeechSession | None = None) -> None:
        self._config = config
        self._session = session or SpeechSession(config)
        self._state = "IDLE"
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._server: socket.socket | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def serve_forever(self) -> None:
        self._config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.socket_path.unlink(missing_ok=True)
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
                with connection:
                    response = self._handle_connection(connection)
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
        self._config.socket_path.unlink(missing_ok=True)

    def shutdown(self) -> None:
        self._shutdown.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.connect(str(self._config.socket_path))
        except OSError:
            pass

    def _handle_connection(self, connection: socket.socket) -> dict[str, object]:
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            payload += chunk
        command = json.loads(payload.decode("utf-8") or "{}")
        return self.handle_command(str(command.get("command", "")))

    def handle_command(self, command: str) -> dict[str, object]:
        if command == "status":
            return {"ok": True, "state": self.state}
        if command != "toggle":
            return {"ok": False, "error": f"Unsupported command: {command}"}

        with self._lock:
            if self._state == "IDLE":
                self._session.start_recording()
                self._state = "LISTENING"
                return {"ok": True, "state": self._state}
            if self._state != "LISTENING":
                return {"ok": False, "state": self._state, "error": "Daemon is busy."}
            self._state = "THINKING"

        try:
            result = self._session.stop_and_process()
            return {"ok": True, "state": result.state, "text": result.text}
        except Exception as error:
            return {"ok": False, "state": "IDLE", "error": str(error)}
        finally:
            with self._lock:
                self._state = "IDLE"


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
    return json.loads(payload.decode("utf-8"))
