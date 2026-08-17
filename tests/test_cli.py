from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from murmly.cli import _run_doctor, delivery_diagnostics, overlay_diagnostics
from murmly.config import MurmlyConfig
from murmly.stt import FasterWhisperTranscriber


class CliTests(unittest.TestCase):
    def test_daemon_exits_cleanly_on_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n\n[overlay]\nenabled = false\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            process = subprocess.Popen(
                [sys.executable, "-m", "murmly", "--config", str(config_path), "daemon"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not socket_path.exists():
                    if process.poll() is not None:
                        self.fail(f"daemon exited before creating its socket: {process.stderr.read()}")
                    if time.monotonic() >= deadline:
                        self.fail("daemon socket was not created")
                    time.sleep(0.01)

                process.send_signal(signal.SIGTERM)
                _stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

        self.assertEqual(0, process.returncode, stderr)
        self.assertFalse(socket_path.exists())

    def test_doctor_reports_effective_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with (
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cuda", "float16"),
                ),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch("murmly.cli.choose_paste_command", return_value=["xdotool"]),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertEqual("auto", report["device"])
        self.assertEqual("auto", report["compute_type"])
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])

    def test_overlay_diagnostics_reports_available_plasma_x11_runtime(self) -> None:
        config = self._config()
        completed = self._helper_result(
            {
                "available": True,
                "pygobject": True,
                "gtk4": "4.22.4",
                "gdk_x11": True,
                "native_x11": True,
                "gtk4_layer_shell": False,
            }
        )

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "x11",
                "DISPLAY": ":0",
                "XDG_CURRENT_DESKTOP": "KDE",
            },
            run_command=lambda *_args, **_kwargs: completed,
        )

        self.assertTrue(report["available"])
        self.assertTrue(report["supported_session"])
        self.assertEqual("x11", report["backend"])
        self.assertTrue(report["gdk_x11"])
        self.assertTrue(report["native_x11"])
        self.assertEqual("4.22.4", report["gtk4"])

    def test_overlay_diagnostics_reports_partial_install(self) -> None:
        config = self._config()
        completed = self._helper_result(
            {
                "available": False,
                "pygobject": True,
                "gtk4": "4.22.4",
                "gdk_x11": False,
                "native_x11": False,
                "gtk4_layer_shell": False,
                "error": "Gtk4LayerShell namespace not available",
            },
            returncode=1,
        )

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "KDE",
            },
            run_command=lambda *_args, **_kwargs: completed,
        )

        self.assertFalse(report["available"])
        self.assertEqual("wayland", report["backend"])
        self.assertTrue(report["pygobject"])
        self.assertFalse(report["gtk4_layer_shell"])
        self.assertIn("Gtk4LayerShell", report["detail"])

    def test_overlay_diagnostics_rejects_unsupported_session(self) -> None:
        config = self._config()
        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
            run_command=lambda *_args, **_kwargs: self.fail("helper should not run"),
        )

        self.assertFalse(report["available"])
        self.assertFalse(report["supported_session"])
        self.assertIsNone(report["backend"])
        self.assertIn("KDE Plasma", report["detail"])

    def test_overlay_diagnostics_reports_disabled_overlay_on_unsupported_session(self) -> None:
        config = self._config(overlay_enabled=False)
        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
            run_command=lambda *_args, **_kwargs: self.fail("helper should not run"),
        )

        self.assertFalse(report["enabled"])
        self.assertFalse(report["available"])
        self.assertFalse(report["supported_session"])
        self.assertEqual("Overlay is disabled in configuration.", report["detail"])

    def test_overlay_diagnostics_handles_helper_failure(self) -> None:
        config = self._config()

        def failed_helper(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise OSError("system interpreter missing")

        report = overlay_diagnostics(
            config,
            env={"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "KDE", "DISPLAY": ":0"},
            run_command=failed_helper,
        )

        self.assertFalse(report["available"])
        self.assertIn("system interpreter missing", report["detail"])

    def _run_spike(self, *, paste: bool, focus_changed: bool, verify_target: bool = True):
        from murmly.focus import WindowIdentity

        target = WindowIdentity(1, 10, "editor")
        current = WindowIdentity(2, 20, "browser") if focus_changed else target

        class Observer:
            supported = True
            detail = None

            def active_window(self):
                return Observer.window

        Observer.window = target
        config = MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            verify_target=verify_target,
        )
        with (
            patch("murmly.cli.SoundDeviceRecorder") as recorder,
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.ClipboardPaster") as paster,
            patch("murmly.cli.create_focus_observer", return_value=Observer()),
            redirect_stdout(StringIO()),
        ):
            recorder.return_value.record_for_seconds.return_value = b"pcm"
            recorder.return_value.sample_rate_hz = 16_000

            def transcribe_while_focus_moves(*_args, **_kwargs):
                # Focus moves during transcription, after the target was recorded.
                Observer.window = current
                return "hello world"

            transcriber.return_value.transcribe_pcm16.side_effect = transcribe_while_focus_moves
            from murmly.cli import _run_spike

            _run_spike(config, 1.0, paste)
        return paster.return_value

    def test_spike_pastes_when_focus_is_unchanged(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=False)

        paster.copy_and_paste.assert_called_once_with("hello world")
        paster.copy.assert_not_called()

    def test_spike_copies_without_pasting_when_focus_changed(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=True)

        paster.copy.assert_called_once_with("hello world")
        paster.copy_and_paste.assert_not_called()

    def test_spike_with_verification_disabled_pastes_despite_focus_change(self) -> None:
        paster = self._run_spike(paste=True, focus_changed=True, verify_target=False)

        paster.copy_and_paste.assert_called_once_with("hello world")

    def test_spike_without_paste_flag_only_copies(self) -> None:
        paster = self._run_spike(paste=False, focus_changed=False)

        paster.copy.assert_called_once_with("hello world")
        paster.copy_and_paste.assert_not_called()

    class _Focus:
        def __init__(self, supported: bool, detail: str | None = None) -> None:
            self.supported = supported
            self.detail = detail

        def active_window(self):
            return None

    def test_delivery_diagnostics_reports_supported_and_enabled(self) -> None:
        report = delivery_diagnostics(self._config(), observer=self._Focus(True))

        self.assertTrue(report["verification_supported"])
        self.assertTrue(report["verification_enabled"])
        self.assertEqual(500, report["restore_delay_ms"])
        self.assertIn("supported and enabled", report["detail"])

    def test_delivery_diagnostics_reports_supported_but_disabled(self) -> None:
        config = MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            verify_target=False,
        )

        report = delivery_diagnostics(config, observer=self._Focus(True))

        self.assertTrue(report["verification_supported"])
        self.assertFalse(report["verification_enabled"])
        self.assertIn("disabled in configuration", report["detail"])

    def test_delivery_diagnostics_reports_unverified_session(self) -> None:
        observer = self._Focus(False, "Delivery target verification requires an X11 session.")

        report = delivery_diagnostics(self._config(), observer=observer)

        self.assertFalse(report["verification_supported"])
        self.assertIn("X11 session", report["detail"])

    def test_doctor_includes_delivery_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch("murmly.cli.choose_paste_command", return_value=["xdotool"]),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIn("delivery", report)
        self.assertIn("verification_supported", report["delivery"])
        self.assertEqual(500, report["delivery"]["restore_delay_ms"])

    @staticmethod
    def _config(overlay_enabled: bool = True) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=Path("/tmp/murmly.sock"),
            config_path=Path("/tmp/config.toml"),
            overlay_enabled=overlay_enabled,
        )

    @staticmethod
    def _helper_result(report: dict[str, object], returncode: int = 0) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["/usr/bin/python3", "overlay_renderer.py", "--check"],
            returncode=returncode,
            stdout=json.dumps(report),
            stderr="",
        )

