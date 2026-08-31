"""Task 18.8: the GNOME hotkey backend against a fake command runner.

Binding written, read back, a foreign owner detected so a caller can refuse,
and removal taking out exactly what was added -- including the mandated case
where `custom-keybindings` already holds three entries belonging to other
applications.
"""

from __future__ import annotations

import ast
import subprocess
import unittest

from murmly.desktop import (
    GNOME_CUSTOM_KEYBINDING_SCHEMA,
    GNOME_MEDIA_KEYS_SCHEMA,
    GnomeShortcuts,
    GnomeShortcutLauncher,
    gnome_binding_path,
)
from murmly.hotkey import parse_hotkey


class HotkeyPurpose:
    """A minimal stand-in for `installer.HotkeyPurpose` -- desktop.py does not
    import installer.py, so the real one is not reachable here without a
    circular import; the launcher only reads these four attributes."""

    def __init__(self, key: str, desktop_id: str, name: str, command: str) -> None:
        self.key = key
        self.desktop_id = desktop_id
        self.name = name
        self.command = command


WINDOW_PURPOSE = HotkeyPurpose("window", "net.local.murmly.desktop", "murmly", "toggle")
SESSION_PURPOSE = HotkeyPurpose("session", "net.local.murmly-session.desktop", "murmly speech session", "toggle-session")

FOREIGN_PATH_1 = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
FOREIGN_PATH_2 = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom1/"
FOREIGN_PATH_3 = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom2/"
THREE_FOREIGN_PATHS = [FOREIGN_PATH_1, FOREIGN_PATH_2, FOREIGN_PATH_3]


class FakeGsettings:
    """An in-memory dconf stand-in, stateful across calls the way the real
    gsettings/dconf pair is -- `recorded()` in test_desktop.py is stateless and
    cannot model a read-modify-write against what an earlier call wrote."""

    def __init__(self, custom_keybindings: list[str] | None = None) -> None:
        self._list: list[str] = list(custom_keybindings or [])
        self._values: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command, **_kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        _binary, verb, *rest = command
        if verb == "get":
            schema_arg, key = rest
            return self._get(schema_arg, key)
        if verb == "set":
            schema_arg, key, value = rest
            return self._set(schema_arg, key, value)
        if verb == "reset-recursively":
            (schema_arg,) = rest
            return self._reset(schema_arg)
        return self._result(1, stderr=f"unknown verb {verb!r}")

    @staticmethod
    def _path_of(schema_arg: str) -> str | None:
        if ":" not in schema_arg:
            return None
        schema, path = schema_arg.split(":", 1)
        assert schema == GNOME_CUSTOM_KEYBINDING_SCHEMA
        return path

    def _get(self, schema_arg: str, key: str) -> subprocess.CompletedProcess[str]:
        path = self._path_of(schema_arg)
        if path is None:
            assert schema_arg == GNOME_MEDIA_KEYS_SCHEMA
            assert key == "custom-keybindings"
            return self._result(0, stdout=self._format_list())
        raw = self._values.get((path, key), "''")
        return self._result(0, stdout=raw)

    def _set(self, schema_arg: str, key: str, value: str) -> subprocess.CompletedProcess[str]:
        path = self._path_of(schema_arg)
        if path is None:
            assert key == "custom-keybindings"
            self._list = self._parse_list(value)
            return self._result(0)
        self._values[(path, key)] = value
        return self._result(0)

    def _reset(self, schema_arg: str) -> subprocess.CompletedProcess[str]:
        path = self._path_of(schema_arg)
        for key in ("name", "command", "binding"):
            self._values.pop((path, key), None)
        return self._result(0)

    def _format_list(self) -> str:
        if not self._list:
            return "@as []"
        return "[" + ", ".join(repr(p) for p in self._list) + "]"

    @staticmethod
    def _parse_list(value: str) -> list[str]:
        text = value[4:] if value.startswith("@as ") else value
        return ast.literal_eval(text) if text else []

    @staticmethod
    def _result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def value(self, path: str, key: str) -> str | None:
        raw = self._values.get((path, key))
        if raw is None:
            return None
        return ast.literal_eval(raw)


