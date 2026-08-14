from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from murmly.config import MurmlyConfig
from murmly.daemon import MurmlyDaemon, ProcessingResult, send_command


class DummySession:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start_recording(self) -> None:
        self.started += 1

    def stop_and_process(self) -> ProcessingResult:
        self.stopped += 1
        return ProcessingResult(text="hello world", state="DONE")


class DaemonTests(unittest.TestCase):
    def test_daemon_initializes_without_clipboard_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            daemon = MurmlyDaemon(config)

        self.assertEqual("IDLE", daemon.state)

    def test_socket_protocol_toggles_between_idle_and_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = MurmlyConfig(socket_path=socket_path, config_path=Path(temp_dir) / "config.toml")
            session = DummySession()
            daemon = MurmlyDaemon(config, session=session)
            thread = threading.Thread(target=daemon.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(daemon.shutdown)
            self.addCleanup(thread.join, 2)

            deadline = time.time() + 2
            while not socket_path.exists():
                if time.time() >= deadline:
                    self.fail("daemon socket was not created")
                time.sleep(0.05)

            status = send_command(str(socket_path), "status")
            first = send_command(str(socket_path), "toggle")
            second = send_command(str(socket_path), "toggle")

        self.assertEqual({"ok": True, "state": "IDLE"}, status)
        self.assertEqual("LISTENING", first["state"])
        self.assertEqual("DONE", second["state"])
        self.assertEqual("hello world", second["text"])
        self.assertEqual(1, session.started)
        self.assertEqual(1, session.stopped)
