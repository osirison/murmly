"""Task 5.4/5.5: the persisted hotkey record and the rebind path built on it.

No in-process hotkey backend exists yet (Windows is section 8, macOS section
13), so every test here either exercises the record's own read/write/remove
behaviour directly, or proves `rebind_from_record`'s branches against a
constructed `PlatformProfile` and a fake registrar -- never a stub wired into
production code.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from murmly.hotkey_record import HotkeyRecordStore, rebind_from_record
from murmly.platform import BackendCandidate, BackendRegistry, OperatingSystem, PlatformProfile


def linux_plasma() -> PlatformProfile:
    from murmly.platform import Desktop

    return PlatformProfile(
        operating_system=OperatingSystem.LINUX,
        architecture="x86_64",
        session_type="x11",
        x11_display=True,
        desktop=Desktop.PLASMA,
    )


def a_future_in_process_platform() -> PlatformProfile:
    """Stands in for Windows or macOS once their registries carry a real
    in-process candidate -- today neither `HOTKEY_REGISTRATION` entry is one,
    so this profile is paired with a fake registry, not the real one."""
    return PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")


def in_process_fixture() -> tuple[BackendRegistry, frozenset[str]]:
    registry = BackendRegistry(
        "hotkey registration",
        candidates=(BackendCandidate("windows-hotkey", lambda profile: True, lambda: object()),),
        unavailable_reason=lambda profile: "unreachable",
    )
    return registry, frozenset({"windows-hotkey"})


class FakeRegistrar:
    def __init__(self) -> None:
        self.rebound: list[dict[str, str]] = []

    def rebind(self, bindings: dict[str, str]) -> None:
        self.rebound.append(dict(bindings))


class HotkeyRecordStoreTests(unittest.TestCase):
    def test_round_trips_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")

            store.write({"window": "Meta+X", "session": "Meta+A"})

            self.assertEqual({"window": "Meta+X", "session": "Meta+A"}, store.read())

    def test_missing_file_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "does-not-exist.json")

            self.assertEqual({}, store.read())

    def test_unreadable_content_reads_as_empty_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hotkeys.json"
            path.write_text("not json", encoding="utf-8")

            self.assertEqual({}, HotkeyRecordStore(path).read())

    def test_a_json_value_that_is_not_an_object_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hotkeys.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")

            self.assertEqual({}, HotkeyRecordStore(path).read())

    def test_write_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "nested" / "dir" / "hotkeys.json")

            store.write({"window": "Meta+X"})

            self.assertEqual({"window": "Meta+X"}, store.read())

    def test_remove_deletes_the_file_and_tolerates_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")
            store.write({"window": "Meta+X"})

            store.remove()
            store.remove()  # a second removal must not raise

            self.assertEqual({}, store.read())

    def test_a_rewrite_replaces_rather_than_merges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")
            store.write({"window": "Meta+X", "session": "Meta+A"})

            store.write({"window": "Meta+Y"})

            self.assertEqual({"window": "Meta+Y"}, store.read())


class RebindFromRecordTests(unittest.TestCase):
    def test_a_desktop_held_mechanism_reports_nothing_to_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")
            store.write({"window": "Meta+X"})

            report = rebind_from_record(linux_plasma(), store, registrar=None)

            self.assertIn("held by the desktop", report)

    def test_the_real_hotkey_registry_and_table_are_used_by_default(self) -> None:
        """No injected `registry`/`in_process`: proves the real, un-faked
        defaults also correctly classify Plasma as desktop-held."""
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")

            report = rebind_from_record(linux_plasma(), store, registrar=None)

            self.assertIn("held by the desktop", report)

    def test_an_in_process_mechanism_with_no_record_reports_nothing_bound(self) -> None:
        registry, in_process = in_process_fixture()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")

            report = rebind_from_record(
                a_future_in_process_platform(), store, registrar=FakeRegistrar(),
                registry=registry, in_process=in_process,
            )

            self.assertIn("nothing to rebind", report)

    def test_an_in_process_mechanism_with_no_running_registrar_says_so(self) -> None:
        registry, in_process = in_process_fixture()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")
            store.write({"window": "Meta+X"})

            report = rebind_from_record(
                a_future_in_process_platform(), store, registrar=None,
                registry=registry, in_process=in_process,
            )

            self.assertIn("No running", report)
            self.assertIn("windows-hotkey", report)

    def test_an_in_process_mechanism_with_a_running_registrar_rebinds(self) -> None:
        registry, in_process = in_process_fixture()
        registrar = FakeRegistrar()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = HotkeyRecordStore(Path(temp_dir) / "hotkeys.json")
            store.write({"window": "Meta+X", "session": "Meta+A"})

            report = rebind_from_record(
                a_future_in_process_platform(), store, registrar=registrar,
                registry=registry, in_process=in_process,
            )

            self.assertEqual([{"window": "Meta+X", "session": "Meta+A"}], registrar.rebound)
            self.assertIn("Rebound 2", report)


if __name__ == "__main__":
    unittest.main()
