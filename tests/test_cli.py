from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from murmly.cli import (
    _measurement_clip,
    _run_daemon,
    _run_doctor,
    leave_without_finalizing,
    command_socket_diagnostics,
    delivery_diagnostics,
    live_transcription_diagnostics,
    main,
    measure_partial_pass_ms,
    overlay_diagnostics,
    paste_injection_diagnostics,
)
from murmly.config import MurmlyConfig
from murmly.daemon import DaemonStartupError, MurmlyDaemon
from murmly.integrations import PasteInjection
from murmly.overlay import OverlayBackend, renderer_environment
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
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertEqual("auto", report["device"])
        self.assertEqual("auto", report["compute_type"])
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])

    def test_paste_injection_diagnostics_names_the_method_when_it_can_inject(self) -> None:
        report = paste_injection_diagnostics(
            PasteInjection("ydotool", ("ydotool", "key", "29:1", "47:1", "47:0", "29:0"))
        )

        self.assertTrue(report["available"])
        self.assertEqual("ydotool", report["method"])
        self.assertEqual(["ydotool", "key", "29:1", "47:1", "47:0", "29:0"], report["command"])
        self.assertNotIn("remedy", report)

    def test_paste_injection_diagnostics_separates_absent_from_unusable(self) -> None:
        absent = paste_injection_diagnostics(
            PasteInjection(
                None,
                None,
                reason="No Wayland paste injector is installed; install wtype or ydotool.",
                remedy=("sudo dnf install ydotool",),
            )
        )
        unusable = paste_injection_diagnostics(
            PasteInjection(
                None,
                None,
                reason="wtype is installed but cannot inject in this session: no virtual keyboard",
                remedy=("sudo dnf install ydotool",),
            )
        )

        self.assertFalse(absent["available"])
        self.assertIn("is installed", absent["reason"])
        self.assertEqual(["sudo dnf install ydotool"], absent["remedy"])
        self.assertFalse(unusable["available"])
        self.assertIn("cannot inject in this session", unusable["reason"])
        self.assertEqual(["sudo dnf install ydotool"], unusable["remedy"])

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

    def test_overlay_diagnostics_checks_under_the_renderer_environment(self) -> None:
        config = self._config()
        completed = self._helper_result({"available": True, "gtk4_layer_shell": True})
        recorded: dict[str, object] = {}

        def record(*arguments: object, **keywords: object) -> object:
            recorded.update(keywords)
            return completed

        report = overlay_diagnostics(
            config,
            env={
                "XDG_SESSION_TYPE": "wayland",
                "WAYLAND_DISPLAY": "wayland-0",
                "XDG_CURRENT_DESKTOP": "KDE",
                "LD_PRELOAD": "/tmp/injected.so",
            },
            run_command=record,
        )

        self.assertTrue(report["available"])
        # Whatever the renderer will be launched with, down to the preload that
        # decides whether Layer Shell works at all.
        self.assertEqual(
            renderer_environment(
                OverlayBackend.WAYLAND,
                {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"},
            ),
            recorded["env"],
        )

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
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
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

    def test_a_daemon_that_answers_nothing_is_reported_without_a_restart(self) -> None:
        # Something is listening, so starting the service would answer a question
        # the caller did not ask.
        from murmly.cli import DaemonUnavailableError, send_command_with_recovery
        from murmly.daemon import DaemonNotRespondingError

        service = StubService(installed=True)
        sender = CountingSender([DaemonNotRespondingError("nothing came back")])
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(Path(temp_dir) / "murmly.sock")
            with patch("murmly.cli.send_command", sender):
                with self.assertRaises(DaemonUnavailableError) as raised:
                    send_command_with_recovery(config, "toggle", service=service)

        self.assertIn("nothing came back", str(raised.exception))
        self.assertEqual(0, service.starts, "a daemon that answered must not be restarted")
        self.assertEqual(1, sender.attempts, "no retry when the connection was accepted")

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

    def test_only_the_hotkey_that_was_not_confirmed_is_named(self) -> None:
        """The other one is bound, confirmed and working right now.

        An install of two hotkeys can fail on either, and naming both tells the
        person that a key which works is not active.
        """
        from murmly.cli import _run_install
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyNotConfirmedError

        with (
            patch("murmly.cli.Installer") as installer,
            redirect_stderr(StringIO()) as errors,
        ):
            installer.return_value.install.side_effect = HotkeyNotConfirmedError(
                "The desktop did not register Meta+A within 5 seconds.",
                parse_hotkey("Meta+A"),
            )
            exit_code = _run_install("Meta+X", "Meta+A")

        self.assertEqual(1, exit_code)
        report = errors.getvalue()
        self.assertIn("Meta+A is not active in this session", report)
        self.assertNotIn("Meta+X is not active", report)
        self.assertNotIn("Meta+X, Meta+A", report)


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
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
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


class LiveTranscriptionDiagnosticsTests(unittest.TestCase):
    def _config(self, **overrides: object) -> MurmlyConfig:
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            **overrides,
        )

    def test_defaults_report_both_features_off_without_loading_anything(self) -> None:
        with patch("murmly.cli.SilenceDetector") as detector, patch(
            "murmly.cli.FasterWhisperTranscriber"
        ) as transcriber:
            report = live_transcription_diagnostics(self._config())

        self.assertFalse(report["live_transcribe"])
        self.assertEqual("off", report["auto_transcribe"])
        self.assertIsNone(report["partial_pass_ceiling_ms"])
        self.assertEqual("Auto-transcribe is disabled.", report["silence_detection_detail"])
        # Neither the VAD nor the model may be loaded for a disabled feature.
        detector.assert_not_called()
        transcriber.assert_not_called()

    def test_a_rejected_mode_is_surfaced(self) -> None:
        config = self._config(auto_transcribe="off", auto_transcribe_rejected_value="whenever")

        with patch("murmly.cli.SilenceDetector"):
            report = live_transcription_diagnostics(config)

        self.assertEqual("whenever", report["auto_transcribe_rejected_value"])

    def test_silence_is_checked_against_the_negotiated_rate(self) -> None:
        config = self._config(auto_transcribe="stop", sample_rate_hz=16_000)

        with (
            patch("murmly.cli.negotiated_capture_rate", return_value=(44_100, None)),
            patch("murmly.cli.SilenceDetector") as detector,
        ):
            detector.return_value.available = False
            detector.return_value.unavailable_reason = "capture rate 44100 Hz is not supported"
            report = live_transcription_diagnostics(config)

        self.assertEqual(44_100, report["negotiated_sample_rate_hz"])
        self.assertFalse(report["silence_detection_available"])
        self.assertEqual(44_100, detector.call_args.args[0])

    def test_keeps_pace_compares_the_ceiling_against_the_interval(self) -> None:
        config = self._config(live_transcribe=True, live_interval_ms=1_000)

        for measured, expected in ((300, True), (12_000, False)):
            with self.subTest(measured=measured):
                with (
                    patch("murmly.cli.SilenceDetector"),
                    patch("murmly.cli.measure_partial_pass_ms", return_value=(measured, None)),
                ):
                    report = live_transcription_diagnostics(config)

                self.assertEqual(measured, report["partial_pass_ceiling_ms"])
                self.assertEqual(expected, report["partial_pass_keeps_pace"])

    def test_a_failed_measurement_reports_detail_and_no_verdict(self) -> None:
        config = self._config(live_transcribe=True)

        with (
            patch("murmly.cli.SilenceDetector"),
            patch("murmly.cli.measure_partial_pass_ms", return_value=(None, "model exploded")),
        ):
            report = live_transcription_diagnostics(config)

        self.assertIsNone(report["partial_pass_ceiling_ms"])
        self.assertEqual("model exploded", report["partial_pass_detail"])
        self.assertNotIn("partial_pass_keeps_pace", report)

    def test_measurement_clip_is_never_digital_silence(self) -> None:
        """A silent clip short-circuits before the model runs.

        The measurement would then report None forever without anything failing.
        """
        clip = _measurement_clip(16_000, 15)

        self.assertTrue(any(clip))
        self.assertEqual(16_000 * 15 * 2, len(clip))

    def test_measurement_refuses_a_transcriber_with_the_vad_filter_on(self) -> None:
        """With the filter on, the synthetic clip is discarded as non-speech.

        The pass would then time no decoding and report a fast ceiling on a
        machine that cannot keep pace at all.
        """
        config = self._config(live_transcribe=True, vad_filter=True)
        transcriber = FasterWhisperTranscriber(config)

        measured, detail = measure_partial_pass_ms(config, transcriber)

        self.assertIsNone(measured)
        self.assertIn("voice activity filter", detail)

    def test_measurement_uses_a_vad_free_config_when_it_builds_its_own(self) -> None:
        config = self._config(live_transcribe=True, vad_filter=True)

        with patch("murmly.cli.FasterWhisperTranscriber") as factory:
            factory.return_value.partials_available = True
            factory.return_value.transcribe_partial.return_value = "text"
            measured, detail = measure_partial_pass_ms(config)

        self.assertIsNone(detail)
        self.assertIsInstance(measured, int)
        self.assertFalse(factory.call_args.args[0].vad_filter)


