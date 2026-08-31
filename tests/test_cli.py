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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from channel_helpers import command_channel_address
from module_stubs import injected_module

from murmly.cli import (
    _measurement_clip,
    _run_daemon,
    _run_doctor,
    leave_without_finalizing,
    command_socket_diagnostics,
    daemon_residency,
    delivery_diagnostics,
    live_transcription_diagnostics,
    main,
    measure_partial_pass_ms,
    microphone_diagnostics,
    overlay_diagnostics,
    paste_injection_diagnostics,
    platform_diagnostics,
    transcription_model_cache_path,
)
from murmly.config import MurmlyConfig
from murmly.daemon import DaemonNotRespondingError, DaemonStartupError, MurmlyDaemon, send_command
from murmly.integrations import PasteInjection
from murmly.overlay import (
    SYSTEM_PYTHON,
    OverlayBackend,
    OverlayHealth,
    renderer_environment,
    renderer_python,
)
from murmly.platform import (
    BACKEND_REGISTRIES,
    Desktop,
    OperatingSystem,
    Permission,
    PermissionState,
    PlatformProfile,
    RuntimeGap,
    resolve_platform,
)
from murmly.stt import FasterWhisperTranscriber


@contextmanager
def _pinned_overlay_platform(operating_system: OperatingSystem, backend: OverlayBackend | None):
    """Pins what `overlay_diagnostics` sees for the platform and the overlay
    backend, independent of the real host `sys.platform` genuinely resolves
    to -- the same two-name patch `test_overlay_diagnostics_reports_the_qt_
    renderer_on_windows` already uses for its Windows case, generalized so
    every other scenario in this file can assert its own platform's
    behaviour on any CI host, real Windows included.

    Both names need patching, not just one: `overlay_diagnostics` calls
    `resolve_platform` directly for its own `desktop`/`session` fields, but
    `detect_overlay_backend` resolves the operating system through
    `murmly.overlay`'s own module-level `resolve_platform` binding, which
    patching `murmly.cli.resolve_platform` never touches.
    """
    profile = PlatformProfile(operating_system=operating_system, architecture="x86_64")
    with (
        patch("murmly.cli.resolve_platform", return_value=profile),
        patch("murmly.cli.detect_overlay_backend", return_value=backend),
    ):
        yield


class CliTests(unittest.TestCase):
    def test_daemon_exits_cleanly_on_sigterm(self) -> None:
        if not hasattr(os, "getuid") or not resolve_platform().supported:
            # A real subprocess running the real `python -m murmly`, so there
            # is no profile to inject the way every other test in this file
            # injects one into `main()` in-process: it runs under whatever
            # `resolve_platform()` genuinely resolves for this host, checked
            # directly rather than hardcoding a platform name. Windows joined
            # `SUPPORTED_OPERATING_SYSTEMS` (see `test_platform.py`'s
            # `test_supported_reports_only_the_operating_systems_murmly_
            # claims_today`), but still has no `os.getuid`, so this still
            # skips there -- for a different, and still valid, reason:
            # `SIGTERM` is not the same signal there either, since
            # `Popen.send_signal` maps it to `TerminateProcess`, not a
            # catchable signal a clean-exit handler could run for. macOS has
            # both `os.getuid` and a real `SIGTERM`, but is not in
            # `SUPPORTED_OPERATING_SYSTEMS` at all, per this change's binding
            # scope decision, so it skips on the `not .supported` half instead.
            self.skipTest("needs a real subprocess on a Murmly-supported operating system")
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
                encoding="utf-8",
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

    def test_doctor_reports_the_transcription_model_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "auto")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                patch(
                    "murmly.cli.transcription_model_cache_path",
                    return_value=("/tmp/hf-cache/hub", None),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertEqual("/tmp/hf-cache/hub", report["model_cache_path"])
        self.assertNotIn("model_cache_detail", report)

    def test_doctor_reports_system_memory_as_returnable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "auto")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                patch("murmly.cli.system_memory_returnable", return_value=True),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIs(True, report["system_memory_returnable"])
        self.assertNotIn("system_memory_returnable_detail", report)

    def test_doctor_names_the_reason_where_system_memory_is_not_returnable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "auto")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                patch("murmly.cli.system_memory_returnable", return_value=False),
                patch(
                    "murmly.cli.system_memory_unreturnable_reason",
                    return_value="this platform's C library has no malloc_trim",
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIs(False, report["system_memory_returnable"])
        self.assertEqual(
            "this platform's C library has no malloc_trim",
            report["system_memory_returnable_detail"],
        )

    def test_an_unresolvable_model_cache_is_named_rather_than_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "auto")),
                patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
                patch(
                    "murmly.cli.select_paste_injection",
                    return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
                ),
                patch(
                    "murmly.cli.transcription_model_cache_path",
                    return_value=(None, "Unable to determine the transcription model cache: boom"),
                ),
                redirect_stdout(StringIO()) as output,
            ):
                _run_doctor(config)

        report = json.loads(output.getvalue())
        self.assertIsNone(report["model_cache_path"])
        self.assertIn("boom", report["model_cache_detail"])

    def test_the_transcription_model_cache_path_asks_huggingface_hub(self) -> None:
        """2.5: the cache stays wherever `huggingface_hub` itself resolves it,

        not somewhere `murmly` derives on its own -- moving it would strand
        the 1.6 GB an existing install already has cached under the Hub's own
        answer.
        """
        import huggingface_hub.constants as hf_constants

        path, detail = transcription_model_cache_path()

        self.assertIsNone(detail)
        self.assertEqual(hf_constants.HF_HUB_CACHE, path)

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

        with _pinned_overlay_platform(OperatingSystem.LINUX, OverlayBackend.X11):
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
        # PyGObject and GTK4 are distribution packages the system interpreter
        # has them installed into, so the report and the subprocess it ran the
        # check with must agree on that interpreter rather than each picking
        # their own notion of "the" Python.
        self.assertEqual(str(renderer_python(OverlayBackend.X11)), report["system_python"])

    def test_overlay_diagnostics_rejecting_the_session_still_names_an_interpreter(self) -> None:
        """No backend is selected here, so this is `renderer_python`'s only caller
        that cannot hand it a backend -- and the answer must stay what it always
        was rather than silently becoming `None`."""
        config = self._config()
        with _pinned_overlay_platform(OperatingSystem.LINUX, None):
            report = overlay_diagnostics(
                config,
                env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
                run_command=lambda *_args, **_kwargs: self.fail("helper should not run"),
            )

        self.assertIsNone(report["backend"])
        self.assertEqual(str(SYSTEM_PYTHON), report["system_python"])

    def test_overlay_diagnostics_checks_under_the_renderer_environment(self) -> None:
        config = self._config()
        completed = self._helper_result({"available": True, "gtk4_layer_shell": True})
        recorded: dict[str, object] = {}

        def record(*arguments: object, **keywords: object) -> object:
            recorded.update(keywords)
            return completed

        with _pinned_overlay_platform(OperatingSystem.LINUX, OverlayBackend.WAYLAND):
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

        with _pinned_overlay_platform(OperatingSystem.LINUX, OverlayBackend.WAYLAND):
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
        with _pinned_overlay_platform(OperatingSystem.LINUX, None):
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
        with _pinned_overlay_platform(OperatingSystem.LINUX, None):
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

    def test_overlay_diagnostics_reports_the_qt_renderer_on_windows(self) -> None:
        """Task 10.1/10.5: on Windows the Qt renderer's own `--check` is
        launched (`renderer_script`, not the hardcoded GTK4 file name), and
        `session`/`desktop` name the platform instead of misreporting a
        non-Linux machine as `x11` (task 6.3's same reasoning, applied here)."""
        config = self._config()
        completed = self._helper_result(
            {"available": True, "pyside6": True, "qt_version": "6.11.2"}
        )
        launches: list[list[str]] = []

        def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            launches.append(command)
            return completed

        windows_profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        with (
            patch("murmly.cli.resolve_platform", return_value=windows_profile),
            patch("murmly.cli.detect_overlay_backend", return_value=OverlayBackend.WINDOWS),
        ):
            report = overlay_diagnostics(config, env={}, run_command=record)

        self.assertTrue(report["available"])
        self.assertEqual("windows", report["backend"])
        self.assertEqual("windows", report["session"])
        self.assertEqual("windows", report["desktop"])
        self.assertTrue(report["pyside6"])
        self.assertEqual("6.11.2", report["qt_version"])
        # Every GTK4-only key still present, as its does-not-apply default --
        # the field set does not depend on which renderer answered.
        self.assertFalse(report["pygobject"])
        self.assertIsNone(report["gtk4"])

        command = launches[0]
        self.assertEqual(str(Path(sys.executable)), command[0])
        self.assertTrue(command[1].endswith("overlay_renderer_qt.py"))
        self.assertIn("windows", command)

    def test_overlay_diagnostics_reports_the_qt_renderer_on_macos(self) -> None:
        """Task 15.1: the same Qt renderer Windows launches, launched with
        `--backend macos` instead -- `session`/`desktop` name the platform
        the same way they do for Windows (task 6.3's reasoning again)."""
        config = self._config()
        completed = self._helper_result(
            {"available": True, "pyside6": True, "qt_version": "6.11.2"}
        )
        launches: list[list[str]] = []

        def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            launches.append(command)
            return completed

        macos_profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
        with (
            patch("murmly.cli.resolve_platform", return_value=macos_profile),
            patch("murmly.cli.detect_overlay_backend", return_value=OverlayBackend.MACOS),
        ):
            report = overlay_diagnostics(config, env={}, run_command=record)

        self.assertTrue(report["available"])
        self.assertEqual("macos", report["backend"])
        self.assertEqual("macos", report["session"])
        self.assertEqual("macos", report["desktop"])
        self.assertTrue(report["pyside6"])
        self.assertEqual("6.11.2", report["qt_version"])
        self.assertFalse(report["pygobject"])
        self.assertIsNone(report["gtk4"])

        command = launches[0]
        self.assertEqual(str(Path(sys.executable)), command[0])
        self.assertTrue(command[1].endswith("overlay_renderer_qt.py"))
        self.assertIn("macos", command)

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
            patch("murmly.cli.create_clipboard_paster") as paster,
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

    def _successful_outcome(self, hotkey_registered: bool = True):
        from murmly.installer import InstallOutcome

        return InstallOutcome(
            entrypoint=Path("/bin/murmly"),
            hotkey=None,
            service_installed=True,
            hotkey_registered=hotkey_registered,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=("Registered Meta+X.",),
        )

    def test_a_desktop_held_mechanism_never_sends_a_rebind(self) -> None:
        """Task 5.5: a no-op on every platform this change targets -- Plasma
        and GNOME both hold the binding declaratively, not in this process."""
        from murmly.cli import _run_install
        from murmly.platform import OperatingSystem, PlatformProfile, Desktop

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA)
        with (
            patch("murmly.cli.Installer") as installer,
            patch("murmly.cli.send_command") as sent,
            redirect_stdout(StringIO()),
        ):
            installer.return_value.install.return_value = self._successful_outcome()
            exit_code = _run_install("Meta+X", profile=profile)

        self.assertEqual(0, exit_code)
        sent.assert_not_called()

    def test_a_declined_hotkey_never_sends_a_rebind_either(self) -> None:
        from murmly.cli import _run_install
        from murmly.platform import OperatingSystem, PlatformProfile, Desktop

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA)
        with (
            patch("murmly.cli.Installer") as installer,
            patch("murmly.cli.send_command") as sent,
            redirect_stdout(StringIO()),
        ):
            installer.return_value.install.return_value = self._successful_outcome(hotkey_registered=False)
            exit_code = _run_install("Meta+X", profile=profile)

        self.assertEqual(0, exit_code)
        sent.assert_not_called()

    def test_an_in_process_mechanism_reaches_the_running_daemon(self) -> None:
        """The branch a future Windows or macOS backend exercises, proven by
        patching the marker function rather than by a real in-process backend
        existing (none does yet)."""
        from murmly.cli import _run_install
        from murmly.config import MurmlyConfig

        config = MurmlyConfig(socket_path=Path("/tmp/murmly-test.sock"), config_path=Path("/tmp/config.toml"))
        with (
            patch("murmly.cli.Installer") as installer,
            patch("murmly.cli.hotkey_mechanism_is_in_process", return_value=True),
            patch("murmly.cli.send_command") as sent,
            redirect_stdout(StringIO()),
        ):
            installer.return_value.install.return_value = self._successful_outcome()
            exit_code = _run_install("Meta+X", config=config)

        self.assertEqual(0, exit_code)
        sent.assert_called_once_with(str(config.socket_path), "rebind_hotkeys")

    def test_an_in_process_mechanism_with_no_running_daemon_still_reports_success(self) -> None:
        """A daemon that is not running picks the new keys up from the record
        at its next start -- failing to reach one now is not an install
        failure."""
        from murmly.cli import _run_install
        from murmly.config import MurmlyConfig
        from murmly.daemon import DaemonNotRespondingError

        config = MurmlyConfig(socket_path=Path("/tmp/murmly-test.sock"), config_path=Path("/tmp/config.toml"))
        with (
            patch("murmly.cli.Installer") as installer,
            patch("murmly.cli.hotkey_mechanism_is_in_process", return_value=True),
            patch("murmly.cli.send_command", side_effect=DaemonNotRespondingError("no reply")),
            redirect_stdout(StringIO()) as output,
        ):
            installer.return_value.install.return_value = self._successful_outcome()
            exit_code = _run_install("Meta+X", config=config)

        self.assertEqual(0, exit_code)
        self.assertIn("Registered Meta+X.", output.getvalue())


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


