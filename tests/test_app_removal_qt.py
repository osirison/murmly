"""Removing Murmly through the real Qt layer: Settings' "Remove murmly", the
confirmation, the controller and the worker thread it runs on.

The plan and the removal are fakes that count their calls and the thread they ran
on, so no test reads or removes what is installed on the machine it runs on."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from pathlib import Path

from home_fakes import FakeChannel
from murmly import removal as plain
from murmly.app import home, removal
from murmly.installer import InstalledItem
from murmly.settings_apply import RestartWhenIdle

from qt_app import MessageLog, qt_application
from test_app_removal import outcome
from test_app_settings_qt import ORIGINAL, FakeService, RecordingModel, RecordingWriter

ITEMS = (
    InstalledItem("service", "The Murmly background service", "/home/a/.config/systemd/user/murmly.service"),
    InstalledItem("hotkey", "Your dictation key", "Meta+X, in /home/a/.local/share/applications/net.local.murmly.desktop"),
    InstalledItem("app-shortcut", "The Murmly app in your applications menu", "/home/a/.local/share/applications/net.local.murmly-app.desktop"),
)
TARGETS = (
    plain.PurgeTarget("Settings", Path("/home/a/.config/murmly")),
    plain.PurgeTarget("Downloaded models and enrolled voices", Path("/home/a/.local/share/murmly"), ("hooks",)),
)


class RecordingRemoval:
    """The model's two calls: what each was asked and which thread ran it."""

    def __init__(self) -> None:
        self.plan_threads: list[int] = []
        self.remove_calls: list[bool] = []
        self.remove_threads: list[int] = []
        self.failure: Exception | None = None
        self.partial = False
        self.items = ITEMS

    def plan(self) -> plain.RemovalPlan:
        self.plan_threads.append(threading.get_ident())
        return plain.RemovalPlan(self.items, TARGETS)

    def remove(self, purge: bool) -> plain.RemovalResult:
        self.remove_calls.append(purge)
        self.remove_threads.append(threading.get_ident())
        if self.failure is not None:
            raise self.failure
        messages = ["Removed the Murmly service.", "Released the Murmly hotkey."]
        if self.partial:
            problem = "Could not clear the hotkey record: in use"
            partial = dataclasses.replace(outcome(*messages, problem), problems=(problem,))
            return plain.RemovalResult(partial)
        purged = None
        if purge:
            purged = plain.PurgeResult(removed=(Path("/home/a/.config/murmly"),))
        return plain.RemovalResult(outcome(*messages), purged)


class RemovalQtCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = qt_application()

    def build(self, settings_text: str = ORIGINAL) -> None:
        from murmly.app import controller, qt

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "murmly" / "config.toml"
        self.path.parent.mkdir()
        self.path.write_text(settings_text, encoding="utf-8")
        self.log = MessageLog()
        self.log.__enter__()
        self.addCleanup(self.log.__exit__)
        self.addCleanup(lambda: self.assertEqual([], self.log.messages))
        self.assertEqual([], qt.prepare_application())
        self.writer = RecordingWriter(self.path)
        self.settings = controller.SettingsController(
            RecordingModel(self.writer),
            lambda: RestartWhenIdle(status=FakeService().status, restart=FakeService().restart),
        )
        self.fake = RecordingRemoval()
        self.quits = 0
        self.removal = controller.RemovalController(
            removal.RemovalModel(self.fake.plan, self.fake.remove),
            self.settings.executor,
            self._quit,
            before_removal=self.settings.freeze,
            removal_failed=self.settings.thaw,
        )
        self.home = controller.HomeController(home.HomeModel(FakeChannel()))
        self.health = controller.HealthController(controller.default_health_model())
        self.engine, self.theme = qt.create_engine(
            self.home, self.health, self.settings, removal=self.removal
        )
        self.addCleanup(self._close)
        self.window = self.engine.rootObjects()[0]
        self.settle()

    def _quit(self) -> None:
        self.quits += 1

    def _close(self) -> None:
        self.home.shutdown()
        self.health.shutdown()
        self.removal.shutdown()
        self.settings.shutdown()
        self.window.close()
        self.engine.deleteLater()
        self.settle()

    def settle(self) -> None:
        self.application.processEvents()

    def wait_for(self, predicate, timeout: float = 6.0, message: str = "condition") -> None:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            self.settle()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {message}")

    def items_named(self, name: str) -> list:
        found = []
        pending = [self.window.contentItem()]
        while pending:
            item = pending.pop()
            if item.objectName() == name:
                found.append(item)
            pending.extend(item.childItems())
        return found

    def child(self, name: str):
        found = self.items_named(name)
        self.assertTrue(found, f"no item named {name}")
        return found[0]

    def dialog(self):
        """The popup, which is not an item: found as an object under the window."""
        from PySide6.QtCore import QObject

        found = self.window.findChild(QObject, "remove-dialog")
        self.assertIsNotNone(found, "no dialog named remove-dialog")
        return found

    def activate(self) -> None:
        """Give the window the focus, as a person clicking into it would. Keys only
        reach a window that is active, which a real platform (Windows) may not grant
        a test window."""
        self.window.requestActivate()
        started = time.monotonic()
        while time.monotonic() - started < 2.0 and not self.window.isActive():
            self.settle()
            time.sleep(0.01)
        if not self.window.isActive():
            self.skipTest("this display does not give a test window the focus")

    def open_remove_section(self) -> None:
        self.child("nav-settings").click()
        self.child("settings-go-remove").click()
        self.settle()

    def ask(self) -> None:
        self.open_remove_section()
        self.child("remove-open").click()
        self.wait_for(lambda: self.removal.view["step"] == "confirming", message="the plan")
        self.wait_for(lambda: self.dialog().property("opened") is True, message="the dialog to open")

    def visible(self, name: str) -> bool:
        found = self.items_named(name)
        return bool(found) and all(item.isVisible() for item in found[:1])