class UnhandledFailureTests(unittest.TestCase):
    """No Murmly command terminates with an unhandled error."""

    def test_a_daemon_that_answers_nothing_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            socket_path = Path(temp_dir) / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n', encoding="utf-8"
            )
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(str(socket_path))
            server.listen()
            stop = threading.Event()

            def accept_and_close() -> None:
                server.settimeout(0.2)
                while not stop.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1)
                        try:
                            connection.recv(4_096)
                        except OSError:
                            pass

            thread = threading.Thread(target=accept_and_close, daemon=True)
            thread.start()
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                    exit_code = main(["--config", str(config_path), "status"])
            finally:
                stop.set()
                thread.join(timeout=3)

        self.assertEqual(1, exit_code)
        self.assertIn("closed the connection before responding", errors.getvalue())

    def test_a_configuration_that_cannot_be_read_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[daemon\nsocket_path = \n", encoding="utf-8")

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
                exit_code = main(["--config", str(config_path), "doctor"])

        self.assertEqual(1, exit_code)
        self.assertIn(str(config_path), errors.getvalue())
        self.assertIn("Unable to read the configuration", errors.getvalue())

    def test_a_daemon_that_refuses_to_start_is_reported_and_exits_non_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "shared"
            directory.mkdir()
            directory.chmod(0o777)
            socket_path = directory / "murmly.sock"
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{socket_path}"\n\n[overlay]\nenabled = false\n',
                encoding="utf-8",
            )

            with (
                # The real one unregisters sounddevice's atexit hook process-wide,
                # and that hook is the only caller of Pa_Terminate. Leaving it off
                # means PortAudio's PipeWire loop threads outlive the interpreter,
                # and Py_Finalize unloads the libraries underneath them - the whole
                # suite then passes and the process exits 139. See issue #27.
                patch("murmly.cli.disable_portaudio_exit_teardown"),
                # The daemon branch ends in os._exit; performing that here would
                # take the test runner with it.
                patch("murmly.cli.leave_without_finalizing"),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "daemon"])

        self.assertEqual(1, exit_code)
        self.assertIn(str(socket_path), errors.getvalue())
        self.assertIn("XDG_RUNTIME_DIR", errors.getvalue())

    def test_the_top_level_guard_reports_an_unexpected_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("", encoding="utf-8")

            with (
                patch("murmly.cli._run_doctor", side_effect=RuntimeError("probe exploded")),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "doctor"])

        reported = errors.getvalue()
        self.assertEqual(1, exit_code)
        self.assertIn("probe exploded", reported)
        # One line for a person at a terminal. The frames go to the daemon only.
        self.assertNotIn("Traceback", reported)

    def test_the_daemon_guard_keeps_the_traceback_its_journal_needs(self) -> None:
        # The daemon runs unattended, so the one line the guard prints is all
        # that would survive of a crash nothing anticipated. Whoever reads the
        # journal afterwards has nothing else to work from.
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("", encoding="utf-8")

            with (
                patch("murmly.cli._run_daemon", side_effect=RuntimeError("worker exploded")),
                patch("murmly.cli.leave_without_finalizing"),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "daemon"])

        reported = errors.getvalue()
        self.assertEqual(1, exit_code)
        self.assertIn("murmly: unexpected failure: worker exploded", reported)
        self.assertIn("Traceback (most recent call last)", reported)

    def test_argument_errors_still_reach_the_parser(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main(["not-a-command"])


class DoctorCompletenessTests(unittest.TestCase):
    """`murmly doctor` reports every section it can and explains the ones it cannot."""

    SECTIONS = (
        "config_path",
        "socket_path",
        "command_socket",
        "session",
        "clipboard_command",
        "paste_injection",
        "model_profile",
        "model_name",
        "device",
        "compute_type",
        "runtime_device",
        "runtime_compute_type",
        "beam_size",
        "vad_filter",
        "live_transcription",
        "delivery",
        "overlay",
        "speech_output",
        "installation",
    )

    def _report(self, config: MurmlyConfig, resolve_runtime: object) -> dict[str, object]:
        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", resolve_runtime),
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config)
        return json.loads(output.getvalue())

    def test_the_report_is_complete_when_the_configured_runtime_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                device="cuda",
            )
            report = self._report(
                config,
                Mock(side_effect=RuntimeError("the cuda extra is not installed")),
            )

        self.assertEqual("cuda", report["device"])
        self.assertIsNone(report["runtime_device"])
        self.assertIsNone(report["runtime_compute_type"])
        self.assertIn("the cuda extra is not installed", report["runtime_detail"])
        for section in self.SECTIONS:
            self.assertIn(section, report)

    def test_the_success_shape_of_every_section_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            report = self._report(config, Mock(return_value=("cuda", "float16")))

        self.assertEqual(set(self.SECTIONS), set(report))
        self.assertEqual("cuda", report["runtime_device"])
        self.assertEqual("float16", report["runtime_compute_type"])
        self.assertIn(report["session"], {"wayland", "x11"})
        self.assertEqual(
            {"available", "method", "command", "confirms_delivery"},
            set(report["paste_injection"]),
        )
        self.assertEqual(
            {
                "verification_supported",
                "verification_enabled",
                "restore_clipboard",
                "restore_delay_ms",
                "detail",
            },
            set(report["delivery"]),
        )
        self.assertEqual(
            {"path", "path_private", "peer_identity_supported"},
            set(report["command_socket"]),
        )
        self.assertTrue(report["command_socket"]["path_private"])

    def test_diagnostics_report_a_socket_path_the_daemon_would_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "shared"
            directory.mkdir()
            directory.chmod(0o777)
            config = MurmlyConfig(
                socket_path=directory / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
                overlay_enabled=False,
            )
            # The same path the daemon refuses, so the two cannot drift apart.
            with self.assertRaises(DaemonStartupError):
                MurmlyDaemon(config)

            report = self._report(config, Mock(return_value=("cpu", "int8")))

        self.assertFalse(report["command_socket"]["path_private"])
        self.assertIn(str(directory), report["command_socket"]["detail"])
        for section in self.SECTIONS:
            self.assertIn(section, report)

    def test_diagnostics_explain_a_platform_that_cannot_report_peer_identity(self) -> None:
        # The section exists to say what is protecting the socket. Where the
        # identity check is unavailable, file permissions are the whole answer
        # and the report has to say so.
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with patch("murmly.cli.peer_identity_supported", return_value=False):
                report = command_socket_diagnostics(config)

        self.assertFalse(report["peer_identity_supported"])
        self.assertIn("file permissions alone", report["peer_identity_detail"])

    def test_a_private_socket_path_is_reported_as_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            report = command_socket_diagnostics(config)

        self.assertTrue(report["path_private"])
        self.assertNotIn("detail", report)
        self.assertTrue(report["peer_identity_supported"])

    def test_an_unreadable_focus_probe_does_not_abandon_the_delivery_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            with patch(
                "murmly.cli.create_focus_observer",
                side_effect=RuntimeError("the display went away"),
            ):
                report = delivery_diagnostics(config)

        self.assertFalse(report["verification_supported"])
        self.assertIn("the display went away", report["detail"])


