from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from murmly.installer import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    WINDOWS_TASK_NAME,
    InstallError,
    LaunchdUserService,
    UserService,
    WindowsUserService,
    macos_launchd_agent_plist,
    resolve_entrypoint,
    service_unit_text,
    write_atomically,
)
from murmly.integrations import PasteInjection


class FakeSystemctl:
    """Records systemctl invocations and replays scripted results."""

    def __init__(self, failures: dict[str, int] | None = None, stdout: dict[str, str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._failures = failures or {}
        self._stdout = stdout or {}

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        verb = command[2] if len(command) > 2 else ""
        return subprocess.CompletedProcess(
            args=command,
            returncode=self._failures.get(verb, 0),
            stdout=self._stdout.get(verb, ""),
            stderr="boom" if verb in self._failures else "",
        )

    @property
    def verbs(self) -> list[str]:
        return [call[2] for call in self.calls if len(call) > 2]


def make_entrypoint(directory: Path) -> Path:
    """A stand-in console script beside a stand-in interpreter."""
    interpreter = directory / "python3"
    interpreter.write_text("", encoding="utf-8")
    entrypoint = directory / "murmly"
    entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    return interpreter


class EntrypointResolutionTests(unittest.TestCase):
    def test_resolves_console_script_beside_the_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = make_entrypoint(Path(temp_dir))

            resolved = resolve_entrypoint(str(interpreter))

            self.assertEqual(Path(temp_dir).resolve() / "murmly", resolved)
            self.assertTrue(resolved.is_absolute())

    def test_missing_entrypoint_names_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python3"
            interpreter.write_text("", encoding="utf-8")

            with self.assertRaises(InstallError) as raised:
                resolve_entrypoint(str(interpreter))

            self.assertIn("murmly", str(raised.exception))
            self.assertIn(temp_dir, str(raised.exception))

    def test_non_executable_entrypoint_is_refused(self) -> None:
        if not hasattr(os, "getuid"):
            # `os.access(path, os.X_OK)` -- what `resolve_entrypoint` refuses
            # on -- treats every file as executable on Windows, which has no
            # POSIX execute bit for `chmod(0o644)` to clear in the first
            # place.
            self.skipTest("needs a POSIX execute bit, which os.access(X_OK) does not check on Windows")
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python3"
            interpreter.write_text("", encoding="utf-8")
            entrypoint = Path(temp_dir) / "murmly"
            entrypoint.write_text("", encoding="utf-8")
            entrypoint.chmod(0o644)

            with self.assertRaises(InstallError) as raised:
                resolve_entrypoint(str(interpreter))

            self.assertIn("not executable", str(raised.exception))

    def test_an_exe_interpreter_looks_for_an_exe_entrypoint(self) -> None:
        """`uv`'s Windows venvs put `murmly.exe` beside `python.exe`
        (task 8.1's install path has to find it, not `murmly` with no
        suffix) -- a pure function of the interpreter path's own shape, so
        it is testable from Linux with `executable=` alone."""
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python.exe"
            interpreter.write_text("", encoding="utf-8")
            entrypoint = Path(temp_dir) / "murmly.exe"
            entrypoint.write_text("", encoding="utf-8")
            entrypoint.chmod(0o755)

            resolved = resolve_entrypoint(str(interpreter))

            self.assertEqual(Path(temp_dir).resolve() / "murmly.exe", resolved)

    def test_an_exe_interpreter_does_not_find_a_suffixless_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python.exe"
            interpreter.write_text("", encoding="utf-8")
            entrypoint = Path(temp_dir) / "murmly"
            entrypoint.write_text("", encoding="utf-8")
            entrypoint.chmod(0o755)

            with self.assertRaises(InstallError) as raised:
                resolve_entrypoint(str(interpreter))

            self.assertIn("murmly.exe", str(raised.exception))

    def test_directory_entrypoint_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python3"
            interpreter.write_text("", encoding="utf-8")
            (Path(temp_dir) / "murmly").mkdir()

            with self.assertRaises(InstallError) as raised:
                resolve_entrypoint(str(interpreter))

            self.assertIn("not a file", str(raised.exception))


class UnitTextTests(unittest.TestCase):
    def test_anchors_on_the_graphical_session(self) -> None:
        text = service_unit_text(Path("/opt/murmly/.venv/bin/murmly"))

        self.assertIn("PartOf=graphical-session.target", text)
        self.assertIn("After=graphical-session.target", text)
        self.assertIn("WantedBy=graphical-session.target", text)

    def test_is_ordered_after_the_audio_server(self) -> None:
        """systemd stops units in reverse start order, so this stops Murmly first.

        PortAudio's JACK backend aborts the process when it is torn down after
        the audio server has gone, and a logout stops both in no fixed order.
        """
        text = service_unit_text(Path("/opt/murmly/.venv/bin/murmly"))

        self.assertIn("After=pipewire.service wireplumber.service", text)

    def test_the_audio_server_ordering_is_never_a_dependency(self) -> None:
        """Task 3.7: an optimisation on Linux, not something Murmly depends on.

        Murmly already has to survive the audio server disappearing first --
        that is what `disable_portaudio_exit_teardown()` is for -- so the unit
        must never escalate `After=` into `Requires=`, `BindsTo=` or `Wants=`
        naming the audio server: any of those would refuse to start, or would
        stop, Murmly alongside a unit it is required to outlive.
        """
        text = service_unit_text(Path("/opt/murmly/.venv/bin/murmly"))

        for directive in ("Requires=", "BindsTo=", "Wants="):
            for line in text.splitlines():
                if line.startswith(directive):
                    self.assertNotIn(
                        "pipewire",
                        line,
                        f"{line!r} makes the audio server a dependency, not an ordering",
                    )
                    self.assertNotIn(
                        "wireplumber",
                        line,
                        f"{line!r} makes the audio server a dependency, not an ordering",
                    )

    def test_does_not_activate_on_default_target(self) -> None:
        # The superseded template used WantedBy=default.target, which activates
        # before the session environment exists.
        self.assertNotIn("default.target", service_unit_text(Path("/opt/murmly/.venv/bin/murmly")))

    def test_exec_start_is_absolute_and_runs_the_daemon(self) -> None:
        text = service_unit_text(Path("/opt/murmly/.venv/bin/murmly"))

        self.assertIn("ExecStart=/opt/murmly/.venv/bin/murmly daemon", text)


class AtomicWriteTests(unittest.TestCase):
    def test_creates_parents_and_leaves_no_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "nested" / "unit.service"

            write_atomically(target, "body\n")

            self.assertEqual("body\n", target.read_text(encoding="utf-8"))
            self.assertEqual([target.name], [entry.name for entry in target.parent.iterdir()])

    def test_overwrites_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "unit.service"
            target.write_text("old\n", encoding="utf-8")

            write_atomically(target, "new\n")

            self.assertEqual("new\n", target.read_text(encoding="utf-8"))

    def test_applies_requested_mode(self) -> None:
        if not hasattr(os, "getuid"):
            self.skipTest("needs POSIX mode bits, which Windows does not enforce this way")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "unit.service"

            write_atomically(target, "body\n", mode=0o600)

            self.assertEqual(0o600, os.stat(target).st_mode & 0o777)


class ServiceInstallTests(unittest.TestCase):
    def test_writes_unit_then_reloads_enables_and_starts_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl()
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))

            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            self.assertTrue(service.unit_path.is_file())
            self.assertEqual(["daemon-reload", "enable", "start"], systemctl.verbs)

    def test_starts_the_service_not_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl()
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))

            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            started = [call for call in systemctl.calls if call[2] == "start"]
            self.assertEqual([["systemctl", "--user", "start", SERVICE_NAME]], started)

    def test_reinstall_repairs_a_stale_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=FakeSystemctl(), unit_dir=Path(temp_dir))
            service.install(Path("/old/location/.venv/bin/murmly"))

            service.install(Path("/new/location/.venv/bin/murmly"))

            self.assertEqual("/new/location/.venv/bin/murmly", service.recorded_entrypoint())

    def test_failing_systemctl_raises_with_its_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl(failures={"enable": 1})
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))

            with self.assertRaises(InstallError) as raised:
                service.install(Path("/opt/murmly/.venv/bin/murmly"))

            self.assertIn("enable", str(raised.exception))

    def test_unavailable_systemctl_raises(self) -> None:
        def explode(*_args, **_kwargs):
            raise OSError("systemctl missing")

        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=explode, unit_dir=Path(temp_dir))

            with self.assertRaises(InstallError) as raised:
                service.install(Path("/opt/murmly/.venv/bin/murmly"))

            self.assertIn("systemctl missing", str(raised.exception))


class ServiceRemovalTests(unittest.TestCase):
    def test_removes_unit_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl()
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))
            service.install(Path("/opt/murmly/.venv/bin/murmly"))
            systemctl.calls.clear()

            existed = service.remove()

            self.assertTrue(existed)
            self.assertFalse(service.unit_path.exists())
            self.assertEqual(["stop", "disable", "daemon-reload"], systemctl.verbs)

    def test_removal_succeeds_when_nothing_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=FakeSystemctl(), unit_dir=Path(temp_dir))

            self.assertFalse(service.remove())

    def test_removal_tolerates_systemctl_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl(failures={"stop": 5, "disable": 1})
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))
            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            self.assertTrue(service.remove())
            self.assertFalse(service.unit_path.exists())

    def test_removal_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=FakeSystemctl(), unit_dir=Path(temp_dir))
            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            service.remove()

            self.assertFalse(service.remove())


class ServiceStatusTests(unittest.TestCase):
    def test_reports_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=FakeSystemctl(), unit_dir=Path(temp_dir))

            status = service.status()

            self.assertFalse(status.installed)
            self.assertFalse(status.active)
            self.assertIn("murmly install", status.detail)

    def test_reports_installed_and_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl(stdout={"is-active": "active\n"})
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))
            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            status = service.status()

            self.assertTrue(status.installed)
            self.assertTrue(status.active)
            self.assertEqual("/opt/murmly/.venv/bin/murmly", status.entrypoint)

    def test_reports_installed_but_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            systemctl = FakeSystemctl(failures={"is-active": 3}, stdout={"is-active": "inactive\n"})
            service = UserService(run_command=systemctl, unit_dir=Path(temp_dir))
            service.install(Path("/opt/murmly/.venv/bin/murmly"))

            status = service.status()

            self.assertTrue(status.installed)
            self.assertFalse(status.active)
            self.assertIn("not running", status.detail)

    def test_recorded_entrypoint_is_none_without_a_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UserService(run_command=FakeSystemctl(), unit_dir=Path(temp_dir))

            self.assertIsNone(service.recorded_entrypoint())


class FakeSchtasks:
    """A minimal, stateful stand-in for `schtasks.exe`: enough of `/create`,
    `/query`, `/run`, `/end`, `/delete` and `/change` to answer the way a real
    Task Scheduler task would after each. Verb is `command[1]`, one past the
    `schtasks` binary name itself -- `WindowsUserService` never varies the
    binary name mid-argument-list the way `systemctl --user` does."""

    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._failures = failures or {}
        self._created = False
        self._running = False
        self._run_line: str | None = None

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        verb = command[1] if len(command) > 1 else ""
        if verb in self._failures:
            return subprocess.CompletedProcess(command, self._failures[verb], "", "boom")
        if verb == "/create":
            self._run_line = command[command.index("/tr") + 1]
            self._created = True
            self._running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "/query":
            if not self._created:
                return subprocess.CompletedProcess(command, 1, "", "ERROR: task not found")
            if "/v" in command:
                return subprocess.CompletedProcess(
                    command, 0, f"Task To Run:                          {self._run_line}\n", ""
                )
            status = "Running" if self._running else "Ready"
            return subprocess.CompletedProcess(
                command, 0, f"Status:                               {status}\n", ""
            )
        if verb == "/run":
            self._running = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "/end":
            self._running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "/delete":
            self._created = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "/change":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    @property
    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if len(call) > 1]


class MacosLaunchdAgentPlistTests(unittest.TestCase):
    """Task 12.3: `macos_launchd_agent_plist` shapes the plist content --
    `launchctl` is never invoked here, and neither is a file written; that is
    section 13's `LaunchdUserService`. What this pins is the dictionary
    `plistlib.dump` would serialize, including `AssociatedBundleIdentifiers`,
    which is the documented first remedy for design.md's largest risk: TCC
    attributing a launchd-started process's microphone request to a bundle
    that already holds the grant.
    """

    def test_the_baseline_keys_are_always_present(self) -> None:
        plist = macos_launchd_agent_plist("com.murmly.daemon", ["/usr/bin/python3", "-m", "murmly", "daemon"])

        self.assertEqual("com.murmly.daemon", plist["Label"])
        self.assertEqual(["/usr/bin/python3", "-m", "murmly", "daemon"], plist["ProgramArguments"])
        self.assertIs(True, plist["RunAtLoad"])
        self.assertIs(True, plist["KeepAlive"])

    def test_no_bundle_identifier_omits_the_key_entirely(self) -> None:
        """Omitted, not an empty list: `AssociatedBundleIdentifiers: []` is
        not documented as "no association" the way leaving the key out is,
        and Murmly ships no bundle to name until task 12.4 or a confirmed
        12.3 spike produces one."""
        plist = macos_launchd_agent_plist("com.murmly.daemon", ["/usr/bin/python3"])

        self.assertNotIn("AssociatedBundleIdentifiers", plist)

    def test_a_bundle_identifier_is_carried_as_a_list(self) -> None:
        plist = macos_launchd_agent_plist(
            "com.murmly.daemon",
            ["/usr/bin/python3"],
            associated_bundle_identifiers=["com.example.murmly-carrier"],
        )

        self.assertEqual(["com.example.murmly-carrier"], plist["AssociatedBundleIdentifiers"])

    def test_the_plist_round_trips_through_plistlib(self) -> None:
        """The real consumer -- `plistlib.dump` -- has to accept this
        dictionary's shape without complaint; a dict `plistlib` cannot
        serialize would only be discovered on a real Mac otherwise."""
        plist = macos_launchd_agent_plist(
            "com.murmly.daemon",
            ["/usr/bin/python3", "daemon"],
            associated_bundle_identifiers=["com.example.murmly-carrier"],
        )

        serialized = plistlib.dumps(plist)
        round_tripped = plistlib.loads(serialized)

        self.assertEqual(plist, round_tripped)


class FakeLaunchctl:
    """A minimal, stateful stand-in for `launchctl`: enough of `bootstrap`,
    `bootout`, `kickstart -k` and `print` to answer the way a real launchd
    domain target would after each -- and nothing at all for `load`, so a
    test asserting task 13.4's "never `load`" rule has a fake that would
    itself fail loudly (`AttributeError`-shaped, via the `KeyError` a bad
    verb falls through to) rather than silently accepting one. Verb is
    `command[1]`, one past the `launchctl` binary name itself.
    """

    def __init__(self, failures: dict[str, int] | None = None, never_runs: bool = False) -> None:
        self.calls: list[list[str]] = []
        self._failures = failures or {}
        self._never_runs = never_runs
        self._bootstrapped = False
        self._running = False

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        verb = command[1] if len(command) > 1 else ""
        if verb in self._failures:
            return subprocess.CompletedProcess(command, self._failures[verb], "", "boom")
        if verb == "bootstrap":
            self._bootstrapped = True
            self._running = not self._never_runs
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "bootout":
            self._bootstrapped = False
            self._running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "kickstart":
            if not self._bootstrapped:
                return subprocess.CompletedProcess(command, 3, "", "Could not find service")
            self._running = not self._never_runs
            return subprocess.CompletedProcess(command, 0, "", "")
        if verb == "print":
            if not self._bootstrapped:
                return subprocess.CompletedProcess(command, 3, "", "Could not find service")
            state = "running" if self._running else "not running"
            return subprocess.CompletedProcess(command, 0, f"\tstate = {state}\n", "")
        raise AssertionError(f"FakeLaunchctl was asked for an unmodelled verb: {verb!r}")

    @property
    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if len(call) > 1]