class SyncCommandTests(unittest.TestCase):
    """Task 16.1's caller in `cli.py`: `murmly sync` reaches
    `environment.py`'s pre-sync gates and its extras/GPU-swap logic, refusing
    or delegating rather than reimplementing either."""

    @staticmethod
    def _parse(*argv: str):
        from murmly.cli import build_parser

        return build_parser().parse_args(["sync", *argv])

    def test_refuses_before_syncing_on_a_machine_with_no_transcription_runtime(self) -> None:
        from murmly.cli import _run_sync

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="musl")
        with (
            patch("murmly.environment.sync_environment") as sync_environment,
            patch("murmly.environment.install_system_packages") as install_system_packages,
            redirect_stderr(StringIO()) as errors,
        ):
            exit_code = _run_sync(self._parse(), profile)

        self.assertEqual(1, exit_code)
        self.assertIn("ctranslate2", errors.getvalue())
        sync_environment.assert_not_called()
        install_system_packages.assert_not_called()

    def test_refuses_before_syncing_when_a_windows_precondition_fails(self) -> None:
        from murmly.cli import _run_sync

        # `refuse_before_sync` is patched out wholesale -- rather than relied
        # on to pass a Windows profile through its own unsupported-platform
        # check for free -- so this test does not depend on whether Windows
        # is in `SUPPORTED_OPERATING_SYSTEMS`: it isolates the
        # environment-precondition refusal this test is actually about,
        # regardless of that flag's current value.
        profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        with (
            patch("murmly.environment.refuse_before_sync", return_value=None),
            patch(
                "murmly.environment.refuse_or_warn_environment_preconditions",
                return_value="murmly: refusing to sync. long paths are off.",
            ),
            patch("murmly.environment.sync_environment") as sync_environment,
            redirect_stderr(StringIO()) as errors,
        ):
            exit_code = _run_sync(self._parse(), profile)

        self.assertEqual(1, exit_code)
        self.assertIn("long paths are off", errors.getvalue())
        sync_environment.assert_not_called()

    def test_installs_system_packages_only_on_linux(self) -> None:
        from murmly.cli import _run_sync

        windows_profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        with (
            patch("murmly.environment.refuse_before_sync", return_value=None),
            patch("murmly.environment.install_system_packages") as install_system_packages,
            patch("murmly.environment.sync_environment"),
        ):
            _run_sync(self._parse(), windows_profile)

        install_system_packages.assert_not_called()

        linux_profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="glibc")
        with (
            patch("murmly.environment.install_system_packages") as install_system_packages,
            patch("murmly.environment.sync_environment"),
        ):
            _run_sync(self._parse(), linux_profile)

        install_system_packages.assert_called_once()

    def test_forwards_flags_to_sync_environment(self) -> None:
        from murmly.cli import _run_sync

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="glibc")
        with (
            patch("murmly.environment.install_system_packages"),
            patch("murmly.environment.sync_environment") as sync_environment,
        ):
            exit_code = _run_sync(
                self._parse("--cuda", "--no-tts", "--yes", "--project", "/tmp/some-project"), profile
            )

        self.assertEqual(0, exit_code)
        sync_environment.assert_called_once()
        _args, kwargs = sync_environment.call_args
        self.assertEqual(Path("/tmp/some-project"), _args[0])
        self.assertEqual("yes", kwargs["want_cuda"])
        self.assertEqual("no", kwargs["want_tts"])

    def test_a_failing_sync_reports_and_exits_non_zero(self) -> None:
        from murmly.cli import _run_sync
        from murmly.environment import EnvironmentSyncError

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="glibc")
        with (
            patch("murmly.environment.install_system_packages"),
            patch("murmly.environment.sync_environment", side_effect=EnvironmentSyncError("uv sync failed")),
            redirect_stderr(StringIO()) as errors,
        ):
            exit_code = _run_sync(self._parse(), profile)

        self.assertEqual(1, exit_code)
        self.assertIn("uv sync failed", errors.getvalue())

    def test_declines_prompts_with_nothing_attached_and_no_yes(self) -> None:
        """Task 16.6, exercised through the real `make_confirm` this command
        builds -- not a fake -- with stdin faked as non-interactive."""
        from murmly.cli import _run_sync

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="glibc")
        confirms: list = []

        def fake_sync_environment(_project_dir, *, confirm, **_kwargs):
            confirms.append(confirm("Install the GPU runtime?"))

        with (
            patch("murmly.environment.install_system_packages"),
            patch("murmly.environment.sync_environment", side_effect=fake_sync_environment),
            patch("sys.stdin.isatty", return_value=False),
        ):
            exit_code = _run_sync(self._parse(), profile)

        self.assertEqual(0, exit_code)
        self.assertEqual([False], confirms)


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
        if not hasattr(socket, "AF_UNIX"):
            # Builds its own bare UNIX-socket server by hand to simulate a
            # daemon that accepts and then closes without responding -- the
            # same pattern `test_daemon.py`'s equivalent tests skip on
            # Windows for, and for the same reason: no comparably bare way to
            # fake this against a real named-pipe server without
            # reimplementing `win_pipe.NamedPipeServer` here a second time.
            self.skipTest("needs a real UNIX socket to build a bare server for")
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
                with (
                    patch(
                        "murmly.cli.resolve_platform",
                        return_value=PlatformProfile(
                            operating_system=OperatingSystem.LINUX, architecture="x86_64"
                        ),
                    ),
                    redirect_stdout(StringIO()),
                    redirect_stderr(StringIO()) as errors,
                ):
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

            with (
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "doctor"])

        self.assertEqual(1, exit_code)
        self.assertIn(str(config_path), errors.getvalue())
        self.assertIn("Unable to read the configuration", errors.getvalue())

    def test_a_daemon_that_refuses_to_start_is_reported_and_exits_non_zero(self) -> None:
        if not hasattr(os, "getuid"):
            self.skipTest("needs POSIX directory permissions, which Windows does not enforce this way")
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
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
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
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
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
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
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