class SecondHotkeyCommandTests(unittest.TestCase):
    """The CLI surface the second hotkey needs."""

    @staticmethod
    def _outcome_type():
        from murmly.installer import InstallOutcome

        return InstallOutcome

    def test_install_accepts_a_second_hotkey_and_passes_it_on(self) -> None:
        from murmly.cli import _run_install

        captured: list[tuple[str, str | None]] = []

        outcome_type = self._outcome_type()

        class FakeInstaller:
            def install(self, hotkey, session_hotkey=None):
                captured.append(
                    (hotkey.portable, session_hotkey.portable if session_hotkey else None)
                )
                return outcome_type(
                    entrypoint=Path("/bin/murmly"),
                    hotkey=hotkey,
                    service_installed=True,
                    hotkey_registered=True,
                    already_bound=False,
                    session_supported=True,
                    session_verified=True,
                    user_override=None,
                    messages=("Registered both.",),
                    session_hotkey=session_hotkey,
                    session_hotkey_registered=session_hotkey is not None,
                )

        with patch("murmly.cli.Installer", FakeInstaller), redirect_stdout(StringIO()):
            self.assertEqual(0, _run_install("Meta+X", "Meta+A"))

        self.assertEqual([("Meta+X", "Meta+A")], captured)

    def test_install_without_a_second_hotkey_passes_none(self) -> None:
        from murmly.cli import _run_install

        captured: list[tuple[str, str | None]] = []

        outcome_type = self._outcome_type()

        class FakeInstaller:
            def install(self, hotkey, session_hotkey=None):
                captured.append(
                    (hotkey.portable, session_hotkey.portable if session_hotkey else None)
                )
                return outcome_type(
                    entrypoint=Path("/bin/murmly"),
                    hotkey=hotkey,
                    service_installed=True,
                    hotkey_registered=True,
                    already_bound=False,
                    session_supported=True,
                    session_verified=True,
                    user_override=None,
                    messages=(),
                )

        with patch("murmly.cli.Installer", FakeInstaller), redirect_stdout(StringIO()):
            self.assertEqual(0, _run_install("Meta+X"))

        self.assertEqual([("Meta+X", None)], captured)

    def test_an_unreadable_second_hotkey_is_refused_before_anything_is_installed(self) -> None:
        from murmly.cli import _run_install

        class RefusingInstaller:
            def install(self, hotkey, session_hotkey=None):
                raise AssertionError("install must not run on an unreadable hotkey")

        with patch("murmly.cli.Installer", RefusingInstaller), redirect_stderr(StringIO()):
            self.assertEqual(2, _run_install("Meta+X", "notakey"))

    def test_the_parser_accepts_the_session_toggle(self) -> None:
        from murmly.cli import build_parser

        arguments = build_parser().parse_args(["toggle-session"])

        self.assertEqual("toggle-session", arguments.command)

    def test_the_session_toggle_reaches_the_daemon_under_its_wire_name(self) -> None:
        """argparse spells it with a hyphen; the wire protocol does not."""
        from murmly.cli import DAEMON_COMMANDS, main

        sent: list[str] = []

        def fake_send(config, command):
            sent.append(command)
            return {"ok": True, "state": "LISTENING"}

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("")
            with (
                patch("murmly.cli.send_command_with_recovery", fake_send),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "toggle-session"])

        self.assertEqual(["toggle_session"], sent)
        self.assertEqual("toggle_session", DAEMON_COMMANDS["toggle-session"])

    def test_the_existing_toggle_is_sent_unchanged(self) -> None:
        from murmly.cli import main

        sent: list[str] = []

        def fake_send(config, command):
            sent.append(command)
            return {"ok": True, "state": "LISTENING"}

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("")
            with (
                patch("murmly.cli.send_command_with_recovery", fake_send),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "toggle"])

        self.assertEqual(["toggle"], sent)