class StubService:
    """A stand-in for the installed systemd user unit."""

    def __init__(self, installed: bool = True, on_start=None) -> None:
        self.is_installed = installed
        self.starts = 0
        self._on_start = on_start

    def start(self) -> bool:
        self.starts += 1
        if self._on_start is not None:
            self._on_start()
        return True


class CountingSender:
    """Counts send_command attempts and replays scripted outcomes."""

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.attempts = 0

    def __call__(self, _socket_path, command):
        self.attempts += 1
        outcome = self._outcomes.pop(0) if self._outcomes else self._outcomes
        if isinstance(outcome, Exception):
            raise outcome
        return {"ok": True, "state": "LISTENING", "command": command}


class ToggleRecoveryTests(unittest.TestCase):
    def _config(self, socket_path: Path) -> MurmlyConfig:
        return MurmlyConfig(socket_path=socket_path, config_path=Path("/tmp/config.toml"))

    def test_running_daemon_is_used_without_touching_the_service(self) -> None:
        from murmly.cli import send_command_with_recovery

        service = StubService()
        sender = CountingSender([None])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                response = send_command_with_recovery(config, "toggle", service=service)

        self.assertTrue(response["ok"])
        self.assertEqual(1, sender.attempts)
        self.assertEqual(0, service.starts, "a healthy daemon must not be restarted")

    def test_installed_but_not_listening_starts_the_service_and_retries_once(self) -> None:
        from murmly.cli import send_command_with_recovery

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = self._config(socket_path)

            def create_socket() -> None:
                socket_path.write_text("", encoding="utf-8")

            service = StubService(installed=True, on_start=create_socket)
            sender = CountingSender([FileNotFoundError("no socket"), None])

            with patch("murmly.cli.send_command", sender):
                response = send_command_with_recovery(
                    config, "toggle", service=service, sleep=lambda _s: None
                )

        self.assertTrue(response["ok"])
        self.assertEqual(1, service.starts)
        self.assertEqual(2, sender.attempts, "exactly one retry after the initial attempt")

    def test_not_installed_names_the_install_command(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        service = StubService(installed=False)
        sender = CountingSender([FileNotFoundError("no socket")])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(config, "toggle", service=service)

        self.assertIn("murmly install", str(raised.exception))
        self.assertEqual(0, service.starts)
        self.assertEqual(1, sender.attempts, "no retry when nothing can be started")

    def test_service_that_never_comes_up_fails_within_the_bound(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        service = StubService(installed=True)
        sender = CountingSender([FileNotFoundError("no socket")])
        ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(
                        config,
                        "toggle",
                        service=service,
                        sleep=lambda _s: None,
                        clock=lambda: next(ticks),
                        timeout=1.0,
                    )

        self.assertIn("did not start", str(raised.exception))
        self.assertEqual(1, service.starts)
        self.assertEqual(1, sender.attempts, "the command is not retried when the socket never appears")

    def test_daemon_that_starts_but_refuses_the_command_is_reported(self) -> None:
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery

        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config = self._config(socket_path)
            socket_path.write_text("", encoding="utf-8")
            service = StubService(installed=True)
            sender = CountingSender([ConnectionRefusedError("refused"), ConnectionRefusedError("refused")])

            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(config, "toggle", service=service, sleep=lambda _s: None)

        self.assertIn("did not accept", str(raised.exception))
        self.assertEqual(2, sender.attempts)

    def test_client_command_reports_the_failure_without_a_traceback(self) -> None:
        from murmly.cli import _run_client_command

        service = StubService(installed=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with (
                patch("murmly.cli.send_command", CountingSender([FileNotFoundError("no socket")])),
                patch("murmly.cli.UserService", return_value=service),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = _run_client_command(config, "toggle")

        self.assertEqual(1, exit_code)
        self.assertIn("murmly install", errors.getvalue())


class InstallCommandTests(unittest.TestCase):
    def test_rejects_an_unparseable_hotkey_before_touching_anything(self) -> None:
        from murmly.cli import _run_install

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            exit_code = _run_install("Meta+Frobnicate")

        self.assertEqual(2, exit_code)
        self.assertIn("Frobnicate", errors.getvalue())
        installer.assert_not_called()

    def test_prints_outcome_messages_on_success(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import InstallOutcome

        outcome = InstallOutcome(
            entrypoint=Path("/bin/murmly"),
            hotkey=None,
            service_installed=True,
            hotkey_registered=True,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=("Registered Meta+X.", "Press it once to confirm."),
        )
        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stdout(StringIO()) as output,
        ):
            installer.return_value.install.return_value = outcome
            exit_code = _run_install("Meta+X")

        self.assertEqual(0, exit_code)
        self.assertIn("Registered Meta+X.", output.getvalue())

    def test_conflict_exits_non_zero_naming_the_owner(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import HotkeyConflictError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.install.side_effect = HotkeyConflictError("Meta+X is used by Klipper.")
            exit_code = _run_install("Meta+X")

        self.assertEqual(1, exit_code)
        self.assertIn("Klipper", errors.getvalue())

    def test_unconfirmed_binding_reports_next_login_and_fails(self) -> None:
        from murmly.cli import _run_install
        from murmly.installer import HotkeyNotConfirmedError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.install.side_effect = HotkeyNotConfirmedError("timed out")
            exit_code = _run_install("Meta+X")

        self.assertEqual(1, exit_code)
        self.assertIn("next login", errors.getvalue())
        self.assertIn("not active in this session", errors.getvalue())


class UninstallCommandTests(unittest.TestCase):
    def test_prints_what_was_removed(self) -> None:
        from murmly.cli import _run_uninstall
        from murmly.installer import InstallOutcome

        outcome = InstallOutcome(
            entrypoint=None,
            hotkey=None,
            service_installed=False,
            hotkey_registered=False,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=("Removed the Murmly service.", "Released the Murmly hotkey."),
        )
        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stdout(StringIO()) as output,
        ):
            installer.return_value.uninstall.return_value = outcome
            exit_code = _run_uninstall()

        self.assertEqual(0, exit_code)
        self.assertIn("Removed the Murmly service.", output.getvalue())

    def test_reports_a_failure_without_a_traceback(self) -> None:
        from murmly.cli import _run_uninstall
        from murmly.installer import InstallError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.uninstall.side_effect = InstallError("systemctl unavailable")
            exit_code = _run_uninstall()

        self.assertEqual(1, exit_code)
        self.assertIn("systemctl unavailable", errors.getvalue())


class InstallationDiagnosticsTests(unittest.TestCase):
    def test_reports_the_installer_status(self) -> None:
        from murmly.cli import installation_diagnostics

        class Stub:
            def status(self):
                return {"installed": True, "hotkey": "Meta+X", "hotkey_held": True}

        report = installation_diagnostics(Stub())

        self.assertTrue(report["installed"])
        self.assertEqual("Meta+X", report["hotkey"])

    def test_never_raises_when_the_desktop_is_unreachable(self) -> None:
        from murmly.cli import installation_diagnostics

        class Broken:
            def status(self):
                raise RuntimeError("bus is down")

        report = installation_diagnostics(Broken())

        self.assertFalse(report["installed"])
        self.assertIn("bus is down", report["detail"])

    def test_doctor_includes_the_installation_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch("murmly.cli.choose_paste_command", return_value=["xdotool"]),
                patch(
                    "murmly.cli.installation_diagnostics",
                    return_value={"installed": False, "detail": "not installed"},
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIn("installation", report)
        self.assertFalse(report["installation"]["installed"])