class SectionTests(RemovalQtCase):
    def test_settings_has_a_this_computer_group_with_the_entry(self) -> None:
        self.build()
        self.child("nav-settings").click()

        self.assertEqual("Remove murmly", self.child("settings-go-remove").property("text"))

        self.child("settings-go-remove").click()
        self.settle()
        self.assertEqual("This computer", self.child("settings-title").property("text"))
        self.assertEqual("Remove murmly…", self.child("remove-open").property("text"))
        self.assertTrue(self.child("remove-open").isVisible())

    def test_the_other_groups_are_not_shown_beside_it(self) -> None:
        self.build()
        self.open_remove_section()

        self.assertFalse(self.visible("set-profile-balanced"))

    def test_it_works_when_the_settings_file_cannot_be_read(self) -> None:
        self.build(settings_text="this is = not [valid toml")
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("error", "") != "", message="the file to be refused")

        self.child("settings-go-remove").click()
        self.settle()
        self.child("remove-open").click()
        self.wait_for(lambda: self.removal.view["step"] == "confirming", message="the plan")

        self.assertEqual(1, len(self.fake.plan_threads))
        self.assertFalse(self.visible("settings-error"))


class ConfirmationTests(RemovalQtCase):
    def test_asking_reads_the_plan_off_the_gui_thread_and_removes_nothing(self) -> None:
        self.build()

        self.ask()

        self.assertEqual(1, len(self.fake.plan_threads))
        self.assertNotIn(threading.get_ident(), self.fake.plan_threads)
        self.assertEqual([], self.fake.remove_calls)

    def test_the_dialog_names_every_installed_item_and_where_it_is(self) -> None:
        self.build()

        self.ask()

        labels = [item.property("text") for item in self.items_named("remove-item-label")]
        details = [item.property("text") for item in self.items_named("remove-item-detail")]
        self.assertCountEqual([item.label for item in ITEMS], labels)
        self.assertCountEqual([item.detail for item in ITEMS], details)
        self.assertEqual("Remove murmly from this computer?", self.child("remove-title").property("text"))

    def test_the_purge_choice_is_off_and_names_both_folders(self) -> None:
        self.build()

        self.ask()

        self.assertFalse(self.child("remove-purge").property("on"))
        paths = [item.property("text") for item in self.items_named("remove-target-path")]
        self.assertCountEqual([str(target.path) for target in TARGETS], paths)
        self.assertEqual("Your settings and downloaded models are kept.", self.child("remove-kept-note").property("text"))
        self.assertEqual(removal.PURGE_FOOTNOTE, self.child("remove-purge-footnote").property("text"))
        self.assertEqual("Remove murmly", self.child("remove-confirm").property("text"))

    def test_the_first_button_to_take_the_focus_is_the_safe_one(self) -> None:
        self.build()

        self.ask()

        self.assertTrue(self.child("remove-cancel").property("focus"))
        self.assertFalse(self.child("remove-confirm").property("focus"))

    def test_keeping_murmly_closes_it_and_removes_nothing(self) -> None:
        self.build()
        self.ask()

        self.child("remove-cancel").click()
        self.wait_for(lambda: self.dialog().property("opened") is False, message="the dialog to close")

        self.assertEqual("idle", self.removal.view["step"])
        self.assertEqual([], self.fake.remove_calls)

    def test_escape_closes_it_and_removes_nothing(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.build()
        self.ask()
        self.activate()

        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.wait_for(lambda: self.removal.view["step"] == "idle", message="Escape to cancel")

        self.assertEqual([], self.fake.remove_calls)

    def test_nothing_installed_says_so(self) -> None:
        self.build()
        self.fake.items = ()

        self.ask()

        self.assertEqual(removal.INTRO_NOTHING, self.child("remove-intro").property("text"))
        self.assertEqual([], self.items_named("remove-item-label"))


class RemovingTests(RemovalQtCase):
    def confirm(self) -> None:
        self.child("remove-confirm").click()
        self.wait_for(
            lambda: self.removal.view["step"] in ("done", "partial", "failed"),
            message="the removal to finish",
        )

    def test_confirming_runs_the_removal_once_off_the_gui_thread_without_purge(self) -> None:
        self.build()
        self.ask()

        self.confirm()

        self.assertEqual([False], self.fake.remove_calls)
        self.assertNotIn(threading.get_ident(), self.fake.remove_threads)

    def test_it_then_reports_what_was_removed(self) -> None:
        self.build()
        self.ask()

        self.confirm()

        self.assertEqual("Murmly was removed", self.child("remove-title").property("text"))
        lines = [item.property("text") for item in self.items_named("remove-report-line")]
        self.assertCountEqual(["Removed the Murmly service.", "Released the Murmly hotkey."], lines)
        self.assertEqual(0, self.quits)

    def test_a_partial_removal_says_so_lists_what_happened_and_keeps_the_app_open(self) -> None:
        self.build()
        self.fake.partial = True
        self.ask()

        self.confirm()

        self.assertEqual(removal.TITLE_PARTIAL, self.child("remove-title").property("text"))
        lines = [item.property("text") for item in self.items_named("remove-report-line")]
        self.assertIn("Could not clear the hotkey record: in use", lines)
        self.assertEqual(removal.PARTIAL_NOTE, self.child("remove-done-note").property("text"))
        self.assertEqual("Close", self.child("remove-close").property("text"))
        self.child("remove-close").click()
        self.wait_for(lambda: self.removal.view["step"] == "idle", message="the dialog to close")
        self.assertEqual(0, self.quits)

    def test_the_close_button_quits_the_app(self) -> None:
        self.build()
        self.ask()
        self.confirm()

        self.child("remove-close").click()

        self.assertEqual(1, self.quits)

    def test_quitting_is_refused_before_a_removal_has_finished(self) -> None:
        self.build()
        self.ask()

        self.removal.finish()

        self.assertEqual(0, self.quits)

    def test_turning_the_purge_choice_on_changes_the_button_and_is_passed_on(self) -> None:
        self.build()
        self.ask()

        self.child("remove-purge").click()
        self.settle()

        self.assertTrue(self.child("remove-purge").property("on"))
        self.assertEqual("Remove murmly and delete my data", self.child("remove-confirm").property("text"))
        self.assertTrue(self.child("remove-purge-note").isVisible())
        self.confirm()
        self.assertEqual([True], self.fake.remove_calls)
        lines = [item.property("text") for item in self.items_named("remove-report-line")]
        self.assertIn(f"Deleted {Path('/home/a/.config/murmly')}.", lines)

    def test_settings_changes_stop_once_removal_is_confirmed(self) -> None:
        self.build()
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.ask()
        self.confirm()

        self.settings.change("speak", True)
        time.sleep(0.1)
        self.settle()

        self.assertEqual(0, self.writer.writes)

    def test_a_failed_removal_says_why_and_lets_settings_work_again(self) -> None:
        from murmly.installer import InstallError

        self.build()
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.fake.failure = InstallError("systemctl is unavailable")
        self.ask()

        self.confirm()

        self.assertEqual("Murmly could not be removed", self.child("remove-title").property("text"))
        self.assertEqual("systemctl is unavailable", self.child("remove-error").property("text"))
        self.child("remove-failed-close").click()
        self.wait_for(lambda: self.removal.view["step"] == "idle", message="the dialog to close")
        self.assertEqual(0, self.quits)
        self.settings.change("speak", True)
        self.wait_for(lambda: self.writer.writes >= 1, message="Settings to write again")

    def test_closing_the_app_after_the_files_are_gone_does_not_fail(self) -> None:
        self.build()
        self.child("nav-settings").click()
        self.wait_for(lambda: self.settings.view.get("loaded") is True, message="the file to be read")
        self.ask()
        self.confirm()
        self.path.unlink()
        self.path.parent.rmdir()

        self.child("remove-close").click()
        self.settings.shutdown()
        self.home.shutdown()
        self.health.shutdown()
        self.removal.shutdown()

        self.assertEqual(1, self.quits)
        self.assertEqual([], self.log.messages)


if __name__ == "__main__":
    unittest.main()