class SpeechOutputDiagnosticsTests(unittest.TestCase):
    def _config(self, temp_dir: str, **overrides):
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
            tts_model_dir=Path(temp_dir) / "models",
            **overrides,
        )

    def test_disabled_speech_output_is_reported_as_disabled(self) -> None:
        from murmly.cli import speech_output_diagnostics

        with tempfile.TemporaryDirectory() as temp_dir:
            report = speech_output_diagnostics(self._config(temp_dir))

        self.assertFalse(report["enabled"])
        self.assertFalse(report["available"])
        self.assertIn("disabled", report["detail"])

    def test_an_unavailable_stack_names_what_to_install(self) -> None:
        from murmly.cli import speech_output_diagnostics
        from fakes import FakeSynthesizer

        probe = FakeSynthesizer(
            available=False,
            unavailable_reason="kokoro-onnx is not installed. Run `uv sync --extra tts`.",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report = speech_output_diagnostics(self._config(temp_dir, tts_enabled=True), probe)

        self.assertTrue(report["enabled"])
        self.assertFalse(report["available"])
        self.assertIn("--extra tts", report["detail"])

    def test_a_working_stack_names_the_voice_rate_and_output_device(self) -> None:
        from murmly.cli import speech_output_diagnostics
        from fakes import FakeSynthesizer

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True, tts_voice="bf_emma", tts_rate_percent=120)
            with patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Studio Speakers", None, None),
            ):
                report = speech_output_diagnostics(config, FakeSynthesizer())

        self.assertTrue(report["available"])
        self.assertEqual("bf_emma", report["voice"])
        self.assertEqual(120, report["rate_percent"])
        self.assertEqual(48_000, report["negotiated_output_rate_hz"])
        # The spec has the working report naming the device in use. The
        # configured value cannot stand in: it is empty by default, which is
        # the case a person with sound in the wrong place is looking at.
        self.assertEqual("Studio Speakers", report["output_device_in_use"])

    def test_a_configured_value_that_was_not_honoured_is_reported_alongside_the_one_in_use(
        self,
    ) -> None:
        from murmly.cli import speech_output_diagnostics
        from murmly.config import load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[tts]\nvoice = "morgan_freeman"\nrate = 900\n')
            report = speech_output_diagnostics(load_config(config_path))

        self.assertEqual("af_heart", report["voice"])
        self.assertEqual("morgan_freeman", report["voice_rejected_value"])
        self.assertEqual(100, report["rate_percent"])
        self.assertEqual(900, report["rate_rejected_value"])

    def test_a_rate_that_json_cannot_encode_still_produces_a_report(self) -> None:
        """`rate = 1979-05-27` is a TOML date, and the report is serialised.

        The rejected value was carried through raw, so one mistyped setting made
        `murmly doctor` print no report at all -- including every section that
        had nothing to do with it.
        """
        import json

        from murmly.cli import speech_output_diagnostics
        from murmly.config import load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[tts]\nrate = 1979-05-27\n")
            report = speech_output_diagnostics(load_config(config_path))

        self.assertEqual(100, report["rate_percent"])
        self.assertEqual("1979-05-27", report["rate_rejected_value"])
        json.dumps(report)

    def test_a_device_that_cannot_be_opened_is_named_in_the_report(self) -> None:
        from murmly.cli import speech_output_diagnostics
        from fakes import FakeSynthesizer

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True)
            with patch(
                "murmly.cli.negotiated_output",
                return_value=(None, None, None, "No output device could be opened: nothing there"),
            ):
                report = speech_output_diagnostics(config, FakeSynthesizer())

        self.assertIsNone(report["negotiated_output_rate_hz"])
        self.assertIn("No output device could be opened", report["detail"])
        # The daemon refuses every session for this same reason, so a report
        # calling speech available would contradict the next thing that happens.
        self.assertFalse(report["available"])

    def test_a_configured_device_that_was_not_used_names_what_was(self) -> None:
        from murmly.cli import speech_output_diagnostics
        from fakes import FakeSynthesizer

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True, tts_output_device="Missing Headset")
            with patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Built-in Audio", "The configured output device 'Missing Headset' could not be opened; using the system default instead.", None),
            ):
                report = speech_output_diagnostics(config, FakeSynthesizer())

        self.assertEqual("Missing Headset", report["output_device"])
        self.assertEqual("Built-in Audio", report["output_device_in_use"])
        self.assertIn("instead", report["output_device_detail"])

    def test_a_probe_that_fails_does_not_abandon_the_rest_of_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch(
                    "murmly.cli.speech_output_diagnostics",
                    side_effect=RuntimeError("the probe exploded"),
                ),
                patch.object(
                    FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")
                ),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIn("the probe exploded", report["speech_output"]["detail"])
        self.assertFalse(report["speech_output"]["available"])
        self.assertEqual("cpu", report["runtime_device"], "another section was abandoned")
        self.assertIn("installation", report)

    def test_diagnostics_report_both_hotkeys_with_their_purposes(self) -> None:
        from murmly.cli import installation_diagnostics

        class FakeInstaller:
            def status(self):
                return {
                    "installed": True,
                    "service_active": True,
                    "entrypoint": "/bin/murmly",
                    "hotkey": "Meta+X",
                    "hotkey_held": True,
                    "detail": "running",
                    "hotkeys": [
                        {"purpose": "window", "hotkey": "Meta+X", "held": True},
                        {"purpose": "session", "hotkey": "Meta+A", "held": False},
                    ],
                }

        with patch("murmly.cli.Installer", FakeInstaller):
            report = installation_diagnostics()

        purposes = {entry["purpose"]: entry for entry in report["hotkeys"]}
        self.assertEqual({"window", "session"}, set(purposes))
        self.assertTrue(purposes["window"]["held"])
        self.assertFalse(purposes["session"]["held"])


