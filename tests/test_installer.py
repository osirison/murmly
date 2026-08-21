from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from murmly.installer import (
    SERVICE_NAME,
    InstallError,
    UserService,
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
        with tempfile.TemporaryDirectory() as temp_dir:
            interpreter = Path(temp_dir) / "python3"
            interpreter.write_text("", encoding="utf-8")
            entrypoint = Path(temp_dir) / "murmly"
            entrypoint.write_text("", encoding="utf-8")
            entrypoint.chmod(0o644)

            with self.assertRaises(InstallError) as raised:
                resolve_entrypoint(str(interpreter))

            self.assertIn("not executable", str(raised.exception))

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
    ) -> None:
        from murmly.installer import WINDOW_HOTKEY

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

    def unregister(self) -> bool:
        self.unregistrations += 1
        existed, self._declared = self._declared is not None, None
        self._entrypoint = None
        return existed


class FakeSession:
    def __init__(self, supported: bool = True, verified: bool = True, detail: str = "KDE Plasma on x11.") -> None:
        self.supported = supported
        self.verified = verified
        self.detail = detail
        self.is_plasma = supported


class OwnerRegistry:
    """A shortcuts stand-in driven by explicit owner and key tables."""

    def __init__(self, owners=None, keys=None) -> None:
        self._owners = owners or {}
        self._keys = keys or {}

    def owners_of(self, keycode: int):
        return list(self._owners.get(keycode, []))

    def registered_keys(self, component: str):
        return list(self._keys.get(component, []))

    def is_available(self, keycode: int) -> bool:
        return not self._owners.get(keycode)


def owner(component: str, friendly: str = "other"):
    from murmly.desktop import ShortcutOwner

    return ShortcutOwner("_launch", friendly, component, friendly)


def make_installer(
    service=None,
    launcher=None,
    shortcuts=None,
    session=None,
    entrypoint="/bin/murmly",
    injection=None,
    session_launcher=None,
):
    from murmly.installer import DESKTOP_ID, SESSION_HOTKEY, Installer

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
        entrypoint_resolver=lambda: Path(entrypoint),
        injection_selector=lambda: selected,
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

        launcher = FakeLauncher(declared="Meta+X", entrypoint="/bin/murmly toggle")
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
        self.assertEqual("/new/murmly toggle", launcher.declared_entrypoint())

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

        self.assertEqual("/new/murmly toggle", launcher.declared_entrypoint())
        self.assertEqual(
            "/new/murmly toggle-session",
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

        self.assertEqual("/new/murmly toggle", launcher.declared_entrypoint())
        self.assertEqual("/new/murmly toggle-session", session_launcher.declared_entrypoint())


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
        self.assertIn("/bin/murmly toggle", joined)
        self.assertIn("KDE Plasma", joined)

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