class UnsupportedPlatformTests(unittest.TestCase):
    """An operating system Murmly does not support is refused before anything runs (1.7, 18.3)."""

    def _unsupported_profile(self) -> PlatformProfile:
        return PlatformProfile(operating_system=OperatingSystem.OTHER, architecture="x86_64")

    def test_install_is_refused_naming_the_platform_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Never created: a refusal that ran must not have to create the
            # directory a config file would live in before it can be missing.
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch("murmly.cli.resolve_platform", return_value=self._unsupported_profile()),
                patch("murmly.cli.Installer") as installer,
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "install", "Meta+X"])

            self.assertEqual(1, exit_code)
            reported = errors.getvalue()
            self.assertIn("other", reported)
            self.assertIn("linux", reported)
            installer.assert_not_called()
            self.assertEqual([], os.listdir(temp_dir), "a refused platform must write nothing")

    def test_daemon_is_refused_before_the_config_is_even_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch("murmly.cli.resolve_platform", return_value=self._unsupported_profile()),
                patch("murmly.cli.MurmlyDaemon") as daemon,
                patch("murmly.cli.leave_without_finalizing") as leave,
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "daemon"])

            self.assertEqual(1, exit_code)
            self.assertIn("other", errors.getvalue())
            daemon.assert_not_called()
            leave.assert_called_once_with(1)
            self.assertEqual([], os.listdir(temp_dir))

    def test_doctor_is_refused_too(self) -> None:
        """Every command is refused, not only the ones that write files.

        Pins spec.md's "An unsupported operating system" scenario literally:
        it says every command, doctor included. If a later diagnostics change
        wants doctor to run anyway and explain the refusal itself, that is a
        spec change, not a fix to this test.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch("murmly.cli.resolve_platform", return_value=self._unsupported_profile()),
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "doctor"])

            self.assertEqual(1, exit_code)
            self.assertIn("other", errors.getvalue())
            self.assertEqual([], os.listdir(temp_dir))

    def test_toggle_is_refused_without_starting_the_service(self) -> None:
        """The command most likely to run unattended: a hotkey press.

        `toggle` reaches `send_command_with_recovery`, which can start the
        installed systemd service when nothing answers on the socket. An
        unsupported platform must be refused before that recovery path ever
        runs, not merely before the commands that obviously write files.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            with (
                patch("murmly.cli.resolve_platform", return_value=self._unsupported_profile()),
                patch("murmly.cli.send_command_with_recovery") as send,
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = main(["--config", str(config_path), "toggle"])

            self.assertEqual(1, exit_code)
            self.assertIn("other", errors.getvalue())
            send.assert_not_called()
            self.assertEqual([], os.listdir(temp_dir))

    def test_a_supported_platform_is_not_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            supported = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
            with (
                patch("murmly.cli.resolve_platform", return_value=supported),
                patch.object(FasterWhisperTranscriber, "resolve_runtime", return_value=("cpu", "int8")),
                redirect_stdout(StringIO()),
            ):
                exit_code = main(["--config", str(config_path), "doctor"])

            self.assertEqual(0, exit_code)


class TranscriptionRuntimeGapTests(unittest.TestCase):
    """The machine-capability check refuses the daemon before it is constructed (1.8, 18.4)."""

    def _config(self, temp_dir: str) -> MurmlyConfig:
        return MurmlyConfig(
            socket_path=Path(temp_dir) / "murmly.sock",
            config_path=Path(temp_dir) / "config.toml",
        )

    def test_a_missing_transcription_runtime_refuses_before_the_daemon_is_built(self) -> None:
        from murmly.cli import _serve_daemon

        gap = RuntimeGap(
            runtime="ctranslate2",
            characteristic="a musl C library",
            capability="transcription",
            matches=lambda profile: True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("murmly.cli.transcription_runtime_gap", return_value=gap),
                patch("murmly.cli.MurmlyDaemon") as daemon,
                redirect_stderr(StringIO()) as errors,
            ):
                exit_code = _serve_daemon(
                    self._config(temp_dir),
                    PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                )

        self.assertEqual(1, exit_code)
        daemon.assert_not_called()
        reported = errors.getvalue()
        self.assertIn("ctranslate2", reported)
        self.assertIn("musl", reported)

    def test_a_gap_outside_transcription_starts_and_reports_the_capability_unavailable(self) -> None:
        from murmly.cli import _serve_daemon

        other_gap = RuntimeGap(
            runtime="onnxruntime",
            characteristic="a fictional architecture",
            capability="speech synthesis",
            matches=lambda profile: True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_daemon = Mock()
            fake_daemon.serve_forever.return_value = None
            with (
                patch("murmly.cli.transcription_runtime_gap", return_value=None),
                patch("murmly.cli.runtime_gaps_for", return_value=(other_gap,)),
                patch("murmly.cli.MurmlyDaemon", return_value=fake_daemon) as daemon,
                self.assertLogs("murmly.cli", level="WARNING") as logs,
            ):
                exit_code = _serve_daemon(
                    self._config(temp_dir),
                    PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                )

        self.assertEqual(0, exit_code)
        daemon.assert_called_once()
        fake_daemon.serve_forever.assert_called_once()
        joined = " ".join(logs.output)
        self.assertIn("speech synthesis", joined)
        self.assertIn("onnxruntime", joined)
        self.assertIn("unavailable", joined)


class DoctorCompletenessTests(unittest.TestCase):
    """`murmly doctor` reports every section it can and explains the ones it cannot."""

    SECTIONS = (
        "config_path",
        "socket_path",
        "command_socket",
        "platform",
        "session",
        "clipboard_command",
        "paste_injection",
        "model_profile",
        "model_name",
        "model_cache_path",
        "device",
        "compute_type",
        "runtime_device",
        "runtime_compute_type",
        "beam_size",
        "vad_filter",
        "model_resident",
        "unload_after_idle_s",
        "system_memory_returnable",
        "live_transcription",
        "delivery",
        "overlay",
        "speech_output",
        "microphone",
        "installation",
    )

    def _report(
        self,
        config: MurmlyConfig,
        resolve_runtime: object,
        profile: PlatformProfile | None = None,
    ) -> dict[str, object]:
        with (
            patch.object(FasterWhisperTranscriber, "resolve_runtime", resolve_runtime),
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            # Pinned like every other host fact this helper already fixes:
            # `system_memory_returnable` reads the real C library linked into
            # this interpreter, not `profile`, so a musl or macOS host would
            # otherwise add `system_memory_returnable_detail` to the report
            # and fail every shape assertion below that expects `SECTIONS`
            # exactly -- a host difference the True/False behaviour itself
            # already has its own dedicated tests for, elsewhere in this file.
            patch("murmly.cli.system_memory_returnable", return_value=True),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config, profile)
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
        # A daemon that answers, so this is the shape when nothing failed. The
        # `*_detail` keys are what the report adds when something did, and one
        # appearing here would mean a probe was reported as unanswerable.
        if not hasattr(os, "getuid"):
            # `command_socket.path_private` below needs a real POSIX
            # directory's real permissions to come back true, the same as
            # `test_a_private_socket_path_is_reported_as_private`; the
            # `linux_profile` injection keeps the rest of this Linux-shaped
            # report (session, platform.operating_system) exercised on macOS
            # too, but cannot substitute for the POSIX host this one field
            # still needs.
            self.skipTest("needs a POSIX host: command_socket.path_private reads real POSIX permissions")
        linux_profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
        answered = {"ok": True, "state": "IDLE", "model_resident": False}
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch("murmly.cli.send_command", return_value=answered):
                report = self._report(config, Mock(return_value=("cuda", "float16")), profile=linux_profile)

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
        # `peer_identity_supported` is what keeps the shape identical across
        # platforms -- it is always present, and reports the concern
        # unavailable (False) rather than being absent, exactly as the spec's
        # "diagnostics report keeps its shape" scenario requires.
        # `peer_identity_detail` is the accompanying explanation, added only
        # when there is something to explain, the same convention `delivery`
        # and `platform.concerns` already use elsewhere in this same report
        # (compare `test_a_private_socket_path_is_reported_as_private`'s
        # `assertNotIn("detail", report)`). `linux_profile` cannot make this
        # one field answer as Linux would on a real macOS interpreter:
        # `peer_identity_supported` reads `hasattr(socket, "SO_PEERCRED")` on
        # the real host, not the injected profile -- `getpeereid` for macOS
        # is tasks.md 13.2, not yet built -- exactly as
        # `test_a_private_socket_path_reports_peer_identity_support`'s
        # docstring already documents for this same field.
        expected_command_socket_fields = {"path", "path_private", "peer_identity_supported"}
        if not hasattr(socket, "SO_PEERCRED"):
            expected_command_socket_fields.add("peer_identity_detail")
        self.assertEqual(
            expected_command_socket_fields,
            set(report["command_socket"]),
        )
        self.assertTrue(report["command_socket"]["path_private"])
        self.assertEqual(
            {
                "operating_system",
                "supported",
                "architecture",
                "libc",
                "desktop",
                "concerns",
                "permissions",
                "environment",
            },
            set(report["platform"]),
        )
        self.assertEqual(set(BACKEND_REGISTRIES), set(report["platform"]["concerns"]))
        self.assertEqual("linux", report["platform"]["operating_system"])
        # 9.5: the Windows microphone permission's `applies` keeps a Linux
        # report from growing an entry for a grant Linux does not gate
        # anything behind -- the field stays exactly as empty as it was
        # before that permission existed.
        self.assertEqual({}, report["platform"]["permissions"])
        # 11.5, 11.6: same rule, for the two Windows environment preconditions.
        self.assertEqual({}, report["platform"]["environment"])

    def test_the_report_carries_the_same_field_names_on_a_platform_with_no_mechanisms(self) -> None:
        """18.17: an unserviceable concern is reported unavailable, not absent
        -- proved by a platform this change gives no backend at all (an
        unrecognized operating system, `OperatingSystem.OTHER`), so every
        concern reports unavailable rather than one being dropped."""
        answered = {"ok": True, "state": "IDLE", "model_resident": False}
        other_profile = PlatformProfile(operating_system=OperatingSystem.OTHER, architecture="x86_64")
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch("murmly.cli.send_command", return_value=answered):
                report = self._report(
                    config, Mock(return_value=("cpu", "int8")), profile=other_profile
                )

        # Same top-level keys as the Linux report above (6.5), and the same
        # eight concern keys inside `platform` (6.1) -- present and reporting
        # unavailable, never dropped.
        self.assertEqual(set(self.SECTIONS), set(report))
        self.assertEqual(set(BACKEND_REGISTRIES), set(report["platform"]["concerns"]))
        for concern, section in report["platform"]["concerns"].items():
            with self.subTest(concern=concern):
                self.assertFalse(section["available"])
                self.assertIsNone(section["mechanism"])

    def test_windows_reports_the_backends_this_change_built_for_it(self) -> None:
        """The command channel (task 7.1), hotkey registration (task 8.3),
        service management (task 8.1), clipboard (task 9.1), paste injection
        (task 9.2), focus observation (task 9.4) and the overlay (task 10.1)
        all have a Windows mechanism now, and so does speech synthesis --
        which this change did not build for Windows because it did not have
        to: `KokoroSynthesizer` names no platform, and gating its registry on
        Linux was a leftover that made this report deny Windows a capability
        it has. Every concern is expected to resolve here, so 18.17's
        "unavailable rather than dropped" is asserted by the shape check
        above rather than by any concern being absent."""
        answered = {"ok": True, "state": "IDLE", "model_resident": False}
        windows_profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with patch("murmly.cli.send_command", return_value=answered):
                report = self._report(
                    config, Mock(return_value=("cpu", "int8")), profile=windows_profile
                )

        self.assertEqual(set(self.SECTIONS), set(report))
        self.assertEqual(set(BACKEND_REGISTRIES), set(report["platform"]["concerns"]))
        built = {
            "command_channel": "named-pipe",
            "hotkey_registration": "windows-hotkey",
            "service_management": "task-scheduler",
            "clipboard": "windows",
            "paste_injection": "windows",
            "focus_observation": "windows",
            "overlay": "qt",
            "speech_synthesis": "kokoro",
        }
        for concern, section in report["platform"]["concerns"].items():
            with self.subTest(concern=concern):
                if concern in built:
                    self.assertTrue(section["available"])
                    self.assertEqual(built[concern], section["mechanism"])
                else:
                    self.assertFalse(section["available"])
                    self.assertIsNone(section["mechanism"])
                    self.assertTrue(section["reason"])

        # 6.3: a non-Linux session is not misreported as `wayland` or `x11`.
        self.assertNotIn(report["session"], {"wayland", "x11"})
        self.assertEqual("windows", report["session"])

        # 9.5: the first `permissions` entry, present on a Windows profile.
        self.assertIn("windows-microphone", report["platform"]["permissions"])
        self.assertEqual(
            "microphone capture",
            report["platform"]["permissions"]["windows-microphone"]["capability"],
        )

        # 11.5, 11.6: both environment preconditions, present on a Windows
        # profile. Not asserted here: what `satisfied` actually is -- this
        # test also runs on a real Windows CI runner, where
        # `_real_read_registry_value` reads that runner's own registry rather
        # than failing to import `winreg` the way the Linux host running this
        # assertion locally does, so `satisfied` is a real `True`/`False`
        # there, not `None`. Asserted instead is the one thing true on every
        # host: both names are reported, each with a description, and a
        # remedy is present exactly when the precondition is not satisfied --
        # the same restraint this test already shows the microphone entry
        # above, asserting `capability` and never `state`.
        self.assertEqual(
            {"windows-long-paths", "windows-developer-mode"},
            set(report["platform"]["environment"]),
        )
        for name, section in report["platform"]["environment"].items():
            with self.subTest(precondition=name):
                self.assertIn(section["satisfied"], (None, True, False))
                self.assertTrue(section["description"])
                self.assertEqual("remedy" in section, section["satisfied"] is not True)

        # `choose_clipboard_copy_command` has no Windows branch (task 9.1 is a
        # Win32 API call, not a command); `_run_doctor` must not call it and
        # report the wrong platform's remedy instead.
        self.assertEqual("Win32 clipboard API (CF_UNICODETEXT)", report["clipboard_command"])

    def test_macos_reports_the_backends_this_change_built_for_it(self) -> None:
        """The macOS sibling of the Windows test just above: every one of the
        eight concerns has a macOS mechanism (tasks 12-15), so 18.17's
        "unavailable rather than dropped" is asserted by the shape check
        below the same way, with no concern absent.

        `paste_injection` is ANDed with the Accessibility grant
        (`platform_diagnostics`'s own docstring), which reads the real,
        undetermined state of a Linux test runner that has no `Application
        Services` framework at all -- forced to `GRANTED` here so this test
        proves the report's *shape* on every host, the same way
        `test_macos_paste_injection_concern_is_available_when_granted`
        already isolates that concern's own AND-ing from the host's real
        grant state.
        """
        from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, Permission

        answered = {"ok": True, "state": "IDLE", "model_resident": False}
        macos_profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
        granted_accessibility = Permission(
            name=MACOS_ACCESSIBILITY_PERMISSION,
            capability="paste injection",
            grant_location="System Settings > Privacy & Security > Accessibility",
            check=lambda profile: PermissionState.GRANTED,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )
            with (
                patch("murmly.cli.send_command", return_value=answered),
                patch.dict(
                    "murmly.cli.PERMISSIONS",
                    {MACOS_ACCESSIBILITY_PERMISSION: granted_accessibility},
                ),
            ):
                report = self._report(
                    config, Mock(return_value=("cpu", "int8")), profile=macos_profile
                )

        self.assertEqual(set(self.SECTIONS), set(report))
        self.assertEqual(set(BACKEND_REGISTRIES), set(report["platform"]["concerns"]))
        built = {
            "command_channel": "unix-socket",
            "hotkey_registration": "macos-hotkey",
            "service_management": "launchd",
            "clipboard": "macos",
            "paste_injection": "macos",
            "focus_observation": "macos",
            "overlay": "qt",
            "speech_synthesis": "kokoro",
        }
        self.assertEqual(set(built), set(BACKEND_REGISTRIES))
        for concern, section in report["platform"]["concerns"].items():
            with self.subTest(concern=concern):
                self.assertTrue(section["available"])
                self.assertEqual(built[concern], section["mechanism"])

        # 6.3: a non-Linux session is not misreported as `wayland` or `x11`.
        self.assertNotIn(report["session"], {"wayland", "x11"})
        self.assertEqual("macos", report["session"])

        # 12.5, 14.4: both macOS permissions are present, each naming what it
        # gates -- not their `state`, which this Linux runner cannot read for
        # real (the same restraint the Windows test above shows its own
        # registry-backed permissions and preconditions).
        self.assertEqual(
            {"macos-microphone", "macos-accessibility"}, set(report["platform"]["permissions"])
        )
        self.assertEqual(
            "microphone capture", report["platform"]["permissions"]["macos-microphone"]["capability"]
        )
        self.assertEqual(
            "paste injection", report["platform"]["permissions"]["macos-accessibility"]["capability"]
        )

        # 11.5, 11.6: macOS gates neither Windows environment precondition.
        self.assertEqual({}, report["platform"]["environment"])

        # `choose_clipboard_copy_command` has no macOS branch (task 14.1 is an
        # `NSPasteboard` call, not a command); `_run_doctor` must not call it
        # and report a misleading "install xclip" instead (the defect this
        # pins: left unguarded, this field asked the Linux-only chooser about
        # a macOS profile and got exactly that).
        self.assertEqual("NSPasteboard", report["clipboard_command"])

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
        self.assertIn("access control alone", report["peer_identity_detail"])

    def test_a_private_socket_path_is_reported_as_private(self) -> None:
        if not hasattr(os, "getuid"):
            # A filesystem path resolves to Windows' own named-pipe mismatch
            # branch there instead (correctly, and differently) -- this test
            # is about the POSIX directory-privacy analysis specifically,
            # which needs a real POSIX host's own permissions to mean
            # anything, the same as `test_daemon.py`'s `SocketAccessTests`.
            self.skipTest("needs a POSIX host: private-directory reporting reads real POSIX permissions")
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            report = command_socket_diagnostics(config)

        self.assertTrue(report["path_private"])
        self.assertNotIn("detail", report)

    def test_a_private_socket_path_reports_peer_identity_support(self) -> None:
        """Split out of `test_a_private_socket_path_is_reported_as_private`:
        `peer_identity_supported` ignores the `profile` it is handed and
        answers from `hasattr(socket, "SO_PEERCRED")` on the real interpreter
        (task 13 -- macOS's own `getpeereid` mechanism is not built yet), so
        there is no profile this test could inject to make the real answer
        True on a host where it genuinely is not, unlike every other
        Linux-shaped field `command_socket_diagnostics` reports. Kept as its
        own test, rather than folded back with a conditional assertion, so
        Linux keeps proving `SO_PEERCRED` is actually detected rather than
        merely echoed back."""
        if not hasattr(socket, "SO_PEERCRED"):
            self.skipTest("needs a real SO_PEERCRED socket option")
        with tempfile.TemporaryDirectory() as temp_dir:
            config = MurmlyConfig(
                socket_path=Path(temp_dir) / "murmly.sock",
                config_path=Path(temp_dir) / "config.toml",
            )

            report = command_socket_diagnostics(config)

        self.assertTrue(report["peer_identity_supported"])

    def test_a_pipe_shaped_path_is_reported_private_on_windows(self) -> None:
        """Task 7.5: the filesystem path-privacy analysis is skipped entirely
        for a pipe-shaped configured value -- the DACL `win_pipe.
        create_named_pipe_server` builds is unconditionally owner-only, so
        there is nothing else to check."""
        from murmly.config import WINDOWS_PIPE_NAME
        from murmly.platform import OperatingSystem, PlatformProfile

        config = MurmlyConfig(
            socket_path=Path(WINDOWS_PIPE_NAME),
            config_path=Path("/nonexistent/config.toml"),
        )
        windows_profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")

        report = command_socket_diagnostics(config, windows_profile)

        self.assertTrue(report["path_private"])
        self.assertNotIn("detail", report)

    def test_a_pipe_shaped_path_is_reported_not_private_off_windows(self) -> None:
        from murmly.config import WINDOWS_PIPE_NAME

        config = MurmlyConfig(
            socket_path=Path(WINDOWS_PIPE_NAME),
            config_path=Path("/nonexistent/config.toml"),
        )

        # "Off Windows" is the scenario under test, not merely whichever
        # platform happens to run the suite -- an explicit non-Windows
        # profile is what keeps this exercised even on a real Windows
        # runner, where the unresolved default would agree with a
        # pipe-shaped path instead of calling it out as the mismatch it is.
        report = command_socket_diagnostics(
            config, PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
        )

        self.assertFalse(report["path_private"])
        self.assertIn("filesystem socket", report["detail"])

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


class PlatformDiagnosticsTests(unittest.TestCase):
    """`platform_diagnostics`, the `platform` section's own renderer (6.1, 6.2, 6.4)."""

    def test_a_mechanism_that_does_not_exist_names_nothing_to_install(self) -> None:
        # No overlay backend exists for GNOME at all -- the "does not exist"
        # scenario, distinct from Plasma-without-a-display below.
        profile = PlatformProfile(
            operating_system=OperatingSystem.LINUX,
            architecture="x86_64",
            session_type="wayland",
            wayland_display=True,
            desktop=Desktop.GNOME,
        )
        report = platform_diagnostics(profile)
        overlay = report["concerns"]["overlay"]

        self.assertFalse(overlay["available"])
        self.assertTrue(overlay["reason"])
        self.assertEqual([], overlay["remedy"])

    def test_a_mechanism_that_exists_but_could_not_be_used_names_what_to_fix(self) -> None:
        # Plasma is present; only the display is missing -- the "exists but
        # could not be used" scenario, and the one thing this report must not
        # do here is send someone after KDE Plasma they already have.
        headless_plasma = PlatformProfile(
            operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA
        )
        report = platform_diagnostics(headless_plasma)
        overlay = report["concerns"]["overlay"]

        self.assertFalse(overlay["available"])
        self.assertTrue(overlay["remedy"])

    def test_every_concern_is_present_on_every_platform(self) -> None:
        for profile in (
            PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", desktop=Desktop.PLASMA),
            PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64"),
            PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64"),
        ):
            with self.subTest(operating_system=profile.operating_system):
                report = platform_diagnostics(profile)
                self.assertEqual(set(BACKEND_REGISTRIES), set(report["concerns"]))

    def test_a_permission_that_cannot_be_read_is_undetermined_not_granted(self) -> None:
        cannot_tell = Permission(
            name="test_permission",
            capability="test capability",
            grant_location="Settings > Test",
            check=lambda profile: PermissionState.UNDETERMINED,
        )
        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")

        with patch("murmly.cli.PERMISSIONS", {"test_permission": cannot_tell}):
            report = platform_diagnostics(profile)

        self.assertEqual("undetermined", report["permissions"]["test_permission"]["state"])
        self.assertNotEqual("granted", report["permissions"]["test_permission"]["state"])

    def test_a_permission_check_that_raises_is_undetermined_not_granted(self) -> None:
        """18.13: a check that fails to run is exactly as uninformative about
        the grant as a platform offering no way to read it -- neither may be
        reported as granted."""

        def _raises(profile: PlatformProfile) -> PermissionState:
            raise RuntimeError("could not read the grant")

        broken = Permission(
            name="test_permission",
            capability="test capability",
            grant_location="Settings > Test",
            check=_raises,
        )
        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")

        with patch("murmly.cli.PERMISSIONS", {"test_permission": broken}):
            report = platform_diagnostics(profile)

        permission_report = report["permissions"]["test_permission"]
        self.assertEqual("undetermined", permission_report["state"])
        self.assertNotEqual("granted", permission_report["state"])
        self.assertIn("could not read the grant", permission_report["detail"])

    def test_macos_paste_injection_concern_is_anded_with_the_accessibility_grant(self) -> None:
        """`macos` is a genuinely registered `paste_injection` candidate
        (`BackendChoice.available` is `True` on its own), but the concern's
        own `available` must still fold in whether the Accessibility grant
        this mechanism needs has actually been given -- a present mechanism
        with a denied permission is not a usable concern."""
        from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, Permission

        profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
        denied = Permission(
            name=MACOS_ACCESSIBILITY_PERMISSION,
            capability="paste injection",
            grant_location="System Settings > Privacy & Security > Accessibility",
            check=lambda profile: PermissionState.DENIED,
        )

        with patch("murmly.cli.PERMISSIONS", {MACOS_ACCESSIBILITY_PERMISSION: denied}):
            report = platform_diagnostics(profile)

        paste_injection = report["concerns"]["paste_injection"]
        self.assertFalse(paste_injection["available"])
        self.assertIn("Accessibility", paste_injection["reason"])
        self.assertTrue(paste_injection["remedy"])

    def test_macos_paste_injection_concern_is_available_when_granted(self) -> None:
        from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, Permission

        profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
        granted = Permission(
            name=MACOS_ACCESSIBILITY_PERMISSION,
            capability="paste injection",
            grant_location="System Settings > Privacy & Security > Accessibility",
            check=lambda profile: PermissionState.GRANTED,
        )

        with patch("murmly.cli.PERMISSIONS", {MACOS_ACCESSIBILITY_PERMISSION: granted}):
            report = platform_diagnostics(profile)

        paste_injection = report["concerns"]["paste_injection"]
        self.assertTrue(paste_injection["available"])
        self.assertEqual("macos", paste_injection["mechanism"])

    def test_macos_paste_injection_concern_is_unavailable_when_undetermined(self) -> None:
        """18.13's own rule, applied to a concern rather than only to a
        `permissions` entry: an unreadable grant must never be reported as an
        available concern either."""
        from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, Permission

        profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
        cannot_tell = Permission(
            name=MACOS_ACCESSIBILITY_PERMISSION,
            capability="paste injection",
            grant_location="System Settings > Privacy & Security > Accessibility",
            check=lambda profile: PermissionState.UNDETERMINED,
        )

        with patch("murmly.cli.PERMISSIONS", {MACOS_ACCESSIBILITY_PERMISSION: cannot_tell}):
            report = platform_diagnostics(profile)

        self.assertFalse(report["concerns"]["paste_injection"]["available"])

    def test_non_macos_paste_injection_concern_is_untouched(self) -> None:
        """The AND only ever applies on macOS -- Windows' `paste_injection`
        concern (never permission-gated) must not gain a `reason`/`remedy`
        key it did not already have."""
        profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")

        report = platform_diagnostics(profile)

        self.assertTrue(report["concerns"]["paste_injection"]["available"])
        self.assertNotIn("reason", report["concerns"]["paste_injection"])


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
            config_path.write_text("", encoding="utf-8")
            with (
                patch("murmly.cli.send_command_with_recovery", fake_send),
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
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
            config_path.write_text("", encoding="utf-8")
            with (
                patch("murmly.cli.send_command_with_recovery", fake_send),
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
                ),
                redirect_stdout(StringIO()),
            ):
                main(["--config", str(config_path), "toggle"])

        self.assertEqual(["toggle"], sent)


def _sounddevice_with_devices(devices: list[dict[str, object]]) -> ModuleType:
    module = ModuleType("sounddevice")
    module.query_devices = lambda: devices
    return module


def _sounddevice_raising(error: Exception) -> ModuleType:
    def query_devices():
        raise error

    module = ModuleType("sounddevice")
    module.query_devices = query_devices
    return module


_INPUT_DEVICE = {
    "name": "Built-in Microphone",
    "max_input_channels": 1,
    "max_output_channels": 0,
    "default_samplerate": 48_000,
}
_OUTPUT_ONLY_DEVICE = {
    "name": "Built-in Speakers",
    "max_input_channels": 0,
    "max_output_channels": 2,
    "default_samplerate": 48_000,
}

_LINUX_PROFILE = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
_MACOS_PROFILE = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")


def _fake_microphone_permission(state: PermissionState, error: Exception | None = None) -> Permission:
    def check(profile: PlatformProfile) -> PermissionState:
        if error is not None:
            raise error
        return state

    return Permission(
        name="test-microphone-permission",
        capability="microphone capture",
        grant_location="System Settings > Privacy & Security > Microphone",
        check=check,
    )


class MicrophoneDiagnosticsTests(unittest.TestCase):
    """`microphone_diagnostics`, task 12.5: distinguishing a denied
    microphone from an absent device, which are identical from inside the
    capture path itself (design.md's "the whole difficulty")."""

    def test_a_device_present_on_a_platform_with_no_gating_permission_is_available(self) -> None:
        with injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])):
            report = microphone_diagnostics(_LINUX_PROFILE)

        self.assertEqual(
            {"device_present": True, "permission": None, "available": True},
            report,
        )

    def test_no_input_device_is_reported_unavailable_naming_the_absence(self) -> None:
        with injected_module("sounddevice", _sounddevice_with_devices([_OUTPUT_ONLY_DEVICE])):
            report = microphone_diagnostics(_LINUX_PROFILE)

        self.assertFalse(report["device_present"])
        self.assertFalse(report["available"])
        self.assertIn("no microphone input device", report["detail"].casefold())

    def test_a_device_query_failure_is_undetermined_not_a_claim_of_absence_or_grant(self) -> None:
        with injected_module("sounddevice", _sounddevice_raising(RuntimeError("no host api"))):
            report = microphone_diagnostics(_LINUX_PROFILE)

        self.assertIsNone(report["device_present"])
        self.assertFalse(report["available"])
        self.assertIn("no host api", report["device_detail"])

    def test_a_denied_permission_with_a_device_present_is_reported_denied(self) -> None:
        permission = _fake_microphone_permission(PermissionState.DENIED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertTrue(report["device_present"])
        self.assertEqual("denied", report["permission"]["state"])
        self.assertFalse(report["available"])
        self.assertIn(permission.grant_location, report["detail"])

    def test_an_undetermined_permission_is_reported_unavailable_never_granted(self) -> None:
        permission = _fake_microphone_permission(PermissionState.UNDETERMINED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertEqual("undetermined", report["permission"]["state"])
        self.assertFalse(report["available"])

    def test_a_permission_check_that_raises_is_undetermined_not_granted(self) -> None:
        permission = _fake_microphone_permission(PermissionState.GRANTED, error=RuntimeError("boom"))
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertEqual("undetermined", report["permission"]["state"])
        self.assertIn("boom", report["permission"]["detail"])
        self.assertFalse(report["available"])

    def test_no_device_takes_priority_over_a_denied_permission(self) -> None:
        """The scenario a headless macOS CI runner actually is: no
        microphone hardware and a permission this reads as denied (or
        undetermined -- its TCC state is not a normal user's either). The
        report MUST say "no device", never "denied": claiming a denial would
        assert a working microphone exists but is locked away, when the
        truer fact is there is nothing to lock."""
        permission = _fake_microphone_permission(PermissionState.DENIED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_OUTPUT_ONLY_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertFalse(report["available"])
        self.assertIn("no microphone input device", report["detail"].casefold())
        self.assertNotIn("denied", report["detail"].casefold())
        # The permission's own state is still reported, just never allowed to
        # override the device-absence detail.
        self.assertEqual("denied", report["permission"]["state"])

    def test_granted_permission_and_a_present_device_on_macos_is_available_but_flagged_unverified(
        self,
    ) -> None:
        """Task 12.6: every readable fact can be good on macOS and this must
        still not claim end-to-end capture works -- a launchd-started daemon
        can have both and still receive nothing (design.md's largest risk,
        tasks 12.1-12.4, none of them provable from here)."""
        permission = _fake_microphone_permission(PermissionState.GRANTED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertTrue(report["available"])
        self.assertIn("not yet verified end to end", report["detail"])

    def test_the_report_shape_is_the_same_key_set_across_every_outcome(self) -> None:
        """18.17's rule applied to this section: the fields present must not
        change with the outcome, only their values -- `device_present`,
        `permission` and `available` are always present; `detail` and
        `device_detail` are the only conditional keys, the same convention
        every other diagnostics section already uses."""
        permission = _fake_microphone_permission(PermissionState.GRANTED)
        base_keys = {"device_present", "permission", "available"}
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=None),
        ):
            clean_report = microphone_diagnostics(_LINUX_PROFILE)
        self.assertEqual(base_keys, set(clean_report))

        with (
            injected_module("sounddevice", _sounddevice_with_devices([_OUTPUT_ONLY_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            absent_report = microphone_diagnostics(_MACOS_PROFILE)
        self.assertEqual(base_keys | {"detail"}, set(absent_report))


def _assert_never_conflates_absence_with_denial(
    case: unittest.TestCase, report: dict[str, object]
) -> None:
    """Task 12.5's own difficulty: an absent device and a denied permission
    are indistinguishable from inside the capture path, so the report must
    resolve that ambiguity itself rather than passing it through. Whenever a
    device was not confirmed present, the detail must never say "denied" --
    that would claim a working microphone exists but is locked away, when
    the truer fact may be there is nothing to lock.

    A module-level helper, not a `TestCase` method, shared between
    `MicrophoneDiagnosticsInvariantTests`'s injected cases and
    `MicrophoneDiagnosticsRuntimeIntegrationTests`'s real one: the two
    classes assert the same guarantee against different inputs and neither
    is a specialisation of the other, so subclassing one from the other
    would double-run the injected cases as skipped copies under the runtime
    class for no benefit."""
    if report["device_present"] is not True:
        case.assertNotIn("denied", report.get("detail", "").casefold(), f"full report: {report!r}")


def _assert_never_claims_launchd_capture_is_verified(
    case: unittest.TestCase, report: dict[str, object]
) -> None:
    """Task 12.6: whatever the permission says, a granted TCC state and an
    enumerable device are still not proof a launchd-started daemon receives
    audio (design.md's largest risk, tasks 12.1-12.4, none of them provable
    from a diagnostics report). So whenever the report claims the microphone
    is available on macOS, the "not yet verified end to end" disclaimer must
    be the reason given -- the claim that must never leak on its own is
    `available: true` standing for confirmed end-to-end capture."""
    if report["available"] and report.get("permission") is not None:
        case.assertIn(
            "not yet verified end to end", report.get("detail", ""), f"full report: {report!r}"
        )


class MicrophoneDiagnosticsInvariantTests(unittest.TestCase):
    """Task 12.5's two guarantees, checked against `microphone_diagnostics`'s
    actual output rather than against one assumed device/permission state.

    These used to be pinned only by `MicrophoneDiagnosticsRuntimeIntegrationTests`,
    against the real machine's `sounddevice` and TCC state, on the assumption
    that a GitHub macOS runner is headless: no microphone device and no
    ordinary user's grant. The first real run of this code on an actual Mac
    (2026-08-31 CI) showed that assumption wrong -- the runner has an audio
    device and TCC already reports the microphone granted. A test that
    asserts "no device, never granted" against a machine that has both a
    device and a grant is not proving the guarantee failed; it is proving the
    test's premise about the runner was never true. `microphone_diagnostics`
    reported correctly, in both directions of that mistaken premise --
    `device_present: True`, `permission.state: "granted"`, `available: True`
    -- so the fault was the test's assumed inputs, not the function's output.

    So the guarantees are asserted here as invariants over the report's
    actual fields, true regardless of what a given Mac's device and grant
    state turn out to be, and checked against every reachable combination
    through injected `sounddevice`/permission fakes -- deviceless, denied,
    undetermined and granted -- rather than against whichever one combination
    a given CI runner happens to have on a given day.
    """

    def test_a_deviceless_runner_never_reports_denied(self) -> None:
        permission = _fake_microphone_permission(PermissionState.DENIED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_OUTPUT_ONLY_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        _assert_never_conflates_absence_with_denial(self, report)
        _assert_never_claims_launchd_capture_is_verified(self, report)
        self.assertFalse(report["available"])

    def test_a_device_query_failure_never_reports_denied(self) -> None:
        with (
            injected_module("sounddevice", _sounddevice_raising(RuntimeError("no host api"))),
            patch("murmly.cli.microphone_permission_for", return_value=None),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        _assert_never_conflates_absence_with_denial(self, report)
        _assert_never_claims_launchd_capture_is_verified(self, report)
        self.assertFalse(report["available"])

    def test_a_denied_permission_with_a_device_present_renders_as_denied(self) -> None:
        permission = _fake_microphone_permission(PermissionState.DENIED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertEqual("denied", report["permission"]["state"])
        _assert_never_claims_launchd_capture_is_verified(self, report)
        self.assertFalse(report["available"])

    def test_an_undetermined_permission_with_a_device_present_renders_as_undetermined(self) -> None:
        permission = _fake_microphone_permission(PermissionState.UNDETERMINED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertEqual("undetermined", report["permission"]["state"])
        _assert_never_conflates_absence_with_denial(self, report)
        _assert_never_claims_launchd_capture_is_verified(self, report)
        self.assertFalse(report["available"])

    def test_a_granted_permission_with_a_device_present_renders_as_granted_but_unverified(
        self,
    ) -> None:
        permission = _fake_microphone_permission(PermissionState.GRANTED)
        with (
            injected_module("sounddevice", _sounddevice_with_devices([_INPUT_DEVICE])),
            patch("murmly.cli.microphone_permission_for", return_value=permission),
        ):
            report = microphone_diagnostics(_MACOS_PROFILE)

        self.assertEqual("granted", report["permission"]["state"])
        _assert_never_conflates_absence_with_denial(self, report)
        _assert_never_claims_launchd_capture_is_verified(self, report)
        self.assertTrue(report["available"])


class MicrophoneDiagnosticsRuntimeIntegrationTests(unittest.TestCase):
    """Task 12.5, against the real machine: `MicrophoneDiagnosticsTests` and
    `MicrophoneDiagnosticsInvariantTests` above prove the priority logic and
    its two invariants against injected fakes for `sounddevice.query_devices`
    and the permission check, covering every reachable combination
    deliberately. This calls `microphone_diagnostics` with no injection at
    all, against whatever `resolve_platform()` and the real `sounddevice`
    report on the runner it executes on, and checks the same two invariants
    against whatever combination that runner actually has.

    Follows the same convention as `MacosMicrophonePermissionRuntimeIntegrationTests`
    and `WindowsMicrophonePermissionRuntimeIntegrationTests`: skip in `setUp`
    on every platform but the one this proves something on, then assert only
    what a real, unknown-state runner can prove, with the full report printed
    into every failure message so a real run's actual facts land in the CI
    log even when the test passes.

    What this class does NOT do any more: assume the runner is headless. The
    first real run of this code on an actual GitHub macOS runner (2026-08-31)
    showed it has a microphone device and TCC already reports it granted --
    not the deviceless, ungranted machine this test was originally written
    for. That runner state is itself informative: it means this class can
    prove the granted/present branch end to end on real hardware, but it
    cannot exercise the deviceless or denied branches -- those stay covered
    only by `MicrophoneDiagnosticsInvariantTests`'s injected cases above, and
    task 12.6's actual question -- whether a launchd-started daemon receives
    audio at all -- remains unanswered by any of tasks 12.1-12.5's
    diagnostics, on this runner or any other; that is what tasks 12.1-12.4
    exist to establish separately.
    """

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("This proves what the real macOS CI runner reports, not a fake")

    def test_the_runner_never_conflates_device_absence_with_denial(self) -> None:
        report = microphone_diagnostics(resolve_platform())

        _assert_never_conflates_absence_with_denial(self, report)

    def test_the_runner_never_claims_launchd_capture_is_verified(self) -> None:
        report = microphone_diagnostics(resolve_platform())

        _assert_never_claims_launchd_capture_is_verified(self, report)

    def test_the_runner_reports_a_recognised_permission_state_or_none(self) -> None:
        report = microphone_diagnostics(resolve_platform())
        permission = report["permission"]

        if permission is not None:
            self.assertIn(
                permission["state"],
                {state.value for state in PermissionState},
                f"full report: {report!r}",
            )


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
            config_path.write_text('[tts]\nvoice = "morgan_freeman"\nrate = 900\n', encoding="utf-8")
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
            config_path.write_text("[tts]\nrate = 1979-05-27\n", encoding="utf-8")
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

    def test_a_default_install_reports_the_cpu_as_the_processor_in_use(self) -> None:
        """Through the real probe, which is what `murmly doctor` constructs.

        The probe holds no session, so the processor named is the one
        resolution would choose -- and an unconfigured `[tts] device` means the
        CPU, which resolution returns before it reaches the CUDA preload.
        """
        from murmly.cli import speech_output_diagnostics
        from murmly.tts import CPU_PROVIDER, MODEL_FILE_NAME, VOICES_FILE_NAME, KokoroSynthesizer

        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True)
            config.tts_model_dir.mkdir(parents=True, exist_ok=True)
            for name in (MODEL_FILE_NAME, VOICES_FILE_NAME):
                (config.tts_model_dir / name).write_bytes(b"")
            with (
                patch("importlib.util.find_spec", return_value=object()),
                patch("murmly.tts.resolve_espeak", return_value=("libespeak-ng.so.1", "/data")),
                patch(
                    "murmly.cli.negotiated_output",
                    return_value=(48_000, "Studio Speakers", None, None),
                ),
                # The weights are 326 MB and the report is asked for on a
                # machine that may be about to explain why speech does not
                # work. Reporting the processor must not be what loads them.
                patch.object(KokoroSynthesizer, "_load_model") as load_model,
            ):
                report = speech_output_diagnostics(config)

        self.assertEqual("cpu", report["device"])
        self.assertEqual("cpu", report["device_in_use"])
        self.assertEqual([CPU_PROVIDER], report["providers"])
        self.assertNotIn("device_detail", report, "nothing fell back")
        load_model.assert_not_called()

    def test_coreml_is_named_rather_than_folded_into_cpu(self) -> None:
        """Task 15.4: `device_in_use` must not hide macOS's own accelerator.

        `CoreMLExecutionProvider` is not a value `[tts] device` accepts (only
        `auto`'s resolution ever selects it), so this reads `_processor_name`
        directly rather than through a doctor report compared against a
        device setting -- `report["device"]` stays `"auto"` regardless of
        which provider actually ran.
        """
        from murmly.cli import _processor_name
        from murmly.tts import COREML_PROVIDER, CPU_PROVIDER, CUDA_PROVIDER

        self.assertEqual("coreml", _processor_name(COREML_PROVIDER))
        self.assertEqual("cuda", _processor_name(CUDA_PROVIDER))
        self.assertEqual("cpu", _processor_name(CPU_PROVIDER))

    def test_an_unrecognized_processor_is_reported_alongside_the_one_in_use(self) -> None:
        """The one in use here is the default the unrecognized value fell back to.

        Read out of the configuration rather than out of a probe, so it is in
        the report on a disabled install too -- which is where a setting nobody
        can see taking effect is most likely to be sitting.
        """
        from murmly.cli import speech_output_diagnostics
        from murmly.config import load_config

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[tts]\ndevice = "rocm"\n', encoding="utf-8")
            report = speech_output_diagnostics(load_config(config_path))

        self.assertEqual("cpu", report["device"])
        self.assertEqual("rocm", report["device_rejected_value"])
        json.dumps(report)

    def test_an_accelerator_that_cannot_be_used_is_reported_with_its_remedy(self) -> None:
        """The spec's fall-back scenario, answered by the report on its own.

        `resolve_providers` logs the remedy, and the report carries the same
        text: someone reading JSON out of `murmly doctor` should not have to
        correlate it with what went to standard error to find out what to do.
        """
        from murmly.cli import speech_output_diagnostics
        from murmly.tts import CPU_PROVIDER
        from fakes import FakeSynthesizer

        probe = FakeSynthesizer()
        # A synthesizer that has not spoken holds no session, which is the state
        # every probe `murmly doctor` builds is in.
        probe.provider = None
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True, tts_device="cuda")
            with (
                # A runtime build without the CUDA provider, stood in for so the
                # assertion does not depend on which build is installed.
                patch("onnxruntime.get_available_providers", return_value=[CPU_PROVIDER]),
                patch(
                    "murmly.cli.negotiated_output",
                    return_value=(48_000, "Studio Speakers", None, None),
                ),
                self.assertLogs("murmly.tts", level="WARNING"),
            ):
                report = speech_output_diagnostics(config, probe)

        self.assertEqual("cuda", report["device"])
        self.assertEqual("cpu", report["device_in_use"])
        self.assertEqual([CPU_PROVIDER], report["providers"])
        self.assertIn("onnxruntime-gpu", report["device_detail"])
        # Speech still works; it is the accelerator that is unavailable, and a
        # report calling speech unavailable would say the opposite.
        self.assertTrue(report["available"])

    def test_the_processor_in_use_comes_off_the_session_and_not_the_runtime(self) -> None:
        """`get_available_providers()` advertises CUDA on a session that fell back.

        Resolution sees a runtime carrying the provider and libraries that load,
        so it asks for the accelerator and has no way to know the session did
        not get it. Only the session knows, so the session is what is read. See
        docs/agent-notes/onnxruntime-gpu-cuda-version.md.
        """
        from murmly.cli import speech_output_diagnostics
        from murmly.tts import CPU_PROVIDER, CUDA_PROVIDER
        from fakes import FakeSynthesizer

        probe = FakeSynthesizer()
        probe.provider = CPU_PROVIDER
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True, tts_device="cuda")
            with (
                patch(
                    "onnxruntime.get_available_providers",
                    return_value=[CUDA_PROVIDER, CPU_PROVIDER],
                ),
                patch("murmly.tts.load_cuda_libraries", return_value=True),
                patch(
                    "murmly.cli.negotiated_output",
                    return_value=(48_000, "Studio Speakers", None, None),
                ),
            ):
                report = speech_output_diagnostics(config, probe)

        self.assertEqual([CUDA_PROVIDER, CPU_PROVIDER], report["providers"])
        self.assertEqual("cpu", report["device_in_use"])
        self.assertIn(CPU_PROVIDER, report["device_detail"])

    def test_a_runtime_that_cannot_be_resolved_still_produces_a_report(self) -> None:
        """The processor is one section of the report, not a precondition for it.

        A half-installed ONNX Runtime is the condition someone runs `murmly
        doctor` to have explained, so the reason belongs in the report rather
        than in place of it.
        """
        from murmly.cli import speech_output_diagnostics
        from fakes import FakeSynthesizer

        probe = FakeSynthesizer()
        probe.provider = None
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir, tts_enabled=True)
            with (
                patch(
                    "murmly.cli.resolve_providers",
                    side_effect=RuntimeError("the ONNX Runtime is missing or unusable"),
                ),
                patch(
                    "murmly.cli.negotiated_output",
                    return_value=(48_000, "Studio Speakers", None, None),
                ),
            ):
                report = speech_output_diagnostics(config, probe)

        self.assertIsNone(report["providers"])
        self.assertIsNone(report["device_in_use"])
        self.assertIn("unusable", report["provider_detail"])
        self.assertEqual(48_000, report["negotiated_output_rate_hz"], "a later section was lost")

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


class StubModelHolder:
    """A synthesis probe that records anything that would have loaded weights.

    Written out rather than reached for with `Mock`, for two reasons that both
    bite here. A `Mock` reports every property truthy by accident, so a report
    built on one passes against an implementation that never read anything. A
    `Mock` also reaches the report itself, where `json.dumps` refuses it.

    It has no `resident`: the probe's own residency is not what the report says
    any more. The session this report is about lives in the daemon, and asking a
    probe built here could only ever answer for this process.
    """

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.unavailable_reason = None if available else "kokoro-onnx is not installed."
        self.provider = "CPUExecutionProvider"
        self.spoken: list[str] = []

    def synthesize(self, text: str):
        # The synthesis session is built here and nowhere else, so a report that
        # never reaches this is a report that loaded no weights.
        self.spoken.append(text)
        return iter(())


class StubDaemonSession:
    """The capture session a served daemon is given, answering residency alone.

    `MurmlyDaemon` would otherwise build a real `SpeechSession`, which
    constructs a transcriber and opens a capture device. The daemon suite has
    its own fuller stand-in; this is the part `murmly doctor` reaches.
    """

    def __init__(self, *, model_resident: bool = False) -> None:
        self.model_resident = model_resident

    def capture_delivery_target(self):
        return None

    def start_recording(self) -> None:
        raise AssertionError("the report must not start a recording")

    def stop_recording(self) -> bytes:
        return b""

    def release_model(self) -> None:
        raise AssertionError("the report must not release the daemon's model")


def answering(response: dict[str, object]):
    """A `send_command` stand-in for a daemon that answers with `response`."""

    def send(_socket_path: str, _command: str) -> dict[str, object]:
        return response

    return send


def refusing(error: BaseException):
    """A `send_command` stand-in for a daemon that cannot be reached or asked."""

    def send(_socket_path: str, _command: str) -> dict[str, object]:
        raise error

    return send


class ModelResidencyDiagnosticsTests(unittest.TestCase):
    """`murmly doctor` reports what the daemon holds, and loads nothing to do it.

    Doctor runs in its own process and holds neither model, so its own residency
    is a constant that says nothing about the system. Everything it can report
    comes over the command socket, and "nobody answered" is a different fact
    from "nothing is held".
    """

    def _config(self, **overrides: object) -> MurmlyConfig:
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        return MurmlyConfig(
            # Host-appropriate: most callers below patch `send_command`
            # outright and never touch this address at all, but the two that
            # don't -- the real "no daemon answers" and "a daemon really is
            # there" cases -- need the transport this host actually has, not
            # a filesystem path Windows would refuse before ever reaching it.
            socket_path=command_channel_address(temp_dir),
            config_path=Path(temp_dir) / "config.toml",
            tts_model_dir=Path(temp_dir) / "models",
            **overrides,
        )

    def _report(self, config: MurmlyConfig) -> dict[str, object]:
        """Run the command and read the report back the way a program would.

        Through `json.loads` rather than off the dictionary the code built,
        because the report is serialised before anyone sees it and a residency
        value JSON cannot encode is a report nobody receives.
        """
        with (
            patch("murmly.cli.choose_clipboard_copy_command", return_value=["xclip"]),
            patch(
                "murmly.cli.select_paste_injection",
                return_value=PasteInjection("xdotool", ("xdotool", "key", "ctrl+v")),
            ),
            redirect_stdout(StringIO()) as output,
        ):
            _run_doctor(config)
        return json.loads(output.getvalue())

    def test_a_daemon_holding_both_models_is_reported_as_holding_them(self) -> None:
        config = self._config(tts_enabled=True)
        held = answering(
            {"ok": True, "state": "IDLE", "model_resident": True, "synthesis_resident": True}
        )

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", held),
            patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Speakers", None, None),
            ),
            patch("murmly.cli.KokoroSynthesizer", return_value=StubModelHolder()),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIs(True, report["model_resident"])
        self.assertIs(True, report["speech_output"]["resident"])
        self.assertNotIn("model_resident_detail", report)
        self.assertNotIn("resident_detail", report["speech_output"])
        # The whole of the no-side-effect guarantee for transcription: the report
        # answered without a transcriber ever being constructed, which is the
        # only way it could have loaded the weights it was asked about.
        transcriber.assert_not_called()

    def test_a_daemon_that_has_released_its_models_reports_them_released(self) -> None:
        # The distinction the constant `False` could not make: a released model
        # and a held one have to read differently.
        config = self._config(tts_enabled=True)
        released = answering(
            {"ok": True, "state": "IDLE", "model_resident": False, "synthesis_resident": False}
        )

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", released),
            patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Speakers", None, None),
            ),
            patch("murmly.cli.KokoroSynthesizer", return_value=StubModelHolder()),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIs(False, report["model_resident"])
        self.assertEqual(300, report["unload_after_idle_s"])
        self.assertIs(False, report["speech_output"]["resident"])
        self.assertEqual(0, report["speech_output"]["unload_after_idle_s"])

    def test_no_daemon_is_reported_as_unanswerable_and_never_as_released(self) -> None:
        # The socket path in a fresh temporary directory has no daemon behind it,
        # which is the real case rather than a simulated one.
        config = self._config(tts_enabled=True)

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Speakers", None, None),
            ),
            patch("murmly.cli.KokoroSynthesizer", return_value=StubModelHolder()),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIsNone(report["model_resident"])
        self.assertIn("No Murmly daemon is running", report["model_resident_detail"])
        self.assertIsNone(report["speech_output"]["resident"])
        self.assertIn("No Murmly daemon is running", report["speech_output"]["resident_detail"])
        # Both periods are still named: a reader has to be able to tell a model
        # that will never be released from one this report did not mention.
        self.assertEqual(300, report["unload_after_idle_s"])
        self.assertEqual(0, report["speech_output"]["unload_after_idle_s"])

    def test_reporting_never_starts_the_daemon_to_get_an_answer(self) -> None:
        # `send_command_with_recovery` starts the installed service when nothing
        # answers. Using it here would have the report boot a daemon and then say
        # its models are idle, which inverts the distinction this section exists
        # to draw.
        config = self._config()
        recovery = Mock()

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command_with_recovery", recovery),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        recovery.assert_not_called()
        self.assertIsNone(report["model_resident"])

    def test_a_daemon_too_old_to_carry_the_fields_is_named_as_such(self) -> None:
        config = self._config()
        older = answering({"ok": True, "state": "IDLE"})

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", older),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIsNone(report["model_resident"])
        self.assertIn("does not report residency", report["model_resident_detail"])
        self.assertIsNone(report["speech_output"]["resident"])
        self.assertIn("does not report residency", report["speech_output"]["resident_detail"])

    def test_a_daemon_holding_no_synthesizer_reports_no_synthesis_residency(self) -> None:
        # Left out of the daemon's answer rather than reported false, because a
        # daemon with speech output off never built a session to hold. Reported
        # from the daemon's side rather than from the configuration read here:
        # the two disagree after an edit nothing has restarted for.
        config = self._config()
        speechless = answering({"ok": True, "state": "IDLE", "model_resident": True})

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", speechless),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIs(True, report["model_resident"])
        self.assertIsNone(report["speech_output"]["resident"])
        self.assertIn(
            "holds no synthesis session", report["speech_output"]["resident_detail"]
        )

    def test_a_daemon_that_cannot_read_its_own_holder_passes_its_reason_through(self) -> None:
        config = self._config()
        unaskable = answering(
            {
                "ok": True,
                "state": "IDLE",
                "model_resident": None,
                "model_resident_detail": (
                    "Unable to determine transcription residency: this build cannot "
                    "report residency"
                ),
            }
        )

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", unaskable),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIsNone(report["model_resident"])
        # The daemon's own reason, not one invented here: it knows why it could
        # not read its holder and this process does not.
        self.assertIn("this build cannot report residency", report["model_resident_detail"])

    def test_a_failed_query_does_not_abandon_the_report(self) -> None:
        config = self._config()

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", refusing(RuntimeError("the socket exploded"))),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIsNone(report["model_resident"])
        self.assertIn("the socket exploded", report["model_resident_detail"])
        for section in ("installation", "delivery", "overlay", "speech_output", "command_socket"):
            self.assertIn(section, report, f"the {section} section was abandoned")

    def test_a_daemon_that_does_not_answer_in_time_is_reported_rather_than_waited_for(
        self,
    ) -> None:
        config = self._config()

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch(
                "murmly.cli.send_command",
                refusing(DaemonNotRespondingError("Murmly daemon did not respond within 30 s.")),
            ),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        self.assertIsNone(report["model_resident"])
        self.assertIn("did not answer", report["model_resident_detail"])

    def test_a_served_daemon_is_asked_and_answers_over_the_real_socket(self) -> None:
        # The one test that puts both halves together. `daemon_residency` reads
        # keys the daemon writes, and nothing else checks that the two agree on
        # what they are called.
        config = self._config()
        daemon = MurmlyDaemon(
            config,
            session=StubDaemonSession(model_resident=True),
            overlay=_NullOverlay(),
        )
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(daemon.shutdown)
        # `.exists()` alone only ever proves the UNIX transport is ready: a
        # named pipe is never a filesystem node to begin with, so it would
        # never become true and this would just spin out the deadline. A
        # whole exchange is what both transports can prove readiness with.
        deadline = time.time() + 3
        while True:
            try:
                send_command(str(config.socket_path), "status")
                break
            except Exception:  # noqa: BLE001 - not up yet is the ordinary case here
                if time.time() >= deadline:
                    self.fail("daemon socket was not accepting connections")
                time.sleep(0.01)

        transcription, synthesis = daemon_residency(config)

        self.assertEqual((True, None), transcription)
        self.assertIsNone(synthesis[0])
        self.assertIn("holds no synthesis session", synthesis[1])

    def test_a_period_of_zero_is_reported_rather_than_left_out(self) -> None:
        # The reverse of the shipped defaults, so a report that names only the
        # enabled period cannot pass. A reader who cannot see the zero cannot
        # tell a model that will never be released from one nobody reported on.
        config = self._config(
            tts_enabled=True,
            unload_after_idle_s=0,
            tts_unload_after_idle_s=900,
        )

        with patch("murmly.cli.FasterWhisperTranscriber") as transcriber:
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            with patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Speakers", None, None),
            ):
                report = self._report(config)

        self.assertEqual(0, report["unload_after_idle_s"])
        self.assertEqual(900, report["speech_output"]["unload_after_idle_s"])

    def test_a_disabled_speech_stack_still_names_its_period_and_residency(self) -> None:
        from murmly.cli import speech_output_diagnostics

        report = speech_output_diagnostics(
            self._config(tts_unload_after_idle_s=900),
            residency=(False, None),
        )

        self.assertFalse(report["enabled"])
        # The daemon's answer, carried through the early return. Speech output
        # being off in the configuration read here does not decide what a daemon
        # started before that edit is holding.
        self.assertIs(False, report["resident"])
        self.assertEqual(900, report["unload_after_idle_s"])

    def test_reporting_on_an_enabled_speech_stack_builds_no_session(self) -> None:
        config = self._config(tts_enabled=True)
        holder = StubModelHolder()

        with (
            patch("murmly.cli.KokoroSynthesizer", return_value=holder) as synthesizer,
            patch(
                "murmly.cli.negotiated_output",
                return_value=(48_000, "Speakers", None, None),
            ),
        ):
            from murmly.cli import speech_output_diagnostics

            report = speech_output_diagnostics(config, residency=(True, None))

        self.assertIs(True, report["resident"])
        self.assertTrue(report["available"])
        synthesizer.assert_called_once_with(config)
        # Constructing the synthesizer is not the load. Building the session is,
        # and that happens on the first request for audio, which a report has no
        # reason to make.
        self.assertEqual([], holder.spoken)

    def test_a_speech_probe_that_fails_still_names_its_period_and_residency(self) -> None:
        config = self._config(tts_enabled=True, tts_unload_after_idle_s=900)
        held = answering(
            {"ok": True, "state": "IDLE", "model_resident": False, "synthesis_resident": True}
        )

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", held),
            patch(
                "murmly.cli.speech_output_diagnostics",
                side_effect=RuntimeError("the probe exploded"),
            ),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        speech = report["speech_output"]
        self.assertIn("the probe exploded", speech["detail"])
        # A section that drops these reads as one the report never asked about,
        # which is the distinction the residency requirement exists to preserve.
        # The daemon's answer was taken before the probe ran, and the probe
        # failing does not make it less true.
        self.assertEqual(900, speech["unload_after_idle_s"])
        self.assertIs(True, speech["resident"])

    def test_residency_is_asked_before_the_report_loads_anything(self) -> None:
        # The live transcription probe loads the model when live transcription
        # is enabled. Residency has to describe what the daemon held when the
        # question was put, not what this report loaded on its way past, so the
        # order the two run in is the guarantee rather than an accident of
        # layout.
        order: list[str] = []

        def residency(config: MurmlyConfig, send: object = None):
            order.append("residency")
            return (False, None), (False, None)

        def live(config: MurmlyConfig) -> dict[str, object]:
            order.append("live transcription")
            return {}

        config = self._config(live_transcribe=True)
        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.daemon_residency", residency),
            patch("murmly.cli.live_transcription_diagnostics", live),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            self._report(config)

        self.assertEqual(["residency", "live transcription"], order)

    def test_the_measurement_declares_that_it_loaded_a_model(self) -> None:
        # Reporting residency loads nothing. This section does, and the report
        # has to say so rather than leave a reader to infer it from a docstring.
        config = self._config(live_transcribe=True)
        held = answering({"ok": True, "state": "IDLE", "model_resident": True})

        with (
            patch("murmly.cli.FasterWhisperTranscriber") as transcriber,
            patch("murmly.cli.send_command", held),
            patch("murmly.cli.negotiated_capture_rate", return_value=(16_000, None)),
            patch("murmly.cli.measure_partial_pass_ms", return_value=(120, None)),
        ):
            transcriber.resolve_runtime.return_value = ("cpu", "int8")
            report = self._report(config)

        live = report["live_transcription"]
        self.assertIs(True, live["partial_pass_loaded_model"])
        self.assertIn("loaded the transcription model", live["partial_pass_detail"])
        # And the residency beside it is what the daemon held before that ran.
        self.assertIs(True, report["model_resident"])

    def test_a_report_that_measures_nothing_declares_no_load(self) -> None:
        report = live_transcription_diagnostics(self._config(live_transcribe=False))

        self.assertIs(False, report["partial_pass_loaded_model"])


class _NullOverlay:
    """An overlay that does nothing, so a served daemon needs no display."""

    @property
    def health(self):
        return OverlayHealth(True)

    def publish_state(self, state) -> None:
        pass

    def publish_level(self, level: float) -> None:
        pass

    def publish_partial(self, text: str) -> None:
        pass

    def publish_error(self, duration_ms: int = 2_000) -> None:
        pass

    def close(self) -> None:
        pass


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
            # A TOML literal string (single quotes), not a basic one: a
            # Windows temp path is full of backslashes, and a basic string
            # feeds each one to TOML's own escape processing -- most letters
            # following it are not a recognised escape at all, which is a
            # parse error ("Invalid hex value") `load_config` inside `main()`
            # then reports as this command's own failure before `doctor` is
            # ever reached.
            config_path.write_text(
                f"[daemon]\nsocket_path = '{Path(temp_dir) / 'murmly.sock'}'\n",
                encoding="utf-8",
            )
            with (
                patch("murmly.cli.disable_portaudio_exit_teardown") as disable,
                patch.object(
                    FasterWhisperTranscriber,
                    "resolve_runtime",
                    return_value=("cpu", "int8"),
                ),
                patch(
                    "murmly.cli.resolve_platform",
                    return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
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

    def setUp(self) -> None:
        # Every test below but the two exercising `leave_without_finalizing`
        # directly goes through `main()`, which resolves the real platform
        # before any of the mocked-out daemon behaviour below ever runs.
        # Pinned to Linux so that gate keeps agreeing to run this suite's own
        # daemon-exit tests on every host, exactly as it already does when
        # this suite happens to run on Linux for real.
        patcher = patch(
            "murmly.cli.resolve_platform",
            return_value=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_file(self, temp_dir: str) -> Path:
        config_path = Path(temp_dir) / "config.toml"
        # A TOML literal string -- see
        # `DaemonExitTeardownTests.test_a_command_other_than_the_daemon_leaves_the_teardown_alone`
        # for why a basic (double-quoted) string cannot hold a Windows path.
        config_path.write_text(
            f"[daemon]\nsocket_path = '{Path(temp_dir) / 'murmly.sock'}'\n",
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

    def test_a_base_exception_still_reaches_the_exit(self) -> None:
        """`except Exception` catches neither of these, so before the exit moved
        into a `finally` they escaped main() and the process left through
        Py_Finalize. Ctrl-C is how the daemon is run while developing."""
        for raiser, expected in (
            (KeyboardInterrupt, 130),
            (SystemExit, 1),
        ):
            with self.subTest(raiser=raiser.__name__):
                with tempfile.TemporaryDirectory() as temp_dir:
                    config_path = self._config_file(temp_dir)
                    with (
                        patch("murmly.cli._run_daemon", side_effect=raiser("stopped")),
                        patch("murmly.cli.leave_without_finalizing") as leave,
                        redirect_stdout(StringIO()),
                        redirect_stderr(StringIO()),
                    ):
                        with self.assertRaises(raiser):
                            main(["--config", str(config_path), "daemon"])

                leave.assert_called_once_with(expected)

    def test_a_base_exception_outside_the_daemon_is_left_alone(self) -> None:
        """Only the daemon is in the configuration that makes finalization unsafe."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self._config_file(temp_dir)
            with (
                patch("murmly.cli._run_doctor", side_effect=KeyboardInterrupt()),
                patch("murmly.cli.leave_without_finalizing") as leave,
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    main(["--config", str(config_path), "doctor"])

        leave.assert_not_called()

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