class LaunchdServiceInstallTests(unittest.TestCase):
    """Task 13.3/13.4: `LaunchdUserService` writes the plist and drives
    `bootstrap`/`kickstart -k`/`print`, verifying the agent is running before
    reporting success -- never `load`, whose disqualifying failure mode
    (exits 0, does nothing, on a malformed plist) is exactly what a binding
    verified before success is reported (this task's own spec-level concern)
    exists to rule out."""

    def _service(self, launchctl: FakeLaunchctl, agents_dir: Path) -> LaunchdUserService:
        return LaunchdUserService(run_command=launchctl, agents_dir=agents_dir, uid=501)

    def test_install_writes_the_plist_bootstraps_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "LaunchAgents"
            launchctl = FakeLaunchctl()
            service = self._service(launchctl, agents_dir)

            service.install(Path("/opt/murmly/murmly"))

            self.assertTrue(service.is_installed)
            plist = plistlib.loads(service.plist_path.read_bytes())
            self.assertEqual(LAUNCHD_LABEL, plist["Label"])
            self.assertEqual(["/opt/murmly/murmly", "daemon"], plist["ProgramArguments"])
            self.assertIs(True, plist["RunAtLoad"])
            self.assertIs(True, plist["KeepAlive"])
            self.assertTrue(service.is_active())

    def test_never_calls_load(self) -> None:
        """Every code path in this class, including `start()`'s own
        re-bootstrap fallback after a `bootout` -- the one path most tempted
        to reach for `load` instead of a fresh `bootstrap`."""
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")

            service.install(Path("/opt/murmly/murmly"))
            service.status()
            service.stop()
            service.start()
            service.remove()

            self.assertNotIn("load", launchctl.verbs)

    def test_domain_and_service_targets_use_gui_and_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")

            service.install(Path("/opt/murmly/murmly"))

            bootstrap_call = next(call for call in launchctl.calls if call[1] == "bootstrap")
            self.assertEqual("gui/501", bootstrap_call[2])
            kickstart_call = next(call for call in launchctl.calls if call[1] == "kickstart")
            self.assertEqual(f"gui/501/{LAUNCHD_LABEL}", kickstart_call[3])

    def test_install_bootouts_first_to_clear_a_stale_registration(self) -> None:
        """`bootstrap` refuses to replace an already-bootstrapped domain
        target, so a reinstall has to clear the previous one first -- the
        same role `daemon-reload` plays for `UserService.install`."""
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")

            service.install(Path("/opt/murmly/murmly"))
            service.install(Path("/opt/murmly/murmly"))

            self.assertEqual(["bootout", "bootstrap", "kickstart", "print", "bootout", "bootstrap", "kickstart", "print"], launchctl.verbs)

    def test_reinstall_repairs_a_stale_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")
            service.install(Path("/opt/old/murmly"))

            service.install(Path("/opt/new/murmly"))

            self.assertEqual("/opt/new/murmly", service.recorded_entrypoint())

    def test_failing_bootstrap_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl(failures={"bootstrap": 1})
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")

            with self.assertRaises(InstallError) as raised:
                service.install(Path("/opt/murmly/murmly"))

            self.assertIn("boom", str(raised.exception))

    def test_unavailable_launchctl_raises(self) -> None:
        def explode(*_args, **_kwargs):
            raise OSError("launchctl missing")

        with tempfile.TemporaryDirectory() as tmp:
            service = LaunchdUserService(run_command=explode, agents_dir=Path(tmp) / "LaunchAgents", uid=501)

            with self.assertRaises(InstallError) as raised:
                service.install(Path("/opt/murmly/murmly"))

            self.assertIn("launchctl missing", str(raised.exception))

    def test_install_raises_when_the_agent_never_reports_running(self) -> None:
        """13.4's own verification rule, the case `load` would have hidden:
        `bootstrap` and `kickstart` both report success, but the agent never
        actually comes up (a malformed plist, in the case this rule exists
        for) -- `install()` must not report success anyway."""
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl(never_runs=True)
            service = self._service(launchctl, Path(tmp) / "LaunchAgents")

            with self.assertRaises(InstallError) as raised:
                service.install(Path("/opt/murmly/murmly"))

            self.assertIn("not running", str(raised.exception))


class LaunchdServiceRemovalTests(unittest.TestCase):
    def test_removal_boots_out_then_deletes_the_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))
            launchctl.calls.clear()

            existed = service.remove()

            self.assertTrue(existed)
            self.assertEqual(["bootout"], launchctl.verbs)
            self.assertFalse(service.is_installed)

    def test_removal_succeeds_when_nothing_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = LaunchdUserService(
                run_command=FakeLaunchctl(), agents_dir=Path(tmp) / "LaunchAgents", uid=501
            )

            self.assertFalse(service.remove())

    def test_removal_tolerates_launchctl_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl(failures={"bootout": 1})
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))

            self.assertTrue(service.remove())
            self.assertFalse(service.is_installed)

    def test_removal_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))

            service.remove()

            self.assertFalse(service.remove())


class LaunchdServiceLifecycleTests(unittest.TestCase):
    def test_stop_and_start_toggle_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))

            self.assertTrue(service.is_active())

            service.stop()

            self.assertFalse(service.is_active())

            service.start()

            self.assertTrue(service.is_active())

    def test_status_reports_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = LaunchdUserService(
                run_command=FakeLaunchctl(), agents_dir=Path(tmp) / "LaunchAgents", uid=501
            )

            status = service.status()

            self.assertFalse(status.installed)
            self.assertFalse(status.active)
            self.assertIn("murmly install", status.detail)

    def test_status_reports_installed_and_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))

            status = service.status()

            self.assertTrue(status.installed)
            self.assertTrue(status.active)
            self.assertEqual("/opt/murmly/murmly", status.entrypoint)

    def test_status_reports_installed_but_inactive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))
            service.stop()

            status = service.status()

            self.assertTrue(status.installed)
            self.assertFalse(status.active)
            self.assertIn("not running", status.detail)

    def test_recorded_entrypoint_is_read_from_the_plist_not_launchctl_print(self) -> None:
        """`launchctl print`'s output is undocumented and not a stable
        interface (this class's own docstring) -- `recorded_entrypoint`
        reads the plist file this class itself wrote instead, so it answers
        correctly even though this fake's own `print` output carries no
        `arguments = { ... }` block a real one would."""
        with tempfile.TemporaryDirectory() as tmp:
            launchctl = FakeLaunchctl()
            service = LaunchdUserService(run_command=launchctl, agents_dir=Path(tmp) / "LaunchAgents", uid=501)
            service.install(Path("/opt/murmly/murmly"))

            self.assertEqual("/opt/murmly/murmly", service.recorded_entrypoint())


class LaunchdServiceRuntimeIntegrationTests(unittest.TestCase):
    """Task 13.3/13.4 against real `launchctl`: everything above this class
    proves `LaunchdUserService`'s policy against `FakeLaunchctl`, which
    answers exactly the way this class's own docstring says a real domain
    target would -- an assumption nothing on this Linux development machine
    can confirm. This is what turns that assumption into a proof, the same
    role `GetpeereidRuntimeIntegrationTests` (`test_daemon.py`) and
    `MacosHotkeyRuntimeIntegrationTests` (`test_mac_hotkey.py`) play for their
    own mechanisms, applied to `bootstrap`/`print`/`kickstart -k`/`bootout`
    themselves.

    A scratch label (`net.local.murmly-test-<pid>`) and a scratch
    `agents_dir` keep this from ever touching a real Murmly install that
    happens to be registered on the same account -- `bootstrap` takes the
    plist path directly, so the plist never has to live under the real
    `~/Library/LaunchAgents` for `launchctl` to load it. The entrypoint is a
    tiny shell script that ignores the `daemon` argument `install()` always
    appends and execs `sleep 300` instead, so the agent has something to
    still be running when `is_active()`/`print` reads its state, rather than
    an instantly-exiting process racing the read.

    `addCleanup` boots the label out and deletes the plist unconditionally,
    so a failed assertion never leaves a scratch agent registered against a
    real account.

    `setUp` probes `gui/<uid>` itself (`launchctl print gui/<uid>`) before any
    test body runs, and skips only on that probe's own refusal -- naming the
    capability the host lacks, per the suite's own rule. `install()` itself is
    never wrapped in a `try`/`skipTest`: once the domain is confirmed present,
    any failure from `install()` is `is_active()`'s `state = running` parser
    disagreeing with what real `launchctl print` actually emits -- exactly the
    unconfirmed assumption this class exists to prove or disprove -- and must
    fail loudly rather than be swallowed as an environment gap.
    """

    def setUp(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("A macOS kernel is required to call launchctl")
        probe = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest(f"This runner has no gui/{os.getuid()} launchd domain to bootstrap into")

    def _make_service(self, agents_dir: Path) -> LaunchdUserService:
        label = f"net.local.murmly-test-{os.getpid()}"
        service = LaunchdUserService(agents_dir=agents_dir, label=label)
        self.addCleanup(service.remove)
        return service

    def _make_entrypoint(self, directory: Path) -> Path:
        script = directory / "murmly-test-agent.sh"
        script.write_text("#!/bin/sh\nexec sleep 300\n")
        os.chmod(script, 0o755)
        return script

    def test_install_status_stop_start_remove_against_real_launchctl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "LaunchAgents"
            service = self._make_service(agents_dir)
            entrypoint = self._make_entrypoint(Path(tmp))

            service.install(entrypoint)

            self.assertTrue(service.is_active())
            status = service.status()
            self.assertTrue(status.installed)
            self.assertTrue(status.active)
            self.assertEqual(entrypoint.as_posix(), status.entrypoint)

            self.assertTrue(service.stop())
            self.assertFalse(service.is_active())

            self.assertTrue(service.start())
            self.assertTrue(service.is_active())

            existed = service.remove()
            self.assertTrue(existed)
            self.assertFalse(service.is_installed)
            self.assertFalse(service.is_active())


class WindowsServiceInstallTests(unittest.TestCase):
    """Task 8.1: the Task Scheduler backend `SERVICE_MANAGEMENT` selects on
    Windows. Task 8.2 (registers and starts without administrative rights)
    can only be confirmed on Windows; what is proven here is the half that
    can be proven anywhere -- `install()` never asks for elevation."""

    def test_creates_a_logon_trigger_and_starts_it(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)

        service.install(Path("C:/murmly/murmly.exe"))

        self.assertEqual(["/create", "/run"], schtasks.verbs)
        create_call = schtasks.calls[0]
        self.assertIn("/sc", create_call)
        self.assertEqual("onlogon", create_call[create_call.index("/sc") + 1])
        self.assertTrue(service.is_installed)

    def test_never_requests_elevation(self) -> None:
        """Task 8.2's testable half: no `/ru` (run as a different account,
        e.g. SYSTEM) and no `/rl HIGHEST` anywhere in the invocation --
        omitting `/rl` entirely is what leaves the task at Task Scheduler's
        default `LIMITED` privilege level."""
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)

        service.install(Path("C:/murmly/murmly.exe"))

        for call in schtasks.calls:
            self.assertNotIn("/ru", call)
            self.assertNotIn("/rl", call)

    def test_the_run_line_ends_with_the_daemon_subcommand(self) -> None:
        # Asserted in backslash form -- the canonical Windows spelling
        # `_windows_path_text` renders on every host, not the forward-slash
        # form `Path("C:/...")` happens to keep unchanged when the suite runs
        # on Linux or macOS, where `Path` never recognises "C:" as a drive
        # letter to begin with.
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)

        service.install(Path("C:/murmly/murmly.exe"))

        create_call = schtasks.calls[0]
        run_line = create_call[create_call.index("/tr") + 1]
        self.assertEqual("C:\\murmly\\murmly.exe daemon", run_line)
        self.assertEqual("C:\\murmly\\murmly.exe", service.recorded_entrypoint())

    def test_an_entrypoint_containing_a_space_is_quoted(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)

        service.install(Path("C:/Program Files/murmly/murmly.exe"))

        create_call = schtasks.calls[0]
        run_line = create_call[create_call.index("/tr") + 1]
        self.assertEqual('"C:\\Program Files\\murmly\\murmly.exe" daemon', run_line)

    def test_reinstall_repairs_a_stale_path(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/old/murmly.exe"))

        service.install(Path("C:/new/murmly.exe"))

        self.assertEqual("C:\\new\\murmly.exe", service.recorded_entrypoint())

    def test_failing_schtasks_raises_with_its_error(self) -> None:
        schtasks = FakeSchtasks(failures={"/create": 1})
        service = WindowsUserService(run_command=schtasks)

        with self.assertRaises(InstallError) as raised:
            service.install(Path("C:/murmly/murmly.exe"))

        self.assertIn("boom", str(raised.exception))

    def test_unavailable_schtasks_raises(self) -> None:
        def explode(*_args, **_kwargs):
            raise OSError("schtasks missing")

        service = WindowsUserService(run_command=explode)

        with self.assertRaises(InstallError) as raised:
            service.install(Path("C:/murmly/murmly.exe"))

        self.assertIn("schtasks missing", str(raised.exception))


class WindowsServiceRemovalTests(unittest.TestCase):
    def test_removal_ends_then_deletes(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))
        schtasks.calls.clear()

        existed = service.remove()

        self.assertTrue(existed)
        # `remove()` checks `is_installed` first (its own `/query`) to know
        # whether to report `existed`, then ends and deletes the task.
        self.assertEqual(["/query", "/end", "/delete"], schtasks.verbs)
        self.assertFalse(service.is_installed)

    def test_removal_succeeds_when_nothing_is_installed(self) -> None:
        service = WindowsUserService(run_command=FakeSchtasks())

        self.assertFalse(service.remove())

    def test_removal_tolerates_schtasks_failures(self) -> None:
        schtasks = FakeSchtasks(failures={"/end": 1})
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))

        self.assertTrue(service.remove())

    def test_removal_is_idempotent(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))

        service.remove()

        self.assertFalse(service.remove())


