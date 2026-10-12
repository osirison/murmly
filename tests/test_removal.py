"""Removing Murmly: the one function `murmly uninstall` and the app both run.

Everything happens in a temporary directory. The configuration and data folders
are patched in at the `removal` module, never reached through the environment, so
a test on a platform that ignores the environment's folders still cannot touch the
machine's own.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from murmly import removal
from murmly.hotkey_record import HotkeyRecordStore
from murmly.installer import APP_DESKTOP_ID, AppShortcut, InstallError, SESSION_HOTKEY
from test_installer import FakeLauncher, FakeService, make_installer


class RemovalCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config_dir = root / "config" / "murmly"
        self.data_dir = root / "data" / "murmly"
        self.applications = root / "applications"
        self.config_dir.mkdir(parents=True)
        self.data_dir.mkdir(parents=True)
        (self.config_dir / "config.toml").write_text("[stt]\nmodel = 'small'\n", encoding="utf-8")
        (self.data_dir / "kokoro.onnx").write_bytes(b"model")
        (self.data_dir / "voiceprints.json").write_text("{}", encoding="utf-8")
        (self.data_dir / "hooks").mkdir()
        (self.data_dir / "hooks" / "murmly-announce.py").write_text("# hook\n", encoding="utf-8")
        self.record = HotkeyRecordStore(self.config_dir / "hotkeys.json")
        self.record.write({"window": "Meta+X", "session": "Meta+A"})
        self.shortcut = AppShortcut(applications_dir=self.applications)
        self.shortcut.register(Path("/bin/murmly"))
        self.service = FakeService(installed=True)
        self.launcher = FakeLauncher(declared="Meta+X")
        self.session_launcher = FakeLauncher(declared="Meta+A", purpose=SESSION_HOTKEY)
        self.installer = make_installer(
            service=self.service,
            launcher=self.launcher,
            session_launcher=self.session_launcher,
            record_store=self.record,
            app_shortcut=self.shortcut,
        )
        for name, value in (
            ("default_config_path", self.config_dir / "config.toml"),
            ("default_data_dir", self.data_dir),
        ):
            patcher = unittest.mock.patch.object(
                removal, name, lambda env=None, value=value: value
            )
            patcher.start()
            self.addCleanup(patcher.stop)


class RemovalWithoutPurgeTests(RemovalCase):
    def test_the_app_shortcut_is_there_before_and_gone_after(self) -> None:
        self.assertTrue((self.applications / APP_DESKTOP_ID).is_file())

        removal.remove_murmly(self.installer)

        self.assertFalse((self.applications / APP_DESKTOP_ID).exists())

    def test_it_runs_the_installers_uninstall_and_reports_what_it_removed(self) -> None:
        result = removal.remove_murmly(self.installer)

        self.assertEqual(1, self.service.removes)
        self.assertEqual(1, self.launcher.unregistrations)
        self.assertEqual(1, self.session_launcher.unregistrations)
        joined = " ".join(result.messages)
        self.assertIn("Removed the Murmly service", joined)
        self.assertIn("Released the Murmly hotkey", joined)
        self.assertIn("applications menu", joined)

    def test_settings_and_models_are_kept(self) -> None:
        result = removal.remove_murmly(self.installer)

        self.assertTrue((self.config_dir / "config.toml").is_file())
        self.assertTrue((self.data_dir / "kokoro.onnx").is_file())
        self.assertTrue((self.data_dir / "voiceprints.json").is_file())
        self.assertIsNone(result.purged)

    def test_only_the_record_of_the_keys_goes_from_the_settings_folder(self) -> None:
        removal.remove_murmly(self.installer)

        self.assertFalse((self.config_dir / "hotkeys.json").exists())

    def test_purging_is_not_the_default(self) -> None:
        import inspect

        parameter = inspect.signature(removal.remove_murmly).parameters["purge_user_files"]

        self.assertIs(False, parameter.default)

    def test_removing_twice_succeeds_and_says_there_was_nothing_to_remove(self) -> None:
        removal.remove_murmly(self.installer)

        result = removal.remove_murmly(self.installer)

        self.assertIn("nothing to remove", " ".join(result.messages))


class RemovalWithPurgeTests(RemovalCase):
    def test_settings_and_downloaded_files_are_deleted(self) -> None:
        result = removal.remove_murmly(self.installer, purge_user_files=True)

        self.assertFalse(self.config_dir.exists())
        self.assertFalse((self.data_dir / "kokoro.onnx").exists())
        self.assertFalse((self.data_dir / "voiceprints.json").exists())
        self.assertFalse((self.applications / APP_DESKTOP_ID).exists())
        self.assertIsNotNone(result.purged)
        self.assertIn(f"Deleted {self.config_dir}.", result.messages)

    def test_the_announcement_hook_is_left_for_setup_to_unregister(self) -> None:
        result = removal.remove_murmly(self.installer, purge_user_files=True)

        self.assertTrue((self.data_dir / "hooks" / "murmly-announce.py").is_file())
        self.assertIn(f"Left {self.data_dir / 'hooks'} in place.", result.messages)

    def test_a_data_folder_with_nothing_kept_is_removed_itself(self) -> None:
        (self.data_dir / "hooks" / "murmly-announce.py").unlink()
        (self.data_dir / "hooks").rmdir()

        removal.remove_murmly(self.installer, purge_user_files=True)

        self.assertFalse(self.data_dir.exists())

    def test_an_uninstall_that_raises_deletes_nothing(self) -> None:
        installer = unittest.mock.Mock()
        installer.uninstall.side_effect = InstallError("no service manager")

        with self.assertRaises(InstallError):
            removal.remove_murmly(installer, purge_user_files=True)

        self.assertTrue((self.config_dir / "config.toml").is_file())
        self.assertTrue((self.data_dir / "kokoro.onnx").is_file())


class PurgeTargetsFollowTheGivenEnvironmentTests(unittest.TestCase):
    """Only resolves paths: nothing here deletes, and every answer must be inside the temporary home."""

    def setUp(self) -> None:
        if os.name != "posix":
            self.skipTest("builds POSIX-shaped environments")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        patcher = unittest.mock.patch("pathlib.Path.home", side_effect=AssertionError("the real home was read"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_inside_home(self, targets) -> None:
        for target in targets:
            self.assertTrue(target.path.is_relative_to(self.home), f"{target.path} is outside {self.home}")

    def test_a_given_home_is_the_only_home(self) -> None:
        with unittest.mock.patch("sys.platform", "linux"):
            targets = removal.purge_targets({"HOME": str(self.home)})

        self.assert_inside_home(targets)
        self.assertEqual(
            [self.home / ".config" / "murmly", self.home / ".local" / "share" / "murmly"],
            [target.path for target in targets],
        )

    def test_a_relative_xdg_value_is_not_followed(self) -> None:
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": ".", "XDG_DATA_HOME": "relative"}
        with unittest.mock.patch("sys.platform", "linux"), contextlib.chdir(self.home):
            targets = removal.purge_targets(env)

        self.assert_inside_home(targets)
        self.assertEqual(
            [self.home / ".config" / "murmly", self.home / ".local" / "share" / "murmly"],
            [target.path for target in targets],
        )

    def test_an_environment_without_a_home_raises(self) -> None:
        with unittest.mock.patch("sys.platform", "linux"):
            with self.assertRaises(RuntimeError):
                removal.purge_targets({})


class RemovalFailureTests(RemovalCase):
    def failing_installer(self):
        def refuse() -> bool:
            raise InstallError("systemctl could not stop the service")

        self.service.remove = refuse
        return self.installer

    def test_a_clean_removal_did_not_fail(self) -> None:
        result = removal.remove_murmly(self.installer, purge_user_files=True)

        self.assertFalse(result.failed)
        self.assertEqual((), result.outcome.problems)

    def test_a_service_that_would_not_stop_is_a_failure(self) -> None:
        result = removal.remove_murmly(self.failing_installer())

        self.assertTrue(result.failed)
        self.assertTrue(result.outcome.service_removal_failed)
        self.assertIn("systemctl could not stop the service", result.outcome.problems)

    def test_nothing_is_purged_while_the_service_may_still_be_running(self) -> None:
        result = removal.remove_murmly(self.failing_installer(), purge_user_files=True)

        self.assertIsNone(result.purged)
        self.assertTrue(result.failed)
        self.assertTrue((self.config_dir / "config.toml").is_file())
        self.assertTrue((self.data_dir / "kokoro.onnx").is_file())
        self.assertIn(removal.PURGE_SKIPPED, result.messages)
        self.assertIn("systemctl could not stop the service", result.messages)

    def test_no_note_about_kept_files_when_no_purge_was_asked(self) -> None:
        result = removal.remove_murmly(self.failing_installer())

        self.assertNotIn(removal.PURGE_SKIPPED, result.messages)

    def test_a_purge_that_could_not_delete_everything_is_a_failure(self) -> None:
        with unittest.mock.patch.object(removal, "purge", return_value=removal.PurgeResult(problems=("Could not delete x: in use",))):
            result = removal.remove_murmly(self.installer, purge_user_files=True)

        self.assertTrue(result.failed)

    def test_a_hotkey_or_menu_entry_that_would_not_go_is_a_failure(self) -> None:
        self.launcher.unregister = unittest.mock.Mock(side_effect=InstallError("gsettings said no"))

        result = removal.remove_murmly(self.installer)

        self.assertTrue(result.failed)
        self.assertFalse(result.outcome.service_removal_failed)


class PurgeTests(RemovalCase):
    def test_targets_are_the_settings_folder_and_the_data_folder(self) -> None:
        targets = removal.purge_targets()

        self.assertEqual([self.config_dir, self.data_dir], [target.path for target in targets])
        self.assertEqual(("hooks",), targets[1].keep)

    def test_one_shared_folder_is_listed_once(self) -> None:
        shared = self.data_dir
        with (
            unittest.mock.patch.object(removal, "default_config_path", lambda env=None: shared / "config.toml"),
            unittest.mock.patch.object(removal, "default_data_dir", lambda env=None: shared),
        ):
            targets = removal.purge_targets()

        self.assertEqual([shared], [target.path for target in targets])

    def test_a_folder_not_named_murmly_is_refused(self) -> None:
        other = self.data_dir.parent / "photos"
        other.mkdir()
        (other / "holiday.jpg").write_bytes(b"x")

        result = removal.purge((removal.PurgeTarget("Photos", other),))

        self.assertTrue((other / "holiday.jpg").is_file())
        self.assertIn("not a folder Murmly made", result.problems[0])

    def test_a_link_is_refused_and_its_target_untouched(self) -> None:
        real = self.data_dir.parent / "elsewhere"
        real.mkdir()
        (real / "keep.txt").write_text("x", encoding="utf-8")
        link_parent = self.data_dir.parent / "linked"
        link_parent.mkdir()
        link = link_parent / "murmly"
        try:
            os.symlink(real, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this platform cannot make a symbolic link here")

        result = removal.purge((removal.PurgeTarget("Linked", link),))

        self.assertTrue((real / "keep.txt").is_file())
        self.assertIn("link", result.problems[0])

    def test_a_junction_is_refused_and_its_target_untouched(self) -> None:
        real = self.data_dir.parent / "elsewhere"
        real.mkdir()
        (real / "keep.txt").write_text("x", encoding="utf-8")
        junction_parent = self.data_dir.parent / "joined"
        junction_parent.mkdir()
        junction = junction_parent / "murmly"
        junction.mkdir()
        (junction / "inside.txt").write_text("x", encoding="utf-8")

        with unittest.mock.patch.object(
            Path, "is_junction", lambda self: self == junction, create=True
        ):
            result = removal.purge((removal.PurgeTarget("Joined", junction),))

        self.assertTrue((junction / "inside.txt").is_file())
        self.assertTrue((real / "keep.txt").is_file())
        self.assertIn("link", result.problems[0])

    @unittest.skipUnless(os.name == "nt", "a real junction needs Windows")
    def test_a_real_windows_junction_is_refused(self) -> None:
        import _winapi

        real = self.data_dir.parent / "elsewhere"
        real.mkdir()
        (real / "keep.txt").write_text("x", encoding="utf-8")
        junction_parent = self.data_dir.parent / "joined"
        junction_parent.mkdir()
        junction = junction_parent / "murmly"
        _winapi.CreateJunction(str(real), str(junction))

        result = removal.purge((removal.PurgeTarget("Joined", junction),))

        self.assertTrue((real / "keep.txt").is_file())
        self.assertIn("link", result.problems[0])

    def test_a_relative_path_is_refused(self) -> None:
        with contextlib.chdir(self.data_dir.parent):
            (self.data_dir.parent / "relative" / "murmly").mkdir(parents=True)
            result = removal.purge((removal.PurgeTarget("Relative", Path("relative") / "murmly"),))

        self.assertTrue((self.data_dir.parent / "relative" / "murmly").is_dir())
        self.assertIn("not an absolute path", result.problems[0])

    def test_the_home_folder_is_refused_even_when_it_is_named_murmly(self) -> None:
        home = self.data_dir.parent / "home" / "murmly"
        home.mkdir(parents=True)
        (home / "photo.jpg").write_bytes(b"x")

        with unittest.mock.patch.object(Path, "home", return_value=home):
            result = removal.purge((removal.PurgeTarget("Home", home),))

        self.assertTrue((home / "photo.jpg").is_file())
        self.assertIn("home folder", result.problems[0])

    def test_a_filesystem_or_drive_root_is_refused(self) -> None:
        # Asked of the rule alone: purging a root, even to prove it is refused, is not a test.
        root = Path(Path.cwd().anchor)

        self.assertIn("root", removal.refusal(root))

    def test_a_folder_that_passes_every_rule_is_not_refused(self) -> None:
        self.assertIsNone(removal.refusal(self.data_dir))

    def test_a_refused_folder_is_a_problem_and_nothing_is_deleted(self) -> None:
        other = self.data_dir.parent / "photos"
        other.mkdir()
        deleted: list[Path] = []

        result = removal.purge((removal.PurgeTarget("Bad", other),), rmtree=deleted.append)

        self.assertEqual([], deleted)
        self.assertEqual((), result.removed)
        self.assertEqual(1, len(result.problems))

    def test_a_folder_that_is_already_gone_is_not_a_problem(self) -> None:
        missing = self.data_dir.parent / "gone" / "murmly"

        result = removal.purge((removal.PurgeTarget("Gone", missing),))

        self.assertEqual((), result.problems)
        self.assertEqual((), result.removed)

    def test_a_file_that_cannot_be_deleted_is_reported_and_the_rest_goes(self) -> None:
        sub = self.data_dir / "voices"
        sub.mkdir()
        (sub / "af.bin").write_bytes(b"v")

        def refusing(path: Path) -> None:
            raise OSError("in use")

        result = removal.purge(
            (removal.PurgeTarget("Data", self.data_dir, ("hooks",)),), rmtree=refusing
        )

        self.assertFalse((self.data_dir / "kokoro.onnx").exists())
        self.assertTrue(sub.is_dir())
        self.assertTrue(any("in use" in problem for problem in result.problems))

    def test_listing_the_plan_deletes_nothing(self) -> None:
        plan = removal.plan_removal(self.installer)

        self.assertEqual(
            ["service", "hotkey", "hotkey", "app-shortcut", "hotkey-record"],
            [item.kind for item in plan.items],
        )
        self.assertEqual(2, len(plan.purge_targets))
        self.assertEqual(0, self.service.removes)
        self.assertTrue((self.applications / APP_DESKTOP_ID).is_file())
        self.assertTrue((self.config_dir / "config.toml").is_file())


if __name__ == "__main__":
    unittest.main()