class DaemonExitTeardownTests(unittest.TestCase):
    """The daemon must not leave PortAudio's exit-time teardown registered.

    It aborts the process when the audio server has already stopped -- which is
    what a logout is -- and systemd records the abort as a failed unit that will
    not start again until it is reset by hand.
    """

    def _config(self, temp_dir: str) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )

    def test_a_clean_run_disables_the_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("murmly.cli._serve_daemon", return_value=0),
                patch("murmly.cli.disable_portaudio_exit_teardown") as disable,
            ):
                self.assertEqual(0, _run_daemon(self._config(temp_dir)))

        disable.assert_called_once_with()

    def test_a_startup_refusal_disables_the_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("murmly.cli.MurmlyDaemon", side_effect=DaemonStartupError("refused")),
                patch("murmly.cli.disable_portaudio_exit_teardown") as disable,
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(1, _run_daemon(self._config(temp_dir)))

        disable.assert_called_once_with()

    def test_an_unhandled_error_disables_the_teardown_on_its_way_out(self) -> None:
        """The abort happens at process exit whatever the process is exiting for."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("murmly.cli.MurmlyDaemon", side_effect=RuntimeError("worker exploded")),
                patch("murmly.cli.disable_portaudio_exit_teardown") as disable,
            ):
                with self.assertRaises(RuntimeError):
                    _run_daemon(self._config(temp_dir))

        disable.assert_called_once_with()

    def test_a_command_other_than_the_daemon_leaves_the_teardown_alone(self) -> None:
        """A short-lived command keeps sounddevice's own exit behavior."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[daemon]\nsocket_path = "{Path(temp_dir) / "murmly.sock"}"\n',
                encoding="utf-8",
            )
            with (
                patch("murmly.cli.disable_portaudio_exit_teardown") as disable,
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cpu", "int8"),
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(0, main(["--config", str(config_path), "doctor"]))

        disable.assert_not_called()

class DaemonExitWithoutFinalizeTests(unittest.TestCase):
    """The daemon must leave without running interpreter finalization.

    Dropping PortAudio's exit teardown leaves its `pw-PortAudio` loop threads
    running, and `Py_Finalize` unloads the extension libraries underneath them.
    The daemon dumped core exactly that way on 2026-08-22. See issue #27.
    """

    def _config_file(self, temp_dir: str) -> Path:
        config_path = Path(temp_dir) / "config.toml"
        config_path.write_text(
            f'[daemon]\nsocket_path = "{Path(temp_dir) / "murmly.sock"}"\n',
            encoding="utf-8",
        )
        return config_path

    def test_a_clean_daemon_run_leaves_with_its_own_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli._run_daemon", return_value=0),
                patch("murmly.cli.leave_without_finalizing") as leave,
            ):
                main(["--config", str(config_path), "daemon"])

        leave.assert_called_once_with(0)

    def test_a_daemon_that_refuses_to_start_leaves_with_its_own_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli._run_daemon", return_value=1),
                patch("murmly.cli.leave_without_finalizing") as leave,
            ):
                main(["--config", str(config_path), "daemon"])

        leave.assert_called_once_with(1)

    def test_an_unhandled_error_still_leaves_without_finalizing(self) -> None:
        """The backstop in main() is a route out of the daemon like any other."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli._run_daemon", side_effect=RuntimeError("worker exploded")),
                patch("murmly.cli.leave_without_finalizing") as leave,
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                main(["--config", str(config_path), "daemon"])

        leave.assert_called_once_with(1)

    def test_a_broken_stderr_does_not_stop_the_daemon_leaving(self) -> None:
        """The handler reports before it sets the status, and reporting can fail.

        A daemon whose journal socket has already gone raises BrokenPipeError from
        `traceback.print_exc` and from `print`. Unguarded, that escapes main()
        before the daemon branch is reached, and the process leaves through
        Py_Finalize - the crash this whole path exists to avoid.
        """

        class BrokenStderr(StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError("journal socket is gone")

            def flush(self) -> None:
                raise BrokenPipeError("journal socket is gone")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli._run_daemon", side_effect=RuntimeError("worker exploded")),
                patch("murmly.cli.leave_without_finalizing") as leave,
                patch("murmly.cli.sys.stderr", BrokenStderr()),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "daemon"])

        leave.assert_called_once_with(1)

    def test_a_command_other_than_the_daemon_returns_the_ordinary_way(self) -> None:
        """Only the daemon is in the configuration that makes finalization unsafe."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli.leave_without_finalizing") as leave,
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cpu", "int8"),
                ),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(0, main(["--config", str(config_path), "doctor"]))

        leave.assert_not_called()

    def test_it_flushes_before_leaving(self) -> None:
        """os._exit skips the flushing finalization would have done."""
        flushed: list[str] = []

        class RecordingStream(StringIO):
            def __init__(self, name: str) -> None:
                super().__init__()
                self._name = name

            def flush(self) -> None:
                flushed.append(self._name)
                super().flush()

        with (
            patch("murmly.cli.os._exit") as hard_exit,
            patch("murmly.cli.logging.shutdown") as logging_shutdown,
            patch("murmly.cli.sys.stdout", RecordingStream("stdout")),
            patch("murmly.cli.sys.stderr", RecordingStream("stderr")),
        ):
            leave_without_finalizing(7)

        self.assertEqual(["stdout", "stderr"], flushed)
        logging_shutdown.assert_called_once_with()
        hard_exit.assert_called_once_with(7)

    def test_a_failed_flush_does_not_stop_it_leaving(self) -> None:
        """The journal socket can already be gone; a BrokenPipeError here would
        otherwise escape into main() and leave through Py_Finalize -- the crash
        this whole path exists to avoid."""

        class BrokenStream(StringIO):
            def flush(self) -> None:
                raise BrokenPipeError("journal socket is gone")

        for failing in ("stdout", "stderr", "logging"):
            with self.subTest(failing=failing):
                with (
                    patch("murmly.cli.os._exit") as hard_exit,
                    patch(
                        "murmly.cli.logging.shutdown",
                        side_effect=RuntimeError("handler raised")
                        if failing == "logging"
                        else None,
                    ),
                    patch(
                        "murmly.cli.sys.stdout",
                        BrokenStream() if failing == "stdout" else StringIO(),
                    ),
                    patch(
                        "murmly.cli.sys.stderr",
                        BrokenStream() if failing == "stderr" else StringIO(),
                    ),
                ):
                    leave_without_finalizing(3)

                hard_exit.assert_called_once_with(3)

    def test_one_failed_flush_does_not_skip_the_others(self) -> None:
        """A broken stdout says nothing about the log handlers."""
        flushed: list[str] = []

        class BrokenStdout(StringIO):
            def flush(self) -> None:
                raise BrokenPipeError("journal socket is gone")

        class RecordingStderr(StringIO):
            def flush(self) -> None:
                flushed.append("stderr")
                super().flush()

        with (
            patch("murmly.cli.os._exit") as hard_exit,
            patch("murmly.cli.logging.shutdown") as logging_shutdown,
            patch("murmly.cli.sys.stdout", BrokenStdout()),
            patch("murmly.cli.sys.stderr", RecordingStderr()),
        ):
            leave_without_finalizing(0)

        self.assertEqual(["stderr"], flushed)
        logging_shutdown.assert_called_once_with()
        hard_exit.assert_called_once_with(0)