class WindowsServiceLifecycleTests(unittest.TestCase):
    """`is_active()` and `status()` have to answer from a live query -- a
    task file existing says nothing about whether the process it names is
    currently running, unlike a systemd unit's own `is-active`."""

    def test_stop_and_start_toggle_is_active(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))

        self.assertTrue(service.is_active())

        service.stop()

        self.assertFalse(service.is_active())

    def test_enable_and_disable_do_not_raise(self) -> None:
        service = WindowsUserService(run_command=FakeSchtasks())

        self.assertTrue(service.enable())
        self.assertTrue(service.disable())

    def test_status_reports_not_installed(self) -> None:
        service = WindowsUserService(run_command=FakeSchtasks())

        status = service.status()

        self.assertFalse(status.installed)
        self.assertFalse(status.active)
        self.assertIn("murmly install", status.detail)

    def test_status_reports_installed_and_active(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))

        status = service.status()

        self.assertTrue(status.installed)
        self.assertTrue(status.active)
        # Backslash form -- see test_the_run_line_ends_with_the_daemon_subcommand.
        self.assertEqual("C:\\murmly\\murmly.exe", status.entrypoint)

    def test_status_reports_installed_but_inactive(self) -> None:
        schtasks = FakeSchtasks()
        service = WindowsUserService(run_command=schtasks)
        service.install(Path("C:/murmly/murmly.exe"))
        service.stop()

        status = service.status()

        self.assertTrue(status.installed)
        self.assertFalse(status.active)
        self.assertIn("not running", status.detail)

    def test_recorded_entrypoint_is_none_without_a_task(self) -> None:
        service = WindowsUserService(run_command=FakeSchtasks())

        self.assertIsNone(service.recorded_entrypoint())

    def test_task_name_is_distinct_from_the_systemd_unit_name(self) -> None:
        self.assertNotEqual(SERVICE_NAME, WINDOWS_TASK_NAME)


class FakeWindowsService:
    """`WindowsUserService`'s duck-typed surface, with `active` settable
    independently of `is_installed` -- unlike `FakeService` (systemd), a
    Task Scheduler task can be installed and not running, which is exactly
    the state task 8.7's scenarios turn on."""

    def __init__(self, installed: bool = False, active: bool = False, entrypoint: str | None = None) -> None:
        self.is_installed = installed
        self.active = active
        self.entrypoint = entrypoint
        self.installs: list[Path] = []
        self.removes = 0

    def install(self, entrypoint: Path) -> None:
        self.installs.append(entrypoint)
        self.is_installed = True
        self.active = True
        self.entrypoint = str(entrypoint)

    def remove(self) -> bool:
        self.removes += 1
        existed, self.is_installed = self.is_installed, False
        self.active = False
        return existed

    def status(self):
        from murmly.installer import ServiceStatus

        return ServiceStatus(
            installed=self.is_installed,
            active=self.active,
            entrypoint=self.entrypoint,
            detail="installed" if self.is_installed else "Run 'murmly install <hotkey>'",
        )

    def start_command_text(self) -> str:
        return "schtasks /run /tn MurmlyDaemon"