class RegistrationTests(unittest.TestCase):
    def test_binding_is_written_and_read_back(self) -> None:
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)

        launcher.register("/bin/murmly", parse_hotkey("Meta+X"))

        self.assertEqual("Meta+X", launcher.declared_hotkey())
        self.assertEqual("/bin/murmly toggle", launcher.declared_entrypoint())
        self.assertEqual("<Super>x", fake.value(launcher.path, "binding"))
        self.assertEqual("murmly", fake.value(launcher.path, "name"))

    def test_registering_appends_to_three_foreign_entries_without_disturbing_them(self) -> None:
        fake = FakeGsettings(custom_keybindings=list(THREE_FOREIGN_PATHS))
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)

        launcher.register("/bin/murmly", parse_hotkey("Meta+X"))

        self.assertEqual([*THREE_FOREIGN_PATHS, launcher.path], fake._list)

    def test_removal_takes_out_exactly_what_was_added(self) -> None:
        fake = FakeGsettings(custom_keybindings=list(THREE_FOREIGN_PATHS))
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)
        launcher.register("/bin/murmly", parse_hotkey("Meta+X"))

        removed = launcher.unregister()

        self.assertTrue(removed)
        self.assertEqual(THREE_FOREIGN_PATHS, fake._list)
        self.assertIsNone(launcher.declared_hotkey())
        self.assertIsNone(fake.value(launcher.path, "binding"))

    def test_removal_writes_the_typed_empty_array_not_a_bare_list(self) -> None:
        """The GVariant gotcha this backend must not trip on: `gsettings set`
        on a bare `[]` for an `as` key is ambiguous; `@as []` is required."""
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)
        launcher.register("/bin/murmly", parse_hotkey("Meta+X"))

        launcher.unregister()

        set_list_calls = [call for call in fake.calls if call[1] == "set" and call[2] == GNOME_MEDIA_KEYS_SCHEMA]
        self.assertEqual("@as []", set_list_calls[-1][-1])

    def test_removal_when_absent_is_not_an_error(self) -> None:
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)

        self.assertFalse(launcher.unregister())

    def test_two_purposes_share_one_shortcuts_instance_independently(self) -> None:
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        window = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)
        session = GnomeShortcutLauncher(shortcuts, purpose=SESSION_PURPOSE)

        window.register("/bin/murmly", parse_hotkey("Meta+X"))
        session.register("/bin/murmly", parse_hotkey("Meta+A"))

        self.assertEqual("Meta+X", window.declared_hotkey())
        self.assertEqual("Meta+A", session.declared_hotkey())
        self.assertEqual([window.path, session.path], fake._list)

        session.unregister()

        self.assertEqual("Meta+X", window.declared_hotkey())
        self.assertEqual([window.path], fake._list)


class ConflictDetectionTests(unittest.TestCase):
    """`owners_of` is what a caller (Installer's own conflict refusal) bases a
    fail-closed decision on -- GNOME's mechanism does not arbitrate a
    duplicate binding any more than KDE's does."""

    def test_a_foreign_entry_holding_the_target_accelerator_is_reported(self) -> None:
        fake = FakeGsettings(custom_keybindings=list(THREE_FOREIGN_PATHS))
        fake._values[(FOREIGN_PATH_2, "binding")] = repr("<Super>x")
        fake._values[(FOREIGN_PATH_2, "name")] = repr("Totally Reliable App")
        shortcuts = GnomeShortcuts(run_command=fake)

        owners = shortcuts.owners_of(parse_hotkey("Meta+X").keycode)

        self.assertEqual(1, len(owners))
        self.assertEqual("Totally Reliable App", owners[0].component_friendly)
        self.assertEqual(FOREIGN_PATH_2, owners[0].component_unique)

    def test_no_owner_is_reported_for_a_free_key(self) -> None:
        fake = FakeGsettings(custom_keybindings=list(THREE_FOREIGN_PATHS))
        fake._values[(FOREIGN_PATH_1, "binding")] = repr("<Control><Alt>Delete")
        shortcuts = GnomeShortcuts(run_command=fake)

        self.assertEqual([], shortcuts.owners_of(parse_hotkey("Meta+X").keycode))
        self.assertTrue(shortcuts.is_available(parse_hotkey("Meta+X").keycode))

    def test_murmlys_own_binding_is_reported_under_its_desktop_id(self) -> None:
        """Once a launcher has noted its purpose, `owners_of` labels Murmly's
        own entry with the same `desktop_id` Installer's conflict logic
        already compares against, not the raw dconf path."""
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)
        launcher.register("/bin/murmly", parse_hotkey("Meta+X"))

        owners = shortcuts.owners_of(parse_hotkey("Meta+X").keycode)

        self.assertEqual(1, len(owners))
        self.assertEqual(WINDOW_PURPOSE.desktop_id, owners[0].component_unique)

    def test_registered_keys_matches_the_requested_keycode_after_registration(self) -> None:
        fake = FakeGsettings()
        shortcuts = GnomeShortcuts(run_command=fake)
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)
        hotkey = parse_hotkey("Ctrl+Alt+End")

        launcher.register("/bin/murmly", hotkey)

        self.assertEqual([hotkey.keycode], shortcuts.registered_keys(WINDOW_PURPOSE.desktop_id))

    def test_registered_keys_is_empty_for_an_unknown_component(self) -> None:
        shortcuts = GnomeShortcuts(run_command=FakeGsettings())

        self.assertEqual([], shortcuts.registered_keys("net.local.something-else.desktop"))


class NoConfirmationRaisesTests(unittest.TestCase):
    def test_a_write_gsettings_does_not_confirm_raises(self) -> None:
        from murmly.desktop import DesktopQueryError

        class SilentlyIgnoresBinding(FakeGsettings):
            def _set(self, schema_arg, key, value):
                if key == "binding":
                    return self._result(0)  # accepted, but never stored
                return super()._set(schema_arg, key, value)

        shortcuts = GnomeShortcuts(run_command=SilentlyIgnoresBinding())
        launcher = GnomeShortcutLauncher(shortcuts, purpose=WINDOW_PURPOSE)

        with self.assertRaises(DesktopQueryError):
            launcher.register("/bin/murmly", parse_hotkey("Meta+X"))


class PathNamingTests(unittest.TestCase):
    def test_path_is_a_stable_slug_of_the_purpose_key(self) -> None:
        self.assertEqual(
            "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/murmly-window/",
            gnome_binding_path("window"),
        )


if __name__ == "__main__":
    unittest.main()