class FakeDaemonChannel:
    """A stand-in for `murmly.daemon.send_command`, scripted per command.

    Patched in at `murmly.daemon.send_command` -- `Installer`'s in-process
    methods import that name from inside their own function bodies, so
    patching the module attribute is what every one of those deferred
    imports actually sees.
    """

    def __init__(
        self,
        held: set[str] | None = None,
        rebind_detail: str = "",
        unreachable: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.held = set(held) if held is not None else set()
        self.rebind_detail = rebind_detail
        self.unreachable = unreachable

    def __call__(self, _socket_path, command, *_args, **_kwargs):
        from murmly.daemon import COMMAND_REBIND_HOTKEYS, COMMAND_STATUS, DaemonNotRespondingError

        self.calls.append(command)
        if self.unreachable:
            raise DaemonNotRespondingError("no daemon")
        if command == COMMAND_REBIND_HOTKEYS:
            return {"ok": True, "detail": self.rebind_detail}
        if command == COMMAND_STATUS:
            return {"ok": True, "hotkeys_held": sorted(self.held)}
        raise AssertionError(f"unexpected command {command!r}")


def _windows_installer(service=None, channel=None, temp_dir=None, **kwargs):
    """An `Installer` dispatched to the in-process (Windows) branch, backed
    by fakes for the service and the command channel -- never a real
    `schtasks.exe` or `ctypes.windll` call."""
    from murmly.config import MurmlyConfig, WINDOWS_PIPE_NAME
    from murmly.hotkey_record import HotkeyRecordStore
    from murmly.installer import Installer
    from murmly.platform import OperatingSystem, PlatformProfile

    profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
    config = MurmlyConfig(
        socket_path=Path(WINDOWS_PIPE_NAME),
        config_path=Path(temp_dir) / "config.toml",
        overlay_enabled=False,
    )
    installer = Installer(
        service=service if service is not None else FakeWindowsService(),
        profile=profile,
        config=config,
        record_store=HotkeyRecordStore(Path(temp_dir) / "hotkeys.json"),
        entrypoint_resolver=lambda: Path("C:/murmly/murmly.exe"),
        sleep=lambda _seconds: None,
        daemon_timeout=1.0,
        poll_interval=0.01,
        **kwargs,
    )
    return installer


class WindowsInProcessInstallTests(unittest.TestCase):
    """Task 8.6: installation starts the daemon before reporting a hotkey
    bound, and reports the binding as held by the running daemon."""

    def test_installs_and_starts_the_service_before_confirming(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService()
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertEqual([Path("C:/murmly/murmly.exe")], service.installs)
        self.assertTrue(outcome.hotkey_registered)
        self.assertTrue(any("held by the running Murmly daemon" in message for message in outcome.messages))

    def test_persists_the_record_before_reaching_the_daemon(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(temp_dir=temp_dir)
                installer.install(parse_hotkey("Meta+X"))

            record = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read()

        self.assertEqual("Meta+X", record["window"])

    def test_both_purposes_are_registered_and_reported_held(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(held={"window", "session"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(temp_dir=temp_dir)
                outcome = installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+Shift+X"))

        self.assertTrue(outcome.hotkey_registered)
        self.assertTrue(outcome.session_hotkey_registered)

    def test_already_bound_is_reported_not_as_a_conflict(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(
                    service=FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe"),
                    temp_dir=temp_dir,
                )
                outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.already_bound)


class WindowsInProcessConflictTests(unittest.TestCase):
    """Task 8.4: the platform's own refusal is the collision. No service,
    launcher, or hotkey registration is left behind."""

    def test_a_refused_registration_rolls_back_the_record_and_the_new_service(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore
        from murmly.installer import HotkeyConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService()
            # The daemon reports "window" as held (from the fake's own default
            # rebind) but never reports it in `hotkeys_held` -- the platform
            # refused it.
            channel = FakeDaemonChannel(held=set(), rebind_detail="Meta+X is already claimed.")
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)

                with self.assertRaises(HotkeyConflictError) as raised:
                    installer.install(parse_hotkey("Meta+X"))

            record = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read()

        self.assertIn("Meta+X", str(raised.exception))
        self.assertEqual({}, record)
        self.assertEqual(1, service.removes)

    def test_a_refused_second_purpose_does_not_leave_the_first_recorded(self) -> None:
        """Mirrors the desktop-launcher flow's own two-hotkey rule: a
        collision on the second purpose must not leave the first bound."""
        from murmly.hotkey import parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore
        from murmly.installer import HotkeyConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(held={"window"})  # "session" refused
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(temp_dir=temp_dir)

                with self.assertRaises(HotkeyConflictError):
                    installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+Shift+X"))

            record = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read()

        self.assertEqual({}, record)

    def test_an_existing_service_survives_a_rolled_back_install(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(held=set())
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)

                with self.assertRaises(HotkeyConflictError):
                    installer.install(parse_hotkey("Meta+X"))

        self.assertEqual(0, service.removes)

    def test_same_key_for_both_purposes_is_refused_before_the_service_is_touched(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyConflictError

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService()
            installer = _windows_installer(service=service, temp_dir=temp_dir)

            with self.assertRaises(HotkeyConflictError):
                installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+X"))

        self.assertEqual([], service.installs)

    def test_a_key_windows_cannot_encode_is_refused_before_anything_is_written(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import InstallError

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService()
            installer = _windows_installer(service=service, temp_dir=temp_dir)

            with self.assertRaises(InstallError):
                installer.install(parse_hotkey("Ctrl+Microphone Mute"))

        self.assertEqual([], service.installs)


class WindowsInProcessUnconfirmedTests(unittest.TestCase):
    """A daemon that never becomes reachable is "not confirmed", not
    "refused": the record stays, matching the desktop-launcher flow's own
    `HotkeyNotConfirmedError` -- the binding is saved and picked up the next
    time the service starts."""

    def test_an_unreachable_daemon_is_not_confirmed_and_the_record_stays(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore
        from murmly.installer import HotkeyNotConfirmedError

        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(temp_dir=temp_dir)

                with self.assertRaises(HotkeyNotConfirmedError) as raised:
                    installer.install(parse_hotkey("Meta+X"))

            record = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read()

        self.assertEqual("Meta+X", record["window"])
        self.assertIn("Meta+X", raised.exception.hotkeys[0].portable)


class WindowsInProcessUninstallTests(unittest.TestCase):
    def test_uninstall_clears_the_record_and_removes_the_service(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                outcome = installer.uninstall()

            record = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read()

        self.assertEqual({}, record)
        self.assertEqual(1, service.removes)
        self.assertTrue(any("Released" in message for message in outcome.messages))

    def test_uninstall_with_nothing_installed_reports_nothing_to_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(temp_dir=temp_dir)
                outcome = installer.uninstall()

        self.assertTrue(any("nothing to remove" in message for message in outcome.messages))


class WindowsInProcessStatusTests(unittest.TestCase):
    """Task 8.6/8.7 and 18.10: held by the running daemon while it runs and
    holds the key; not held, naming the daemon and the start command, once
    it is not."""

    def test_held_while_the_daemon_reports_it(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertTrue(status["hotkey_held"])
        self.assertEqual("Meta+X", status["hotkey"])
        window_entry = status["hotkeys"][0]
        self.assertTrue(window_entry["held"])
        self.assertIn("running Murmly daemon", window_entry["detail"])

    def test_not_held_when_the_daemon_is_not_running(self) -> None:
        """8.7 and 18.10: not held, naming the daemon as why, when the
        daemon is not running -- and never even asked, since a stopped
        service cannot hold anything."""
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeWindowsService(installed=True, active=False, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertFalse(status["hotkey_held"])
        window_entry = status["hotkeys"][0]
        self.assertFalse(window_entry["held"])
        self.assertIn("daemon is not running", window_entry["detail"])
        self.assertIn("schtasks /run", window_entry["detail"])
        self.assertEqual([], channel.calls)

    def test_not_held_when_the_daemon_is_running_but_unreachable(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertFalse(status["hotkey_held"])
        self.assertIn("Unable to confirm", status["hotkeys"][0]["detail"])

    def test_no_hotkey_bound_reports_no_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeWindowsService(installed=True, active=True, entrypoint="C:/murmly/murmly.exe")
            channel = FakeDaemonChannel(held=set())
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _windows_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertIsNone(status["hotkey"])
        self.assertFalse(status["hotkey_held"])

    def test_not_installed_reports_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            installer = _windows_installer(temp_dir=temp_dir)
            status = installer.status()

        self.assertFalse(status["installed"])
        self.assertIn("murmly install", status["detail"])


class FakeMacosService:
    """`LaunchdUserService`'s duck-typed surface -- the same shape
    `FakeWindowsService` gives `WindowsUserService`, with `start_command_text`
    naming a `launchctl kickstart` line instead of a `schtasks /run` one."""

    def __init__(self, installed: bool = False, active: bool = False, entrypoint: str | None = None) -> None:
        self.is_installed = installed
        self.active = active
        self.entrypoint = entrypoint
        self.installs: list[Path] = []
        self.removes = 0

    def install(self, entrypoint: Path) -> None:
        self.installs.append(entrypoint)
        self.is_installed = True
        self.active = True
        self.entrypoint = str(entrypoint)

    def remove(self) -> bool:
        self.removes += 1
        existed, self.is_installed = self.is_installed, False
        self.active = False
        return existed

    def status(self):
        from murmly.installer import ServiceStatus

        return ServiceStatus(
            installed=self.is_installed,
            active=self.active,
            entrypoint=self.entrypoint,
            detail="installed" if self.is_installed else "Run 'murmly install <hotkey>'",
        )

    def start_command_text(self) -> str:
        return "launchctl kickstart -k gui/501/net.local.murmly"


def _macos_installer(service=None, temp_dir=None, **kwargs):
    """An `Installer` dispatched to the in-process (macOS) branch, backed by
    fakes for the service and the command channel -- never a real
    `launchctl` or framework call. Mirrors `_windows_installer` exactly,
    substituting a macOS profile and `FakeMacosService`."""
    from murmly.config import MurmlyConfig
    from murmly.hotkey_record import HotkeyRecordStore
    from murmly.installer import Installer
    from murmly.platform import OperatingSystem, PlatformProfile

    profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")
    config = MurmlyConfig(
        socket_path=Path(temp_dir) / "murmly.sock",
        config_path=Path(temp_dir) / "config.toml",
        overlay_enabled=False,
    )
    installer = Installer(
        service=service if service is not None else FakeMacosService(),
        profile=profile,
        config=config,
        record_store=HotkeyRecordStore(Path(temp_dir) / "hotkeys.json"),
        entrypoint_resolver=lambda: Path("/opt/murmly/murmly"),
        sleep=lambda _seconds: None,
        daemon_timeout=1.0,
        poll_interval=0.01,
        **kwargs,
    )
    return installer


class MacosInProcessInstallTests(unittest.TestCase):
    """Task 13.5's own `_install_in_process` dispatch, mirroring
    `WindowsInProcessInstallTests`: the pre-flight refusal must validate
    against macOS's own encoder (F1-F20, not Windows' F1-F24), and requesting
    the Accessibility permission (task 14.5) must happen exactly once, before
    the record is written or the service touched."""

    def test_a_key_macos_cannot_encode_is_refused_before_anything_is_written(self) -> None:
        """macOS's function-key ceiling is F20 -- Windows' own analogous test
        (`WindowsInProcessConflictTests`) uses a key past *Windows'* ceiling;
        this uses one between the two platforms' ceilings (F21: valid on
        Windows and on KDE/GNOME, refused only here) to prove the dispatch
        genuinely reaches macOS's own encoder rather than Windows' by
        accident."""
        from murmly.hotkey import HotkeyError, parse_hotkey
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeMacosService()
            channel = FakeDaemonChannel()
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(service=service, temp_dir=temp_dir)

                with self.assertRaises(InstallError) as raised:
                    installer.install(parse_hotkey("Ctrl+F21"))

            self.assertIn("F21", str(raised.exception))
            self.assertIn("macOS", str(raised.exception))
            self.assertEqual(0, len(service.installs))
            self.assertEqual({}, HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read())

    def test_install_requests_the_accessibility_permission_exactly_once(self) -> None:
        """Task 14.5: `murmly install` is the one call site allowed to
        prompt, and it must do so regardless of which install path this
        platform takes -- macOS reaches `_install_in_process`, not the
        desktop-launcher body every other platform's `install()` runs."""
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(temp_dir=temp_dir)
                requests = []
                installer._request_macos_accessibility_permission = lambda: requests.append(True)

                installer.install(parse_hotkey("Meta+X"))

        self.assertEqual(1, len(requests))

    def test_a_successful_install_registers_and_reports_held(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            service = FakeMacosService()
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(service=service, temp_dir=temp_dir)
                outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.hotkey_registered)
        self.assertEqual(1, len(service.installs))


class MacosInProcessStatusTests(unittest.TestCase):
    """Task 13.6/13.7 and 18.10: held by the running daemon while it runs and
    holds the key; not held, naming the daemon and the start command, once it
    is not; and the Carbon mechanism's own limitation named unconditionally
    (task 13.7), mirroring `WindowsInProcessStatusTests` test-for-test plus
    the one macOS-only assertion neither Windows class needs."""

    def test_held_while_the_daemon_reports_it(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore
        from murmly.installer import MACOS_HOTKEY_MECHANISM_LIMITATION

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeMacosService(installed=True, active=True, entrypoint="/opt/murmly/murmly")
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertTrue(status["hotkey_held"])
        self.assertEqual("Meta+X", status["hotkey"])
        window_entry = status["hotkeys"][0]
        self.assertTrue(window_entry["held"])
        self.assertIn("running Murmly daemon", window_entry["detail"])
        # Task 13.7: named unconditionally, not only when a binding is
        # currently unheld -- a combination can be held by Murmly and still
        # never fire, because the frontmost application consumed it first.
        self.assertEqual(MACOS_HOTKEY_MECHANISM_LIMITATION, status["hotkey_mechanism_limitation"])

    def test_not_held_when_the_daemon_is_not_running(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore
        from murmly.installer import MACOS_HOTKEY_MECHANISM_LIMITATION

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeMacosService(installed=True, active=False, entrypoint="/opt/murmly/murmly")
            channel = FakeDaemonChannel(held={"window"})
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(service=service, temp_dir=temp_dir)
                status = installer.status()

        self.assertFalse(status["hotkey_held"])
        window_entry = status["hotkeys"][0]
        self.assertFalse(window_entry["held"])
        self.assertIn("daemon is not running", window_entry["detail"])
        self.assertIn("launchctl kickstart", window_entry["detail"])
        self.assertEqual([], channel.calls)
        # Still named even when nothing is held: it is a standing property of
        # the mechanism, not a symptom of the current unheld state.
        self.assertEqual(MACOS_HOTKEY_MECHANISM_LIMITATION, status["hotkey_mechanism_limitation"])

    def test_not_installed_still_names_the_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            installer = _macos_installer(temp_dir=temp_dir)
            status = installer.status()

        self.assertFalse(status["installed"])
        self.assertIn("hotkey_mechanism_limitation", status)

    def test_windows_status_carries_no_macos_limitation_key(self) -> None:
        """The field is macOS-only -- a Windows report must not gain it."""
        with tempfile.TemporaryDirectory() as temp_dir:
            installer = _windows_installer(temp_dir=temp_dir)
            status = installer.status()

        self.assertNotIn("hotkey_mechanism_limitation", status)


class MacosInProcessUninstallTests(unittest.TestCase):
    def test_uninstall_clears_the_record_and_removes_the_service(self) -> None:
        from murmly.hotkey_record import HotkeyRecordStore

        with tempfile.TemporaryDirectory() as temp_dir:
            HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").write({"window": "Meta+X"})
            service = FakeMacosService(installed=True, active=True, entrypoint="/opt/murmly/murmly")
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(service=service, temp_dir=temp_dir)
                outcome = installer.uninstall()

        self.assertEqual({}, HotkeyRecordStore(Path(temp_dir) / "hotkeys.json").read())
        self.assertEqual(1, service.removes)
        self.assertFalse(outcome.hotkey_registered)

    def test_uninstall_never_requests_the_accessibility_permission(self) -> None:
        """Task 14.5: only `install()` may prompt -- `uninstall()` must never
        reach `_request_macos_accessibility_permission` at all."""
        with tempfile.TemporaryDirectory() as temp_dir:
            channel = FakeDaemonChannel(unreachable=True)
            with unittest.mock.patch("murmly.daemon.send_command", channel):
                installer = _macos_installer(temp_dir=temp_dir)
                requests = []
                installer._request_macos_accessibility_permission = lambda: requests.append(True)

                installer.uninstall()

        self.assertEqual([], requests)


if __name__ == "__main__":
    unittest.main()


class FakeShortcuts:
    """A stand-in Plasma registry whose component appears once registered."""

    def __init__(self, present: bool = False, owners=None, keys=None) -> None:
        self.present = present
        self._owners = owners if owners is not None else {}
        self._keys = keys if keys is not None else {}
        self.queries: list[str] = []

    def component_exists(self, component: str) -> bool:
        self.queries.append(component)
        return self.present

    def owners_of(self, keycode: int):
        return list(self._owners.get(keycode, []))

    def is_available(self, keycode: int) -> bool:
        return not self._owners.get(keycode)

    def registered_keys(self, component: str):
        return list(self._keys.get(component, []))


class RecordingRun:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(args=command, returncode=self._returncode, stdout="", stderr="")


class FakePlasmaBus:
    """A stand-in `busctl --user --json=short` for task 4's other gap: an
    *unpinned* `Installer` driving the real `PlasmaShortcuts` and
    `ShortcutLauncher` end to end, rather than only the two backends in
    isolation the way `FakeShortcuts`/`RecordingRun` above do.

    Answers every query by reading whatever `.desktop` launcher files
    actually exist under `applications_dir` right now -- the same
    relationship the real `kglobalacceld` has to the files `ShortcutLauncher`
    writes -- so `register()` writing a file and `unregister()` removing one
    is what changes this fake's answers, with nothing to keep in sync by
    hand. Component name is the file name, exactly as `ShortcutLauncher`
    already keys `launcher_path` on `purpose.desktop_id`.
    """

    def __init__(self, applications_dir: Path) -> None:
        self._applications_dir = applications_dir
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if len(command) == 1:
            # `kbuildsycoca6`, the cache-rebuild nudge -- nothing to model.
            return self._ok()
        if len(command) < 8 or command[3] != "call":
            return self._fail(command, f"unrecognized command shape {command!r}")
        method = command[7]
        if method == "getComponent":
            component = command[9]
            if self._path(component).is_file():
                return self._ok()
            return self._fail(command, f"Component {component!r} doesn't exist")
        if method == "getGlobalShortcutsByKey":
            keycode = int(command[9])
            rows = [
                self._row(component)
                for component in self._components()
                if self._keycode_of(component) == keycode
            ]
            return self._json({"type": "a(ssssssaiai)", "data": [rows]})
        if method == "shortcutKeys":
            component = command[10]
            keycode = self._keycode_of(component)
            sequences = [] if keycode is None else [[[keycode, 0, 0, 0]]]
            return self._json({"type": "a(ai)", "data": [sequences]})
        return self._fail(command, f"unhandled method {method!r}")

    def _path(self, component: str) -> Path:
        return self._applications_dir / component

    def _components(self) -> list[str]:
        if not self._applications_dir.is_dir():
            return []
        return [entry.name for entry in self._applications_dir.iterdir() if entry.is_file()]

    def _keycode_of(self, component: str) -> int | None:
        from murmly.hotkey import parse_hotkey

        path = self._path(component)
        if not path.is_file():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("X-KDE-Shortcuts="):
                value = line.removeprefix("X-KDE-Shortcuts=").strip()
                return parse_hotkey(value).keycode if value else None
        return None

    def _row(self, component: str) -> list:
        return ["_launch", component, component, component, "default", "Default Context", [], []]

    def _ok(self) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _fail(self, command, stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr=stderr)

    def _json(self, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")


def make_launcher(temp_dir: str, shortcuts, run=None, **kwargs):
    from murmly.installer import ShortcutLauncher

    return ShortcutLauncher(
        shortcuts=shortcuts,
        run_command=run or RecordingRun(),
        applications_dir=Path(temp_dir) / "applications",
        shortcut_config_path=Path(temp_dir) / "kglobalshortcutsrc",
        sleep=lambda _seconds: None,
        clock=_StepClock(),
        **kwargs,
    )


class _StepClock:
    """Monotonic clock that advances a tenth of a second per read."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        self._now += 0.1
        return self._now


class LauncherTextTests(unittest.TestCase):
    def test_contains_the_mandatory_shortcut_line(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import launcher_text

        text = launcher_text(Path("/opt/murmly/.venv/bin/murmly"), parse_hotkey("Meta+X"))

        self.assertIn("X-KDE-Shortcuts=Meta+X", text)
        self.assertIn("Exec=/opt/murmly/.venv/bin/murmly toggle", text)
        self.assertIn("NoDisplay=true", text)
        self.assertIn("Type=Application", text)
        self.assertIn("X-KDE-GlobalAccel-CommandShortcut=true", text)

    def test_doubles_literal_percent_in_exec(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import launcher_text

        text = launcher_text(Path("/opt/100%murmly/bin/murmly"), parse_hotkey("Meta+X"))

        self.assertIn("Exec=/opt/100%%murmly/bin/murmly toggle", text)

    def test_uses_the_canonical_portable_hotkey_form(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import launcher_text

        text = launcher_text(Path("/bin/murmly"), parse_hotkey("shift+alt+super+x"))

        self.assertIn("X-KDE-Shortcuts=Meta+Alt+Shift+X", text)


class LauncherRegistrationTests(unittest.TestCase):
    def test_writes_launcher_rebuilds_cache_and_confirms(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            shortcuts = FakeShortcuts(present=True)
            run = RecordingRun()
            launcher = make_launcher(temp_dir, shortcuts, run=run)

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            self.assertTrue(launcher.is_present)
            self.assertEqual("Meta+X", launcher.declared_hotkey())
            self.assertEqual([["kbuildsycoca6"]], run.calls)

    def test_unconfirmed_registration_raises_the_distinct_error(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyNotConfirmedError

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = make_launcher(temp_dir, FakeShortcuts(present=False), timeout=0.5)

            with self.assertRaises(HotkeyNotConfirmedError):
                launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            # The file stays: the binding is persisted for the next login.
            self.assertTrue(launcher.is_present)

    def test_cache_rebuild_failure_is_not_fatal(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = make_launcher(temp_dir, FakeShortcuts(present=True), run=RecordingRun(returncode=1))

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            self.assertTrue(launcher.is_present)

    def test_missing_cache_builder_is_not_fatal(self) -> None:
        from murmly.hotkey import parse_hotkey

        def explode(*_args, **_kwargs):
            raise OSError("kbuildsycoca6 missing")

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = make_launcher(temp_dir, FakeShortcuts(present=True), run=explode)

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            self.assertTrue(launcher.is_present)


class FileBackedShortcuts:
    """A registry whose component tracks the launcher file, as Plasma's does.

    Records what it observed each time it was polled, so a test can assert that
    the component genuinely went away before the new one appeared.
    """

    def __init__(self, launcher_path: Path) -> None:
        self._launcher_path = launcher_path
        self.observations: list[bool] = []

    def component_exists(self, component: str) -> bool:
        present = self._launcher_path.is_file()
        self.observations.append(present)
        return present


class LauncherRebindTests(unittest.TestCase):
    """An in-place rewrite is ignored by the running session, so a rebind must
    take the component away before adding the new one."""

    def _launcher(self, temp_dir: str):
        applications = Path(temp_dir) / "applications"
        shortcuts = FileBackedShortcuts(applications / "net.local.murmly.desktop")
        return make_launcher(temp_dir, shortcuts), shortcuts

    def test_rebind_observes_the_component_gone_before_it_returns(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher, shortcuts = self._launcher(temp_dir)
            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))
            shortcuts.observations.clear()

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+Y"))

            self.assertEqual("Meta+Y", launcher.declared_hotkey())
            # First poll of the rebind sees the old component gone; a later one
            # sees the new component present.
            self.assertFalse(shortcuts.observations[0])
            self.assertTrue(shortcuts.observations[-1])

    def test_rebind_rebuilds_the_cache_for_both_halves(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            applications = Path(temp_dir) / "applications"
            shortcuts = FileBackedShortcuts(applications / "net.local.murmly.desktop")
            run = RecordingRun()
            launcher = make_launcher(temp_dir, shortcuts, run=run)
            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))
            run.calls.clear()

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+Y"))

            self.assertEqual([["kbuildsycoca6"], ["kbuildsycoca6"]], run.calls)

    def test_first_registration_rebuilds_once(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            applications = Path(temp_dir) / "applications"
            shortcuts = FileBackedShortcuts(applications / "net.local.murmly.desktop")
            run = RecordingRun()
            launcher = make_launcher(temp_dir, shortcuts, run=run)

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            self.assertEqual([["kbuildsycoca6"]], run.calls)

    def test_rebind_leaves_exactly_one_launcher_file(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            launcher, _shortcuts = self._launcher(temp_dir)
            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))

            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+Y"))

            entries = sorted(entry.name for entry in launcher.launcher_path.parent.iterdir())
            self.assertEqual(["net.local.murmly.desktop"], entries)


class LauncherRemovalTests(unittest.TestCase):
    def test_removes_and_reports_that_it_existed(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            shortcuts = FakeShortcuts(present=True)
            launcher = make_launcher(temp_dir, shortcuts)
            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))
            shortcuts.present = False

            self.assertTrue(launcher.unregister())
            self.assertFalse(launcher.is_present)

    def test_removal_when_absent_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = make_launcher(temp_dir, FakeShortcuts(present=False))

            self.assertFalse(launcher.unregister())

    def test_removal_does_not_touch_the_shortcut_configuration(self) -> None:
        from murmly.hotkey import parse_hotkey

        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "kglobalshortcutsrc"
            config.write_text("[kwin]\nExpose=Ctrl+F9\n", encoding="utf-8")
            before = config.read_bytes()
            shortcuts = FakeShortcuts(present=True)
            launcher = make_launcher(temp_dir, shortcuts)
            launcher.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))
            shortcuts.present = False
            launcher.unregister()

            self.assertEqual(before, config.read_bytes())


class UserOverrideTests(unittest.TestCase):
    def test_detects_an_override_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "kglobalshortcutsrc"
            config.write_text(
                "[kwin]\nExpose=Ctrl+F9\n\n"
                "[services][net.local.murmly.desktop]\n_launch=Meta+Z\n",
                encoding="utf-8",
            )
            launcher = make_launcher(temp_dir, FakeShortcuts())

            self.assertEqual("Meta+Z", launcher.user_override())

    def test_no_override_when_group_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "kglobalshortcutsrc"
            config.write_text("[services][other.desktop]\n_launch=Meta+Z\n", encoding="utf-8")
            launcher = make_launcher(temp_dir, FakeShortcuts())

            self.assertIsNone(launcher.user_override())

    def test_no_override_when_the_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            launcher = make_launcher(temp_dir, FakeShortcuts())

            self.assertIsNone(launcher.user_override())


class FakeService:
    def __init__(self, installed: bool = False, entrypoint: str | None = None) -> None:
        self.is_installed = installed
        self.entrypoint = entrypoint
        self.installs: list[Path] = []
        self.removes = 0

    def install(self, entrypoint: Path) -> None:
        self.installs.append(entrypoint)
        self.is_installed = True
        self.entrypoint = str(entrypoint)

    def remove(self) -> bool:
        self.removes += 1
        existed, self.is_installed = self.is_installed, False
        return existed

    def status(self):
        from murmly.installer import ServiceStatus

        return ServiceStatus(
            installed=self.is_installed,
            active=self.is_installed,
            entrypoint=self.entrypoint,
            detail="installed" if self.is_installed else "Run 'murmly install <hotkey>'",
        )


class FakeLauncher:
    def __init__(
        self,
        declared: str | None = None,
        override: str | None = None,
        fail=None,
        purpose=None,
        entrypoint: str | None = None,
        shortcuts=None,
    ) -> None:
        from murmly.installer import WINDOW_HOTKEY

        # Optional: a test that only reads declared_hotkey does not need it, but
        # anything exercising release-then-claim does.
        self._shortcuts = shortcuts
        self._declared = declared
        self._override = override
        self._fail = fail
        self.purpose = purpose or WINDOW_HOTKEY
        # The whole Exec line, as the real launcher reports it, so a test can
        # place a launcher whose key is current and whose command has moved.
        self._entrypoint = entrypoint
        self.registrations: list[tuple[Path, str]] = []
        self.unregistrations = 0

    def declared_hotkey(self):
        return self._declared

    def declared_entrypoint(self):
        return self._entrypoint

    def user_override(self):
        return self._override

    def register(self, entrypoint: Path, hotkey) -> None:
        if self._fail is not None:
            raise self._fail
        self.registrations.append((entrypoint, hotkey.portable))
        self._declared = hotkey.portable
        self._entrypoint = f"{entrypoint} {self.purpose.command}"
        if self._shortcuts is not None:
            self._shortcuts.claim(self.purpose.desktop_id, hotkey.keycode, self.purpose.name)

    def unregister(self) -> bool:
        self.unregistrations += 1
        existed, self._declared = self._declared is not None, None
        self._entrypoint = None
        if self._shortcuts is not None:
            self._shortcuts.release(self.purpose.desktop_id)
        return existed


class FakeSession:
    def __init__(self, supported: bool = True, verified: bool = True, detail: str = "KDE Plasma on x11.") -> None:
        self.supported = supported
        self.verified = verified
        self.detail = detail
        self.is_plasma = supported


class OwnerRegistry:
    """A shortcuts stand-in driven by explicit owner and key tables.

    `release` models what the desktop does when a launcher is removed: the
    component stops existing and its claims go with it. Without that a test
    cannot exercise anything that releases one key to claim another, because the
    stale claim answers every later question.
    """

    def __init__(self, owners=None, keys=None) -> None:
        self._owners = {code: list(entries) for code, entries in (owners or {}).items()}
        self._keys = {component: list(codes) for component, codes in (keys or {}).items()}

    def owners_of(self, keycode: int):
        return list(self._owners.get(keycode, []))

    def registered_keys(self, component: str):
        return list(self._keys.get(component, []))

    def is_available(self, keycode: int) -> bool:
        return not self._owners.get(keycode)

    def release(self, component: str) -> None:
        self._keys.pop(component, None)
        for code, entries in list(self._owners.items()):
            remaining = [e for e in entries if e.component_unique != component]
            if remaining:
                self._owners[code] = remaining
            else:
                self._owners.pop(code)

    def claim(self, component: str, keycode: int, friendly: str = "murmly") -> None:
        self._keys[component] = [keycode]
        self._owners.setdefault(keycode, []).append(owner(component, friendly))


def owner(component: str, friendly: str = "other"):
    from murmly.desktop import ShortcutOwner

    return ShortcutOwner("_launch", friendly, component, friendly)


class FakeRecordStore:
    """Task 5.4's record, faked: a real `HotkeyRecordStore` would write to the
    developer's own `~/.config/murmly/hotkeys.json` from every test in this
    file that calls `install()` or `uninstall()`, since none of them pins one."""

    def __init__(self) -> None:
        self.bindings: dict[str, str] | None = None

    def write(self, bindings: dict[str, str]) -> None:
        self.bindings = dict(bindings)

    def read(self) -> dict[str, str]:
        return dict(self.bindings or {})

    def remove(self) -> None:
        self.bindings = None


def make_installer(
    service=None,
    launcher=None,
    shortcuts=None,
    session=None,
    entrypoint="/bin/murmly",
    injection=None,
    session_launcher=None,
    record_store=None,
    profile=None,
):
    from murmly.installer import DESKTOP_ID, SESSION_HOTKEY, Installer
    from murmly.platform import OperatingSystem, PlatformProfile

    hotkey_code = 268435544
    # Pinned so the tests never depend on what this machine has installed.
    selected = injection or PasteInjection("xdotool", ("xdotool", "key", "--clearmodifiers", "ctrl+v"))
    return Installer(
        service=service or FakeService(),
        launcher=launcher or FakeLauncher(),
        # Always a fake: a real one would reach the developer's own launcher
        # directory and could release a binding they actually use.
        session_launcher=session_launcher or FakeLauncher(purpose=SESSION_HOTKEY),
        shortcuts=shortcuts or OwnerRegistry(keys={DESKTOP_ID: [hotkey_code]}, owners={hotkey_code: [owner(DESKTOP_ID, "murmly")]}),
        session=session or FakeSession(),
        # A Linux profile every one of this helper's callers gets by default,
        # regardless of which host runs the suite: every fake above (service,
        # launcher, shortcuts) stands in for the GNOME/Plasma desktop flow
        # `Installer.install` only reaches when `hotkey_mechanism_is_in_process
        # (self._profile)` is false. Left to the real host's own
        # `resolve_platform()`, a Windows runner would answer true instead and
        # send every one of these tests into the in-process branch the fakes
        # were never built for -- see `_windows_installer` above for the
        # deliberately-Windows counterpart of this same seam.
        profile=profile
        if profile is not None
        else PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
        entrypoint_resolver=lambda: Path(entrypoint),
        injection_selector=lambda: selected,
        record_store=record_store or FakeRecordStore(),
    )


class ConflictRefusalTests(unittest.TestCase):
    def test_refuses_a_hotkey_owned_by_another_application_and_names_it(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyConflictError

        service, launcher = FakeService(), FakeLauncher()
        shortcuts = OwnerRegistry(owners={268435544: [owner("plasmashell", "Klipper")]})
        installer = make_installer(service=service, launcher=launcher, shortcuts=shortcuts)

        with self.assertRaises(HotkeyConflictError) as raised:
            installer.install(parse_hotkey("Meta+X"))

        self.assertIn("Klipper", str(raised.exception))

    def test_refusal_leaves_nothing_installed(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyConflictError

        service, launcher = FakeService(), FakeLauncher()
        shortcuts = OwnerRegistry(owners={268435544: [owner("plasmashell", "Klipper")]})
        installer = make_installer(service=service, launcher=launcher, shortcuts=shortcuts)

        with self.assertRaises(HotkeyConflictError):
            installer.install(parse_hotkey("Meta+X"))

        self.assertEqual([], service.installs)
        self.assertEqual([], launcher.registrations)
        self.assertFalse(service.is_installed)


class IdempotenceTests(unittest.TestCase):
    def test_hotkey_already_owned_by_murmly_is_not_a_conflict(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID

        # Built through `Path`, not a bare literal, to match whatever
        # separator `make_installer`'s default entrypoint_resolver renders on
        # this host -- see `test_reinstall_repairs_a_stale_entrypoint` for
        # why. `install()` decides "already bound" partly by comparing this
        # string against `f"{entrypoint} {purpose.command}"` (installer.py's
        # own `declared_entrypoint() == ...` check), so a mismatched
        # separator here reads as a moved entrypoint and defeats the very
        # idempotence this test means to prove.
        launcher = FakeLauncher(declared="Meta+X", entrypoint=f"{Path('/bin/murmly')} toggle")
        shortcuts = OwnerRegistry(
            owners={268435544: [owner(DESKTOP_ID, "murmly")]},
            keys={DESKTOP_ID: [268435544]},
        )
        installer = make_installer(launcher=launcher, shortcuts=shortcuts)

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.already_bound)
        self.assertEqual([], launcher.registrations, "must not rebind an identical hotkey")

    def test_reinstall_repairs_a_stale_entrypoint(self) -> None:
        """The launcher is rewritten too, not only the service unit.

        Asserting the service alone passed while the launcher kept running a
        command that no longer exists, which is the whole failure a person
        reinstalls to repair.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID

        service = FakeService(installed=True, entrypoint="/old/murmly")
        launcher = FakeLauncher(declared="Meta+X", entrypoint="/old/murmly toggle")
        shortcuts = OwnerRegistry(
            owners={268435544: [owner(DESKTOP_ID, "murmly")]},
            keys={DESKTOP_ID: [268435544]},
        )
        installer = make_installer(
            service=service, launcher=launcher, shortcuts=shortcuts, entrypoint="/new/murmly"
        )

        installer.install(parse_hotkey("Meta+X"))

        self.assertEqual([Path("/new/murmly")], service.installs)
        self.assertEqual([(Path("/new/murmly"), "Meta+X")], launcher.registrations)
        # Built through `Path`, not a bare literal: `make_installer`'s
        # `entrypoint_resolver` renders "/new/murmly" through the real host's
        # own `pathlib` flavour (task 7's profile pin covers the OS enum, not
        # the separator `str(Path(...))` renders), and on Windows that is
        # backslashes even though the string handed in used forward slashes.
        self.assertEqual(f"{Path('/new/murmly')} toggle", launcher.declared_entrypoint())

    def test_reinstalling_one_hotkey_repairs_a_launcher_it_did_not_request(self) -> None:
        """The session hotkey goes stale whether or not the command names it.

        Reinstalling one hotkey is how a moved checkout is repaired. Repairing
        only the requested launcher leaves the other running a path that no
        longer exists, and nothing reports a launcher's command, so the person
        sees a healthy installation and a hotkey that does nothing.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/old/murmly toggle")
        session_launcher = FakeLauncher(
            declared="Meta+A", purpose=SESSION_HOTKEY, entrypoint="/old/murmly toggle-session"
        )
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly session")],
            },
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [268435521]},
        )
        installer = make_installer(
            launcher=launcher,
            session_launcher=session_launcher,
            shortcuts=shortcuts,
            entrypoint="/new/murmly",
        )

        installer.install(parse_hotkey("Meta+X"))

        # See `test_reinstall_repairs_a_stale_entrypoint` for why these are
        # built through `Path` rather than compared against a bare literal.
        self.assertEqual(f"{Path('/new/murmly')} toggle", launcher.declared_entrypoint())
        self.assertEqual(
            f"{Path('/new/murmly')} toggle-session",
            session_launcher.declared_entrypoint(),
            "a hotkey the command did not name was left running a path that is gone",
        )
        self.assertEqual("Meta+A", session_launcher.declared_hotkey(), "its key changed")

    def test_reinstalling_one_hotkey_repairs_the_other_launcher_too(self) -> None:
        """Both launchers run the entrypoint, so both go stale when it moves."""
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/old/murmly toggle")
        session_launcher = FakeLauncher(
            declared="Meta+A", purpose=SESSION_HOTKEY, entrypoint="/old/murmly toggle-session"
        )
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly session")],
            },
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [268435521]},
        )
        installer = make_installer(
            launcher=launcher,
            session_launcher=session_launcher,
            shortcuts=shortcuts,
            entrypoint="/new/murmly",
        )

        installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        # See `test_reinstall_repairs_a_stale_entrypoint` for why these are
        # built through `Path` rather than compared against a bare literal.
        self.assertEqual(f"{Path('/new/murmly')} toggle", launcher.declared_entrypoint())
        self.assertEqual(
            f"{Path('/new/murmly')} toggle-session", session_launcher.declared_entrypoint()
        )


class VerificationTests(unittest.TestCase):
    def test_key_mismatch_fails_and_cleans_up(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, InstallError

        service, launcher = FakeService(), FakeLauncher()
        # The desktop resolved the launcher to a different key than requested.
        shortcuts = OwnerRegistry(keys={DESKTOP_ID: [268435545]})
        installer = make_installer(service=service, launcher=launcher, shortcuts=shortcuts)

        with self.assertRaises(InstallError) as raised:
            installer.install(parse_hotkey("Meta+X"))

        self.assertIn("Meta+X", str(raised.exception))
        self.assertIn("268435545", str(raised.exception))
        self.assertEqual(1, launcher.unregistrations)
        self.assertEqual(1, service.removes)

    def test_a_first_hotkey_that_is_not_confirmed_still_writes_the_second(self) -> None:
        """A key the desktop was slow to confirm is persisted, not abandoned.

        Raising on the first left the second never written at all: not bound
        now, and not bound at the next login either, while the message named
        only the first. Both are attempted and both unconfirmed keys reported.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import (
            DESKTOP_ID,
            SESSION_DESKTOP_ID,
            SESSION_HOTKEY,
            HotkeyNotConfirmedError,
        )

        launcher = FakeLauncher(fail=HotkeyNotConfirmedError("slow", parse_hotkey("Meta+X")))
        session_launcher = FakeLauncher(purpose=SESSION_HOTKEY)
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly session")],
            },
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [268435521]},
        )
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        with self.assertRaises(HotkeyNotConfirmedError) as raised:
            installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        self.assertEqual(
            [(Path("/bin/murmly"), "Meta+A")],
            session_launcher.registrations,
            "the second hotkey was never written",
        )
        self.assertEqual(("Meta+X",), tuple(k.portable for k in raised.exception.hotkeys))

    def test_the_two_murmly_hotkeys_can_be_swapped(self) -> None:
        """Murmly's own launcher is not a foreign claimant on a key it is
        about to release. Treating it as one refused the install with a message
        naming Murmly as the other application, and the swap was impossible
        without uninstalling first.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/bin/murmly toggle")
        session_launcher = FakeLauncher(
            declared="Meta+A", purpose=SESSION_HOTKEY, entrypoint="/bin/murmly toggle-session"
        )
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly speech session")],
            },
            keys={DESKTOP_ID: [268435521], SESSION_DESKTOP_ID: [268435544]},
        )
        launcher._shortcuts = shortcuts
        session_launcher._shortcuts = shortcuts
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        # The two keys change places.
        installer.install(parse_hotkey("Meta+A"), parse_hotkey("Meta+X"))

        self.assertEqual("Meta+A", launcher.declared_hotkey())
        self.assertEqual("Meta+X", session_launcher.declared_hotkey())

    def test_taking_the_other_murmly_hotkey_says_how_to_move_it(self) -> None:
        """Still refused -- naming one key must not silently unbind the other.

        But the generic message sends the person to their desktop settings to
        release a binding Murmly wrote and Murmly can move, and names Murmly as
        the conflicting application, which reads as a bug rather than a choice.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY, HotkeyConflictError

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/bin/murmly toggle")
        session_launcher = FakeLauncher(
            declared="Meta+A", purpose=SESSION_HOTKEY, entrypoint="/bin/murmly toggle-session"
        )
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly speech session")],
            },
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [268435521]},
        )
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        with self.assertRaises(HotkeyConflictError) as raised:
            installer.install(parse_hotkey("Meta+A"))

        message = str(raised.exception)
        self.assertIn("Murmly's own hotkey", message)
        self.assertIn("murmly install", message)
        self.assertNotIn("desktop shortcut settings", message)
        self.assertEqual([], launcher.registrations, "the refusal wrote a launcher")

    def test_a_key_moving_between_purposes_is_released_before_it_is_claimed(self) -> None:
        """The desktop delivers a key to whichever component claimed it first.

        Writing the new claim while the old one still holds it binds nothing, so
        the release has to come first.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/bin/murmly toggle")
        session_launcher = FakeLauncher(
            declared="Meta+A", purpose=SESSION_HOTKEY, entrypoint="/bin/murmly toggle-session"
        )
        shortcuts = OwnerRegistry(
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner(SESSION_DESKTOP_ID, "murmly speech session")],
            },
            keys={DESKTOP_ID: [268435521], SESSION_DESKTOP_ID: [268435544]},
        )
        launcher._shortcuts = shortcuts
        session_launcher._shortcuts = shortcuts
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        installer.install(parse_hotkey("Meta+A"), parse_hotkey("Meta+X"))

        self.assertGreaterEqual(
            launcher.unregistrations, 1, "the window launcher never released Meta+X"
        )
        self.assertGreaterEqual(
            session_launcher.unregistrations, 1, "the session launcher never released Meta+A"
        )

    def test_an_unconfirmed_launcher_survives_a_later_failure(self) -> None:
        """Its binding is persisted for the next login, which is the one thing
        it still has going for it. A rollback that removes it takes that away
        and the error names only the other key.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import (
            DESKTOP_ID,
            SESSION_DESKTOP_ID,
            SESSION_HOTKEY,
            HotkeyNotConfirmedError,
            InstallError,
        )

        launcher = FakeLauncher(fail=HotkeyNotConfirmedError("slow", parse_hotkey("Meta+X")))
        session_launcher = FakeLauncher(purpose=SESSION_HOTKEY)
        # The session key verifies as a different keycode, so the second
        # registration fails for a reason that does roll back.
        shortcuts = OwnerRegistry(
            owners={268435544: [owner(DESKTOP_ID, "murmly")]},
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [999]},
        )
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        with self.assertRaises(InstallError) as raised:
            installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        self.assertEqual(
            0, launcher.unregistrations, "a binding kept for the next login was rolled back"
        )
        self.assertIn("Meta+X", str(raised.exception))

    def test_a_failed_install_leaves_a_hotkey_it_never_touched_bound(self) -> None:
        """Rollback undoes this run, not the installation.

        Rebinding the window hotkey on a two-hotkey installation must not
        release the session hotkey. It was bound and working before the command
        was typed, this run never mentioned it, and its loss would be reported
        nowhere -- the error names the key that failed.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_HOTKEY, InstallError

        launcher = FakeLauncher()
        session_launcher = FakeLauncher(
            declared="Meta+A",
            purpose=SESSION_HOTKEY,
            entrypoint="/bin/murmly toggle-session",
        )
        # The desktop resolves the window launcher to a different key, so this
        # install fails after writing it.
        shortcuts = OwnerRegistry(keys={DESKTOP_ID: [268435545]})
        installer = make_installer(
            launcher=launcher, session_launcher=session_launcher, shortcuts=shortcuts
        )

        with self.assertRaises(InstallError):
            installer.install(parse_hotkey("Meta+X"))

        self.assertEqual(1, launcher.unregistrations, "this run's launcher was left behind")
        self.assertEqual(
            0, session_launcher.unregistrations, "a hotkey this run never touched was released"
        )
        self.assertEqual("Meta+A", session_launcher.declared_hotkey())

    def test_conflict_introduced_after_the_preflight_check_fails_and_cleans_up(self) -> None:
        """The pre-flight saw a free key, but a second claimant appeared before
        verification. Plasma reports both owners and delivers to neither of
        ours, so this must fail rather than report success."""
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, InstallError

        class RacingRegistry(OwnerRegistry):
            def __init__(self) -> None:
                super().__init__(keys={DESKTOP_ID: [268435544]})
                self.lookups = 0

            def owners_of(self, keycode: int):
                self.lookups += 1
                if self.lookups == 1:  # pre-flight: nobody holds it
                    return []
                return [owner(DESKTOP_ID, "murmly"), owner("net.local.other.desktop", "intruder")]

        service, launcher = FakeService(), FakeLauncher()
        installer = make_installer(service=service, launcher=launcher, shortcuts=RacingRegistry())

        with self.assertRaises(InstallError) as raised:
            installer.install(parse_hotkey("Meta+X"))

        self.assertIn("intruder", str(raised.exception))
        self.assertEqual(1, launcher.unregistrations)
        self.assertEqual(1, service.removes)

    def test_preexisting_double_bind_is_refused_at_preflight(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, HotkeyConflictError

        service, launcher = FakeService(), FakeLauncher()
        shortcuts = OwnerRegistry(
            keys={DESKTOP_ID: [268435544]},
            owners={268435544: [owner(DESKTOP_ID, "murmly"), owner("net.local.other.desktop", "intruder")]},
        )
        installer = make_installer(service=service, launcher=launcher, shortcuts=shortcuts)

        with self.assertRaises(HotkeyConflictError) as raised:
            installer.install(parse_hotkey("Meta+X"))

        self.assertIn("intruder", str(raised.exception))
        self.assertEqual([], service.installs, "a refusal must not install anything")

    def test_no_owner_after_registration_fails(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, InstallError

        shortcuts = OwnerRegistry(keys={DESKTOP_ID: [268435544]}, owners={})
        installer = make_installer(shortcuts=shortcuts)

        with self.assertRaises(InstallError):
            installer.install(parse_hotkey("Meta+X"))

    def test_rollback_preserves_a_preexisting_service(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, InstallError

        service = FakeService(installed=True, entrypoint="/bin/murmly")
        shortcuts = OwnerRegistry(keys={DESKTOP_ID: [268435545]})
        installer = make_installer(service=service, shortcuts=shortcuts)

        with self.assertRaises(InstallError):
            installer.install(parse_hotkey("Meta+X"))

        self.assertEqual(0, service.removes, "a service this run did not create must survive rollback")
        self.assertTrue(service.is_installed)

    def test_rollback_tolerates_a_gnome_launchers_desktop_query_error(self) -> None:
        """`_rollback` must catch `DesktopQueryError` too: GNOME's launcher
        raises it, not `InstallError`, for a gsettings failure while
        unregistering the very entry rollback is trying to undo."""
        from murmly.desktop import DesktopQueryError
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, InstallError

        class FailsToUnregister(FakeLauncher):
            def unregister(self):
                raise DesktopQueryError("gsettings is unreachable")

        shortcuts = OwnerRegistry(keys={DESKTOP_ID: [268435544]}, owners={})
        launcher = FailsToUnregister()
        installer = make_installer(shortcuts=shortcuts, launcher=launcher)

        # `_verify` raises InstallError ("no owner reported"); rollback then
        # tries to undo the just-written launcher and must not let the
        # launcher's own DesktopQueryError replace that original failure.
        with self.assertRaises(InstallError) as raised:
            installer.install(parse_hotkey("Meta+X"))

        self.assertIn("no owner", str(raised.exception))

    def test_unconfirmed_binding_keeps_the_launcher(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyNotConfirmedError

        launcher = FakeLauncher(fail=HotkeyNotConfirmedError("timed out"))
        installer = make_installer(launcher=launcher)

        with self.assertRaises(HotkeyNotConfirmedError):
            installer.install(parse_hotkey("Meta+X"))

        self.assertEqual(0, launcher.unregistrations, "the binding is persisted for next login")

    def test_success_message_does_not_claim_delivery(self) -> None:
        from murmly.hotkey import parse_hotkey

        outcome = make_installer().install(parse_hotkey("Meta+X"))

        joined = " ".join(outcome.messages)
        self.assertIn("Press it once to confirm", joined)
        self.assertIn("only a keypress proves", joined)


class SessionScopeTests(unittest.TestCase):
    def test_unsupported_desktop_installs_service_and_explains_manual_binding(self) -> None:
        from murmly.hotkey import parse_hotkey

        service, launcher = FakeService(), FakeLauncher()
        installer = make_installer(
            service=service,
            launcher=launcher,
            session=FakeSession(supported=False, detail="Hotkey registration requires KDE Plasma."),
        )

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.service_installed)
        self.assertFalse(outcome.hotkey_registered)
        self.assertEqual([], launcher.registrations)
        joined = " ".join(outcome.messages)
        # Built through `Path`, not a bare literal -- see
        # `test_reinstall_repairs_a_stale_entrypoint` for why.
        self.assertIn(f"{Path('/bin/murmly')} toggle", joined)
        self.assertIn("KDE Plasma", joined)

    def test_an_unsupported_desktop_names_the_session_command_too(self) -> None:
        """A requested key that is silently dropped is worse than a refused one.

        The install reports success, nothing mentions the second hotkey, and the
        person has no way to tell it was never bound -- so the manual-binding
        instructions have to cover every purpose that was asked for.
        """
        from murmly.hotkey import parse_hotkey

        installer = make_installer(
            session=FakeSession(supported=False, detail="Hotkey registration requires KDE Plasma."),
        )

        outcome = installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        joined = " ".join(outcome.messages)
        # Built through `Path`, not a bare literal -- see
        # `test_reinstall_repairs_a_stale_entrypoint` for why.
        self.assertIn(f"{Path('/bin/murmly')} toggle", joined)
        self.assertIn(f"{Path('/bin/murmly')} toggle-session", joined)

    def test_a_one_key_install_is_not_told_to_bind_a_second(self) -> None:
        """Binding one hotkey is a deliberate path, not an incomplete install."""
        from murmly.hotkey import parse_hotkey

        installer = make_installer(
            session=FakeSession(supported=False, detail="Hotkey registration requires KDE Plasma."),
        )

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertNotIn("toggle-session", " ".join(outcome.messages))

    def test_unverified_session_proceeds_and_says_so(self) -> None:
        from murmly.hotkey import parse_hotkey

        installer = make_installer(
            session=FakeSession(verified=True, detail="x"),
        )
        outcome = installer.install(parse_hotkey("Meta+X"))
        self.assertTrue(outcome.hotkey_registered)

        installer = make_installer(
            session=FakeSession(verified=False, detail="unverified on this session type")
        )
        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.hotkey_registered)
        self.assertFalse(outcome.session_verified)
        self.assertIn("unverified", " ".join(outcome.messages))

    def test_user_override_is_reported_and_left_alone(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID

        launcher = FakeLauncher(override="Meta+Z")
        shortcuts = OwnerRegistry(
            keys={DESKTOP_ID: [268435544]},
            owners={268435544: [owner(DESKTOP_ID, "murmly")]},
        )
        installer = make_installer(launcher=launcher, shortcuts=shortcuts)

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertEqual("Meta+Z", outcome.user_override)
        self.assertIn("override", " ".join(outcome.messages))


class UninstallTests(unittest.TestCase):
    def test_removes_service_and_hotkey(self) -> None:
        service = FakeService(installed=True)
        launcher = FakeLauncher(declared="Meta+X")
        installer = make_installer(service=service, launcher=launcher)

        outcome = installer.uninstall()

        self.assertEqual(1, service.removes)
        self.assertEqual(1, launcher.unregistrations)
        joined = " ".join(outcome.messages)
        self.assertIn("Removed the Murmly service", joined)
        self.assertIn("Released the Murmly hotkey", joined)

    def test_uninstall_with_nothing_installed_succeeds(self) -> None:
        installer = make_installer(service=FakeService(installed=False), launcher=FakeLauncher(declared=None))

        outcome = installer.uninstall()

        self.assertIn("nothing to remove", " ".join(outcome.messages))

    def test_partial_install_is_removed(self) -> None:
        service = FakeService(installed=True)
        installer = make_installer(service=service, launcher=FakeLauncher(declared=None))

        outcome = installer.uninstall()

        self.assertEqual(1, service.removes)
        joined = " ".join(outcome.messages)
        self.assertIn("Removed the Murmly service", joined)
        self.assertNotIn("nothing to remove", joined)

    def test_launcher_failure_still_removes_the_service(self) -> None:
        from murmly.installer import InstallError

        class StubbornLauncher(FakeLauncher):
            def unregister(self):
                raise InstallError("still held")

        service = FakeService(installed=True)
        installer = make_installer(service=service, launcher=StubbornLauncher(declared="Meta+X"))

        outcome = installer.uninstall()

        self.assertEqual(1, service.removes)
        self.assertIn("still held", " ".join(outcome.messages))

    def test_a_gnome_launchers_desktop_query_error_still_removes_the_service(self) -> None:
        """GNOME's launcher raises `DesktopQueryError`, not `InstallError`, for
        a gsettings failure -- uninstall must catch that too, not just the
        Plasma-flavoured exception."""
        from murmly.desktop import DesktopQueryError

        class FailingGnomeLikeLauncher(FakeLauncher):
            def unregister(self):
                raise DesktopQueryError("gsettings is unreachable")

        service = FakeService(installed=True)
        installer = make_installer(service=service, launcher=FailingGnomeLikeLauncher(declared="Meta+X"))

        outcome = installer.uninstall()

        self.assertEqual(1, service.removes)
        self.assertIn("gsettings is unreachable", " ".join(outcome.messages))


class InstallerStatusTests(unittest.TestCase):
    def test_reports_not_installed(self) -> None:
        report = make_installer(service=FakeService(installed=False), launcher=FakeLauncher(None)).status()

        self.assertFalse(report["installed"])
        self.assertIn("murmly install", report["detail"])

    def test_reports_installed_and_held(self) -> None:
        from murmly.installer import DESKTOP_ID

        shortcuts = OwnerRegistry(owners={268435544: [owner(DESKTOP_ID, "murmly")]})
        report = make_installer(
            service=FakeService(installed=True, entrypoint="/bin/murmly"),
            launcher=FakeLauncher(declared="Meta+X"),
            shortcuts=shortcuts,
        ).status()

        self.assertTrue(report["installed"])
        self.assertTrue(report["hotkey_held"])
        self.assertEqual("Meta+X", report["hotkey"])
        self.assertEqual("/bin/murmly", report["entrypoint"])

    def test_reports_hotkey_lost_to_another_application(self) -> None:
        shortcuts = OwnerRegistry(owners={268435544: [owner("plasmashell", "Klipper")]})
        report = make_installer(
            service=FakeService(installed=True),
            launcher=FakeLauncher(declared="Meta+X"),
            shortcuts=shortcuts,
        ).status()

        self.assertFalse(report["hotkey_held"])
        self.assertEqual("Klipper", report["hotkey_holder"])
        self.assertIn("Klipper", report["detail"])

    def test_reports_installed_without_a_hotkey(self) -> None:
        report = make_installer(
            service=FakeService(installed=True), launcher=FakeLauncher(declared=None)
        ).status()

        self.assertTrue(report["installed"])
        self.assertIsNone(report["hotkey"])
        self.assertIn("no hotkey", report["detail"])

    def test_status_never_raises_when_a_query_fails(self) -> None:
        class BrokenRegistry(OwnerRegistry):
            def owners_of(self, keycode: int):
                raise RuntimeError("bus is down")

        report = make_installer(
            service=FakeService(installed=True),
            launcher=FakeLauncher(declared="Meta+X"),
            shortcuts=BrokenRegistry(),
        ).status()

        self.assertIn("bus is down", report["detail"])


class PasteInjectionReportTests(unittest.TestCase):
    """Installation says whether a transcript will reach the focused window."""

    def _messages(self, injection: PasteInjection) -> str:
        from murmly.hotkey import parse_hotkey

        outcome = make_installer(injection=injection).install(parse_hotkey("Meta+X"))
        return "\n".join(outcome.messages)

    def test_a_usable_injector_is_named(self) -> None:
        report = self._messages(PasteInjection("ydotool", ("ydotool", "key", "29:1")))

        self.assertIn("pasted into the focused window with ydotool", report)

    def test_nothing_installed_reports_the_remedy_without_failing(self) -> None:
        from murmly.hotkey import parse_hotkey

        injection = PasteInjection(
            None,
            None,
            reason="No Wayland paste injector is installed; install wtype or ydotool.",
            remedy=("sudo dnf install ydotool", "sudo systemctl enable --now ydotool.service"),
        )
        service = FakeService()
        outcome = make_installer(service=service, injection=injection).install(parse_hotkey("Meta+X"))
        report = "\n".join(outcome.messages)

        self.assertTrue(outcome.service_installed)
        self.assertTrue(outcome.hotkey_registered)
        self.assertIn("copied to the clipboard but not pasted", report)
        self.assertIn("sudo dnf install ydotool", report)
        self.assertEqual(1, len(service.installs))

    def test_an_installed_but_unusable_injector_reports_that_it_cannot_run(self) -> None:
        report = self._messages(
            PasteInjection(
                None,
                None,
                reason="wtype is installed but cannot inject in this session: no virtual keyboard",
                remedy=("sudo dnf install ydotool",),
            )
        )

        self.assertIn("installed but cannot inject", report)
        self.assertIn("sudo dnf install ydotool", report)

    def test_a_failing_selector_never_fails_the_install(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import Installer, DESKTOP_ID
        from murmly.platform import OperatingSystem, PlatformProfile

        def explode() -> PasteInjection:
            raise OSError("selection blew up")

        hotkey_code = 268435544
        installer = Installer(
            service=FakeService(),
            launcher=FakeLauncher(),
            shortcuts=OwnerRegistry(keys={DESKTOP_ID: [hotkey_code]}, owners={hotkey_code: [owner(DESKTOP_ID, "murmly")]}),
            session=FakeSession(),
            entrypoint_resolver=lambda: Path("/bin/murmly"),
            injection_selector=explode,
            record_store=FakeRecordStore(),
            # See `make_installer`'s own default: unpinned, this would
            # otherwise resolve to the real host's platform.
            profile=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
        )

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.hotkey_registered)


class TwoHotkeyTests(unittest.TestCase):
    """Each binding is claimed, verified, released and reported on its own."""

    @staticmethod
    def _shortcuts(window_code: int = 268435544, session_code: int = 268435521):
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID

        return OwnerRegistry(
            keys={DESKTOP_ID: [window_code], SESSION_DESKTOP_ID: [session_code]},
            owners={
                window_code: [owner(DESKTOP_ID, "murmly")],
                session_code: [owner(SESSION_DESKTOP_ID, "murmly speech session")],
            },
        )

    def test_both_hotkeys_are_bound_and_verified_independently(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import SESSION_HOTKEY

        window, session = FakeLauncher(), FakeLauncher(purpose=SESSION_HOTKEY)
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=self._shortcuts()
        )

        outcome = installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        self.assertEqual([(Path("/bin/murmly"), "Meta+X")], window.registrations)
        self.assertEqual([(Path("/bin/murmly"), "Meta+A")], session.registrations)
        self.assertTrue(outcome.hotkey_registered)
        self.assertTrue(outcome.session_hotkey_registered)

    def test_a_registered_launcher_runs_its_own_purpose_command(self) -> None:
        """Through the real launcher, not the template it is built from.

        Asserting `launcher_text` output proves the template takes a command,
        not that each launcher passes its own -- a session launcher writing
        `toggle` would produce a second hotkey that dictates into the focused
        window, and that assertion would not notice.
        """
        from murmly.hotkey import parse_hotkey
        from murmly.installer import SESSION_HOTKEY

        with tempfile.TemporaryDirectory() as temp_dir:
            window = make_launcher(temp_dir, FakeShortcuts(present=True))
            session = make_launcher(
                temp_dir, FakeShortcuts(present=True), purpose=SESSION_HOTKEY
            )

            window.register(Path("/bin/murmly"), parse_hotkey("Meta+X"))
            session.register(Path("/bin/murmly"), parse_hotkey("Meta+A"))

            self.assertEqual("/bin/murmly toggle", window.declared_entrypoint())
            self.assertEqual("/bin/murmly toggle-session", session.declared_entrypoint())
            self.assertNotEqual(window.launcher_path, session.launcher_path)

    def test_each_launcher_carries_its_own_command(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import SESSION_HOTKEY, WINDOW_HOTKEY, launcher_text

        window = launcher_text(
            Path("/bin/murmly"),
            parse_hotkey("Meta+X"),
            name=WINDOW_HOTKEY.name,
            command=WINDOW_HOTKEY.command,
        )
        session = launcher_text(
            Path("/bin/murmly"),
            parse_hotkey("Meta+A"),
            name=SESSION_HOTKEY.name,
            command=SESSION_HOTKEY.command,
        )

        self.assertIn("Exec=/bin/murmly toggle\n", window)
        self.assertIn("Exec=/bin/murmly toggle-session\n", session)
        self.assertIn("X-KDE-Shortcuts=Meta+A", session)

    def test_the_two_launchers_are_separate_desktop_entries(self) -> None:
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        with tempfile.TemporaryDirectory() as temp_dir:
            window = make_launcher(temp_dir, OwnerRegistry())
            session = make_launcher(temp_dir, OwnerRegistry(), purpose=SESSION_HOTKEY)

            self.assertEqual(DESKTOP_ID, window.launcher_path.name)
            self.assertEqual(SESSION_DESKTOP_ID, session.launcher_path.name)
            self.assertNotEqual(window.launcher_path, session.launcher_path)

    def test_one_hotkey_owned_by_another_application_binds_neither(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, HotkeyConflictError, SESSION_HOTKEY

        window, session = FakeLauncher(), FakeLauncher(purpose=SESSION_HOTKEY)
        shortcuts = OwnerRegistry(
            keys={DESKTOP_ID: [268435544]},
            owners={268435521: [owner("plasmashell", "Klipper")]},
        )
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=shortcuts
        )

        with self.assertRaises(HotkeyConflictError) as raised:
            installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        self.assertIn("Meta+A", str(raised.exception))
        self.assertIn("Klipper", str(raised.exception))
        self.assertEqual([], window.registrations, "the other hotkey was bound anyway")
        self.assertEqual([], session.registrations)

    def test_the_same_key_for_both_purposes_is_refused_naming_the_collision(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import HotkeyConflictError, SESSION_HOTKEY

        window, session = FakeLauncher(), FakeLauncher(purpose=SESSION_HOTKEY)
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=self._shortcuts()
        )

        with self.assertRaises(HotkeyConflictError) as raised:
            installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+X"))

        self.assertIn("Meta+X", str(raised.exception))
        self.assertIn("both", str(raised.exception))
        self.assertEqual([], window.registrations)
        self.assertEqual([], session.registrations)

    def test_installing_without_a_second_hotkey_binds_the_first_alone(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import SESSION_HOTKEY

        window, session = FakeLauncher(), FakeLauncher(purpose=SESSION_HOTKEY)
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=self._shortcuts()
        )

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.hotkey_registered)
        self.assertFalse(outcome.session_hotkey_registered)
        self.assertEqual([], session.registrations)

    def test_uninstall_releases_both_bindings(self) -> None:
        from murmly.installer import SESSION_HOTKEY

        window = FakeLauncher(declared="Meta+X")
        session = FakeLauncher(declared="Meta+A", purpose=SESSION_HOTKEY)
        installer = make_installer(launcher=window, session_launcher=session)

        outcome = installer.uninstall()

        self.assertEqual(1, window.unregistrations)
        self.assertEqual(1, session.unregistrations)
        self.assertIn("Released the Murmly hotkey.", outcome.messages)

    def test_uninstall_succeeds_when_only_one_binding_is_present(self) -> None:
        from murmly.installer import SESSION_HOTKEY

        window = FakeLauncher(declared="Meta+X")
        session = FakeLauncher(purpose=SESSION_HOTKEY)
        installer = make_installer(launcher=window, session_launcher=session)

        outcome = installer.uninstall()

        self.assertIn("Released the Murmly hotkey.", outcome.messages)
        self.assertNotIn(
            "Murmly was not installed; there was nothing to remove.", outcome.messages
        )

    def test_uninstall_with_nothing_bound_still_reports_nothing_to_remove(self) -> None:
        from murmly.installer import SESSION_HOTKEY

        installer = make_installer(
            service=FakeService(installed=False),
            launcher=FakeLauncher(),
            session_launcher=FakeLauncher(purpose=SESSION_HOTKEY),
        )

        outcome = installer.uninstall()

        self.assertIn("Murmly was not installed; there was nothing to remove.", outcome.messages)

    def test_diagnostics_name_each_binding_with_its_purpose(self) -> None:
        from murmly.installer import SESSION_HOTKEY

        window = FakeLauncher(declared="Meta+X")
        session = FakeLauncher(declared="Meta+A", purpose=SESSION_HOTKEY)
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=self._shortcuts()
        )

        report = installer.status()

        purposes = {entry["purpose"]: entry for entry in report["hotkeys"]}
        self.assertEqual("Meta+X", purposes["window"]["hotkey"])
        self.assertEqual("Meta+A", purposes["session"]["hotkey"])
        self.assertTrue(purposes["window"]["held"])
        self.assertTrue(purposes["session"]["held"])
        self.assertIn("focused window", purposes["window"]["description"])
        self.assertIn("speech session", purposes["session"]["description"])

    def test_diagnostics_report_an_unbound_second_hotkey_without_calling_it_a_failure(
        self,
    ) -> None:
        from murmly.installer import SESSION_HOTKEY

        window = FakeLauncher(declared="Meta+X")
        session = FakeLauncher(purpose=SESSION_HOTKEY)
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=self._shortcuts()
        )

        report = installer.status()

        purposes = {entry["purpose"]: entry for entry in report["hotkeys"]}
        self.assertIsNone(purposes["session"]["hotkey"])
        self.assertFalse(purposes["session"]["held"])
        self.assertIn("No hotkey is bound", purposes["session"]["detail"])
        self.assertTrue(report["hotkey_held"], "the bound hotkey was reported as a failure")

    def test_a_binding_lost_to_another_application_is_reported_alone(self) -> None:
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        window = FakeLauncher(declared="Meta+X")
        session = FakeLauncher(declared="Meta+A", purpose=SESSION_HOTKEY)
        shortcuts = OwnerRegistry(
            keys={DESKTOP_ID: [268435544], SESSION_DESKTOP_ID: [268435521]},
            owners={
                268435544: [owner(DESKTOP_ID, "murmly")],
                268435521: [owner("plasmashell", "Klipper")],
            },
        )
        installer = make_installer(
            launcher=window, session_launcher=session, shortcuts=shortcuts
        )

        report = installer.status()

        purposes = {entry["purpose"]: entry for entry in report["hotkeys"]}
        self.assertTrue(purposes["window"]["held"])
        self.assertFalse(purposes["session"]["held"])
        self.assertEqual("Klipper", purposes["session"]["holder"])


class HotkeyRecordPersistenceTests(unittest.TestCase):
    """Task 5.4: `install`/`uninstall` keep the record in step with what is
    actually bound, on every platform -- not read from yet anywhere, but ready
    the day an in-process backend (sections 8, 13) needs it."""

    def test_install_writes_the_bound_purpose_to_the_record(self) -> None:
        from murmly.hotkey import parse_hotkey

        record = FakeRecordStore()
        installer = make_installer(record_store=record)

        installer.install(parse_hotkey("Meta+X"))

        self.assertEqual({"window": "Meta+X"}, record.read())

    def test_install_of_both_hotkeys_records_both(self) -> None:
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, SESSION_DESKTOP_ID, SESSION_HOTKEY

        record = FakeRecordStore()
        window_code, session_code = 268435544, 268435521
        shortcuts = OwnerRegistry(
            keys={DESKTOP_ID: [window_code], SESSION_DESKTOP_ID: [session_code]},
            owners={window_code: [owner(DESKTOP_ID, "murmly")], session_code: [owner(SESSION_DESKTOP_ID, "murmly")]},
        )
        installer = make_installer(
            launcher=FakeLauncher(),
            session_launcher=FakeLauncher(purpose=SESSION_HOTKEY),
            shortcuts=shortcuts,
            record_store=record,
        )

        installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        self.assertEqual({"window": "Meta+X", "session": "Meta+A"}, record.read())

    def test_installing_one_purpose_does_not_drop_the_others_recorded_entry(self) -> None:
        """The record reflects actual bound state, built from both launchers'
        `declared_hotkey()` -- not only the keys this run requested."""
        from murmly.hotkey import parse_hotkey
        from murmly.installer import SESSION_HOTKEY

        record = FakeRecordStore()
        # The session purpose is already bound before this run, as if an
        # earlier `install` had claimed it -- with a matching entrypoint, so
        # it is not treated as stale and re-registered by this run. Built
        # through `Path`, not a bare literal -- see
        # `test_reinstall_repairs_a_stale_entrypoint` for why.
        session_launcher = FakeLauncher(
            declared="Meta+A",
            purpose=SESSION_HOTKEY,
            entrypoint=f"{Path('/bin/murmly')} toggle-session",
        )
        installer = make_installer(record_store=record, session_launcher=session_launcher)

        installer.install(parse_hotkey("Meta+X"))

        self.assertEqual({"window": "Meta+X", "session": "Meta+A"}, record.read())

    def test_uninstall_clears_the_record(self) -> None:
        from murmly.hotkey import parse_hotkey

        record = FakeRecordStore()
        installer = make_installer(record_store=record)
        installer.install(parse_hotkey("Meta+X"))
        self.assertEqual({"window": "Meta+X"}, record.read())

        installer.uninstall()

        self.assertEqual({}, record.read())

    def test_uninstall_clears_the_record_even_with_nothing_installed(self) -> None:
        record = FakeRecordStore()
        installer = make_installer(service=FakeService(installed=False), launcher=FakeLauncher(declared=None), record_store=record)

        installer.uninstall()

        self.assertEqual({}, record.read())

    def test_a_record_that_cannot_be_written_does_not_fail_an_otherwise_successful_install(self) -> None:
        """A hotkey is already bound and verified by the time the record is
        written -- an unwritable config directory must not turn that into a
        reported failure, for a file nothing on Linux reads yet."""
        from murmly.hotkey import parse_hotkey

        class RaisingRecordStore(FakeRecordStore):
            def write(self, bindings):
                raise OSError("Read-only file system")

        installer = make_installer(record_store=RaisingRecordStore())

        outcome = installer.install(parse_hotkey("Meta+X"))

        self.assertTrue(outcome.hotkey_registered)

    def test_a_record_that_cannot_be_cleared_does_not_fail_an_otherwise_successful_uninstall(self) -> None:
        class RaisingRecordStore(FakeRecordStore):
            def remove(self):
                raise OSError("Read-only file system")

        installer = make_installer(record_store=RaisingRecordStore())

        outcome = installer.uninstall()

        self.assertIn("Read-only file system", " ".join(outcome.messages))


class BackendDispatchTests(unittest.TestCase):
    """Which desktop `install`/`status`/`uninstall` actually talk to is chosen
    once, from the resolved session, only when nothing was pinned -- every
    other test in this file pins `shortcuts`/`launcher`/`session_launcher`
    directly and never touches this path at all."""

    def test_a_pinned_backend_is_used_exactly_as_given_regardless_of_session(self) -> None:
        """Task 4's dispatch must not change a single existing test's
        behaviour: pinning any of the three is still the whole story."""
        from murmly.installer import Installer

        shortcuts = OwnerRegistry()
        launcher = FakeLauncher()
        installer = Installer(
            service=FakeService(),
            launcher=launcher,
            shortcuts=shortcuts,
            session=FakeSession(),
            entrypoint_resolver=lambda: Path("/bin/murmly"),
        )

        self.assertIs(launcher, installer._launcher)
        self.assertIs(shortcuts, installer._shortcuts)

    def test_gnome_session_resolves_to_the_gnome_backend(self) -> None:
        from murmly.desktop import GnomeShortcutLauncher, GnomeShortcuts, detect_desktop_session
        from murmly.installer import Installer, SESSION_HOTKEY

        session = detect_desktop_session(
            {"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}
        )
        self.assertTrue(session.supported)  # the fixture actually exercises the GNOME branch
        installer = Installer(service=FakeService(), session=session)

        self.assertIsInstance(installer._shortcuts, GnomeShortcuts)
        self.assertIsInstance(installer._launcher, GnomeShortcutLauncher)
        self.assertIsInstance(installer._session_launcher, GnomeShortcutLauncher)
        self.assertIs(installer._shortcuts, installer._launcher._shortcuts)
        self.assertIs(installer._shortcuts, installer._session_launcher._shortcuts)
        self.assertEqual(SESSION_HOTKEY, installer._session_launcher.purpose)

    def test_plasma_session_still_resolves_to_the_plasma_backend(self) -> None:
        from murmly.desktop import PlasmaShortcuts, detect_desktop_session
        from murmly.installer import Installer, ShortcutLauncher

        session = detect_desktop_session({"XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
        installer = Installer(service=FakeService(), session=session)

        self.assertIsInstance(installer._shortcuts, PlasmaShortcuts)
        self.assertIsInstance(installer._launcher, ShortcutLauncher)

    def test_an_unsupported_desktop_also_resolves_to_the_plasma_shapes(self) -> None:
        """Unchanged from before per-desktop selection existed: `install()`
        never reaches these properties for an unsupported desktop (it checks
        `session.supported` first), and `status()`/`uninstall()` on such a
        desktop only ever read a launcher file that cannot exist there."""
        from murmly.desktop import PlasmaShortcuts, detect_desktop_session
        from murmly.installer import Installer, ShortcutLauncher

        session = detect_desktop_session(
            {"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        )
        installer = Installer(service=FakeService(), session=session)

        self.assertIsInstance(installer._shortcuts, PlasmaShortcuts)
        self.assertIsInstance(installer._launcher, ShortcutLauncher)

    def test_the_backend_is_resolved_once_and_cached(self) -> None:
        from murmly.desktop import DesktopSession, Desktop, OverlayBackend
        from murmly.installer import Installer

        base = DesktopSession(
            is_plasma=False,
            session_type="wayland",
            backend=OverlayBackend.WAYLAND,
            supported=True,
            verified=False,
            detail="GNOME on wayland.",
            desktop=Desktop.GNOME,
        )
        installer = Installer(service=FakeService(), session=base)

        first = installer._shortcuts
        second = installer._shortcuts
        third = installer._launcher

        self.assertIs(first, second)
        self.assertIs(first, third._shortcuts)


class ServiceBackendDispatchTests(unittest.TestCase):
    """`Installer(service=None)` -- what `cli.py` actually constructs --
    must instantiate whatever `SERVICE_MANAGEMENT` selects, not hand back
    the class itself: `BackendCandidate.load()` returns a class (proven in
    `test_platform.py`), so `Installer` is the one seam that has to call it
    again to get something with `install`/`is_installed`/`status` to call.
    """

    def test_an_unpinned_installer_on_linux_gets_a_working_systemd_instance(self) -> None:
        from murmly.installer import Installer, UserService
        from murmly.platform import OperatingSystem, PlatformProfile

        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
        installer = Installer(profile=profile)

        self.assertIsInstance(installer._service, UserService)
        # The exact bug shape this guards: a bare class in `_service` makes
        # `is_installed` the property *object* (truthy, not a bool) rather
        # than evaluating it against a real instance.
        self.assertIsInstance(installer._service.is_installed, bool)

    def test_an_unpinned_installer_on_windows_gets_a_working_task_scheduler_instance(self) -> None:
        """Not `.is_installed` here: the real `WindowsUserService` shells out
        to `schtasks`, which does not exist on this machine -- exactly the
        real Win32/`schtasks` half every other Windows test in this module
        avoids by injecting a fake `run_command`. `isinstance` alone is
        already the assertion the class-vs-instance bug corrupts: a bare
        class is not an instance of itself."""
        from murmly.installer import Installer, WindowsUserService
        from murmly.platform import OperatingSystem, PlatformProfile

        profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")
        installer = Installer(profile=profile)

        self.assertIsInstance(installer._service, WindowsUserService)


class EndToEndBackendSelectionTests(unittest.TestCase):
    """Task 4's other declared-but-unfixed gap: every test above either pins
    `shortcuts`/`launcher` outright (bypassing `_backend_for`) or only checks
    `isinstance` on what auto-selection resolved to. Nothing drove the real
    path a GNOME or Plasma user takes -- `Installer()` with nothing pinned,
    resolving the desktop, selecting the backend, and actually installing and
    uninstalling through it. `run_command=`/`env=` are what make that
    possible without touching a real desktop bus or a real home directory.
    """

    def test_gnome_backend_installs_and_uninstalls_end_to_end(self) -> None:
        from murmly.desktop import Desktop, DesktopSession, GnomeShortcutLauncher, GnomeShortcuts, OverlayBackend
        from murmly.hotkey import parse_hotkey
        from murmly.installer import Installer
        from murmly.platform import OperatingSystem, PlatformProfile
        from test_desktop_gnome import FakeGsettings

        fake = FakeGsettings()
        session = DesktopSession(
            is_plasma=False,
            session_type="wayland",
            backend=OverlayBackend.WAYLAND,
            supported=True,
            verified=False,
            detail="GNOME on wayland.",
            desktop=Desktop.GNOME,
        )
        record_store = FakeRecordStore()
        installer = Installer(
            service=FakeService(),
            session=session,
            entrypoint_resolver=lambda: Path("/bin/murmly"),
            injection_selector=lambda: PasteInjection("xdotool", ("xdotool",)),
            record_store=record_store,
            run_command=fake,
            # Left to the real host's own `resolve_platform()`, a Windows
            # runner would send `install()` into the in-process branch
            # instead of the GNOME desktop flow this test means to drive --
            # see `make_installer`'s own default above for the same seam.
            profile=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
        )

        # Resolved through `_backend_for`'s GNOME branch, not pinned.
        self.assertIsInstance(installer._shortcuts, GnomeShortcuts)
        self.assertIsInstance(installer._launcher, GnomeShortcutLauncher)
        self.assertIsInstance(installer._session_launcher, GnomeShortcutLauncher)

        outcome = installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

        # Proves the injected fake was actually reached through `_backend_for`,
        # not bypassed in favour of some default -- the thing this gap is
        # about closing, not merely that the auto-selected type is right.
        self.assertTrue(fake.calls)
        self.assertTrue(outcome.hotkey_registered)
        self.assertTrue(outcome.session_hotkey_registered)
        self.assertEqual("<Super>x", fake.value(installer._launcher.path, "binding"))
        self.assertEqual("<Super>a", fake.value(installer._session_launcher.path, "binding"))
        self.assertEqual({"window": "Meta+X", "session": "Meta+A"}, record_store.bindings)

        uninstall_outcome = installer.uninstall()

        self.assertIn("Released the Murmly hotkey.", uninstall_outcome.messages)
        self.assertIsNone(installer._launcher.declared_hotkey())
        self.assertIsNone(installer._session_launcher.declared_hotkey())
        self.assertIsNone(fake.value(installer._launcher.path, "binding"))
        self.assertIsNone(record_store.bindings)

    def test_plasma_backend_installs_and_uninstalls_end_to_end(self) -> None:
        from murmly.desktop import Desktop, DesktopSession, OverlayBackend, PlasmaShortcuts
        from murmly.hotkey import parse_hotkey
        from murmly.installer import DESKTOP_ID, Installer, SESSION_DESKTOP_ID, ShortcutLauncher, default_applications_dir
        from murmly.platform import OperatingSystem, PlatformProfile

        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "XDG_DATA_HOME": str(Path(temp_dir) / "data"),
                "XDG_CONFIG_HOME": str(Path(temp_dir) / "config"),
            }
            applications_dir = default_applications_dir(env)
            bus = FakePlasmaBus(applications_dir)
            session = DesktopSession(
                is_plasma=True,
                session_type="x11",
                backend=OverlayBackend.X11,
                supported=True,
                verified=True,
                detail="KDE Plasma on x11.",
                desktop=Desktop.PLASMA,
            )
            record_store = FakeRecordStore()
            installer = Installer(
                service=FakeService(),
                session=session,
                entrypoint_resolver=lambda: Path("/bin/murmly"),
                injection_selector=lambda: PasteInjection("xdotool", ("xdotool",)),
                record_store=record_store,
                run_command=bus,
                env=env,
                # See the GNOME test above: the real host's own resolution
                # would otherwise send this into the in-process branch on a
                # Windows runner instead of the Plasma desktop flow.
                profile=PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64"),
            )

            # Resolved through `_backend_for`'s default (Plasma) branch, not
            # pinned, and reading/writing the scratch directory `env` names
            # rather than the developer's own `~/.local/share/applications`.
            self.assertIsInstance(installer._shortcuts, PlasmaShortcuts)
            self.assertIsInstance(installer._launcher, ShortcutLauncher)
            self.assertEqual(applications_dir / DESKTOP_ID, installer._launcher.launcher_path)

            outcome = installer.install(parse_hotkey("Meta+X"), parse_hotkey("Meta+A"))

            # Proves the injected fake bus was actually reached through
            # `_backend_for`, not bypassed for some default -- without this, a
            # dropped `run_command=` kwarg would default to real
            # `subprocess.run` and only fail loudly on a machine with no
            # `busctl`, which CI may still have.
            self.assertTrue(bus.calls)
            self.assertTrue(outcome.hotkey_registered)
            self.assertTrue(outcome.session_hotkey_registered)
            self.assertTrue((applications_dir / DESKTOP_ID).is_file())
            self.assertTrue((applications_dir / SESSION_DESKTOP_ID).is_file())
            self.assertEqual("Meta+X", installer._launcher.declared_hotkey())
            self.assertEqual("Meta+A", installer._session_launcher.declared_hotkey())
            self.assertEqual({"window": "Meta+X", "session": "Meta+A"}, record_store.bindings)

            uninstall_outcome = installer.uninstall()

            self.assertIn("Released the Murmly hotkey.", uninstall_outcome.messages)
            self.assertFalse((applications_dir / DESKTOP_ID).is_file())
            self.assertFalse((applications_dir / SESSION_DESKTOP_ID).is_file())
            self.assertIsNone(record_store.bindings)


class WindowsSchtasksIntegrationTests(unittest.TestCase):
    """Tasks 8.1 and 8.2, against real `schtasks.exe`: everything
    `WindowsServiceInstallTests` above proves against `FakeSchtasks` is
    exercised here against the real Task Scheduler, following the
    `WindowsPipeSecurityDescriptorIntegrationTests` pattern (`test_platform.py`)
    of skipping in `setUp` on every platform but the one that has the
    mechanism.

    The task name is unique per run (`uuid4`), never `WINDOWS_TASK_NAME`: this
    suite also runs on a developer's own Windows machine, where that name may
    already be a real install, and `/create ... /f` would silently overwrite
    it. `remove()` is registered with `addCleanup` before `install()` runs, so
    a failure partway through -- including `self.fail()` on an assertion --
    still tears the task down rather than leaving it registered against the
    next run of this same test.

    `install()` both creates the task and starts it (`self.start()`, see
    `WindowsUserService.install`), so the entrypoint has to be something
    genuinely safe to run unattended and unprivileged: a batch script that
    idles rather than the real Murmly entrypoint. `install()` appends
    `" daemon"` to whatever path it is given (`WindowsUserService.install`'s
    `run_line`), which a `.bat` file sees as an ignored `%1` and a real
    Murmly entrypoint would not -- one more reason not to point this at one.
    """

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("A Windows kernel is required to drive schtasks.exe")

    def test_full_lifecycle_against_a_real_task(self) -> None:
        import uuid

        from murmly.installer import _windows_path_text

        task_name = f"MurmlyIntegrationTest-{uuid.uuid4().hex[:12]}"
        service = WindowsUserService(task_name=task_name)
        self.addCleanup(service.remove)

        with tempfile.TemporaryDirectory() as temp_dir:
            # Idles for a couple of minutes rather than exiting immediately --
            # long enough for `is_active()`'s `/query` to have something
            # running to observe, per this class's docstring.
            script_path = Path(temp_dir) / "murmly-integration-idle.bat"
            script_path.write_text("@echo off\r\nping -n 120 127.0.0.1 >nul\r\n", encoding="utf-8")

            self.assertIsNone(service.recorded_entrypoint())
            self.assertFalse(service.is_installed)

            service.install(script_path)

            self.assertTrue(service.is_installed)
            self.assertEqual(_windows_path_text(script_path), service.recorded_entrypoint())

            # `install()` already called `start()`, but Task Scheduler does
            # not necessarily report `Running` back the same instant `/run`
            # returns -- this polls rather than asserting on the first read.
            active = False
            for _ in range(20):
                if service.is_active():
                    active = True
                    break
                time.sleep(0.5)
            self.assertTrue(active, "the task never reported Status: Running")

            status = service.status()
            self.assertTrue(status.installed)
            self.assertTrue(status.active)
            self.assertEqual(_windows_path_text(script_path), status.entrypoint)

            self.assertTrue(service.stop())
            self.assertFalse(service.is_active())

            self.assertTrue(service.disable())
            self.assertTrue(service.enable())

            removed = service.remove()
            self.assertTrue(removed)
            self.assertFalse(service.is_installed)

    def test_registers_and_starts_without_asking_for_elevation(self) -> None:
        """Task 8.2's own claim: the process installing Murmly is not itself
        elevated, so a lifecycle that runs to completion at all (the test
        above) already shows `/create` and `/run` asked for nothing this
        session's token could not grant on its own. What is checked here is
        the precondition that makes that reading meaningful: if the *runner's*
        token is already elevated, the test above's success would be
        consistent with 8.2 but would not distinguish it from a runner that
        happens to grant elevated tasks no differently -- so this names that
        case explicitly with `IsUserAnAdmin` rather than leaving it assumed.
        """
        from ctypes import windll

        if bool(windll.shell32.IsUserAnAdmin()):
            self.skipTest(
                "This process is already elevated (IsUserAnAdmin), so a "
                "passing lifecycle above cannot distinguish 'schtasks needs "
                "no elevation' from 'this token already has it'."
            )
        # No assertion beyond the skip guard: the elevation-free schtasks
        # lifecycle itself is `test_full_lifecycle_against_a_real_task`
        # above. Reaching this point unskipped is what makes that other
        # test's pass mean what task 8.2 asks it to mean.
