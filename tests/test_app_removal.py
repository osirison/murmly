"""Removing Murmly in the app, without Qt: the steps, and the confirmation's words.

The list the confirmation shows is built from the installer's own record of what
it installed, here a real `Installer` over fakes, so these tests would fail if the
text were kept anywhere but there.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from murmly import removal as plain
from murmly.app import removal
from murmly.hotkey_record import HotkeyRecordStore
from murmly.installer import AppShortcut, InstallOutcome, SESSION_HOTKEY
from test_installer import FakeLauncher, FakeService, make_installer


def outcome(*messages: str) -> InstallOutcome:
    return InstallOutcome(
        entrypoint=None,
        hotkey=None,
        service_installed=False,
        hotkey_registered=False,
        already_bound=False,
        session_supported=True,
        session_verified=True,
        user_override=None,
        messages=messages,
    )


def a_plan(*, items=(), targets=()) -> plain.RemovalPlan:
    return plain.RemovalPlan(tuple(items), tuple(targets))


class Counting:
    """The plan and the removal, as the controller calls them: remembers every call."""

    def __init__(self, plan: plain.RemovalPlan | None = None) -> None:
        self.plan_calls = 0
        self.remove_calls: list[bool] = []
        self._plan = plan if plan is not None else a_plan()

    def plan(self) -> plain.RemovalPlan:
        self.plan_calls += 1
        return self._plan

    def remove(self, purge: bool) -> plain.RemovalResult:
        self.remove_calls.append(purge)
        return plain.RemovalResult(outcome("Removed the Murmly service."))


class StepTests(unittest.TestCase):
    def test_it_starts_idle_with_the_dialog_closed(self) -> None:
        view = removal.removal_view(removal.RemovalState())

        self.assertEqual("idle", view["step"])
        self.assertFalse(view["open"])

    def test_asking_reads_a_plan_and_removes_nothing(self) -> None:
        state = removal.RemovalState()
        calls = Counting()

        self.assertTrue(state.request())

        self.assertEqual("planning", state.step)
        self.assertEqual([], calls.remove_calls)
        self.assertFalse(removal.removal_view(state)["open"])

    def test_the_plan_opens_the_confirmation(self) -> None:
        state = removal.RemovalState()
        state.request()

        state.planned(a_plan())

        self.assertEqual("confirming", state.step)
        self.assertTrue(removal.removal_view(state)["open"])

    def test_confirming_without_a_plan_on_screen_does_nothing(self) -> None:
        state = removal.RemovalState()

        self.assertFalse(state.confirm())
        state.request()
        self.assertFalse(state.confirm())

        self.assertEqual("planning", state.step)

    def test_a_confirmation_counts_once(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())

        self.assertTrue(state.confirm())
        self.assertFalse(state.confirm())

        self.assertEqual("removing", state.step)

    def test_cancel_returns_to_idle_and_forgets_the_choice(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())
        state.set_purge(True)

        state.cancel()

        self.assertEqual("idle", state.step)
        self.assertFalse(state.purge)

    def test_asking_again_starts_with_purge_off(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())
        state.set_purge(True)
        state.cancel()

        state.request()
        state.planned(a_plan())

        self.assertFalse(state.purge)

    def test_a_request_while_one_is_open_is_ignored(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())

        self.assertFalse(state.request())
        self.assertEqual("confirming", state.step)

    def test_a_late_plan_after_cancel_is_ignored(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.cancel()

        state.planned(a_plan())

        self.assertEqual("idle", state.step)

    def test_an_unreadable_plan_is_a_failure_with_its_reason(self) -> None:
        state = removal.RemovalState()
        state.request()

        state.planned(None, "systemctl is unavailable")

        view = removal.removal_view(state)
        self.assertEqual("failed", view["step"])
        self.assertEqual("systemctl is unavailable", view["error"])

    def test_a_finished_removal_reports_what_it_did(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())
        state.confirm()

        state.removed(plain.RemovalResult(outcome("Removed the Murmly service.", "Released the Murmly hotkey.")))

        view = removal.removal_view(state)
        self.assertEqual("done", view["step"])
        self.assertEqual(["Removed the Murmly service.", "Released the Murmly hotkey."], view["report"])
        self.assertTrue(state.finished)

    def test_a_failed_removal_says_why_and_can_be_closed(self) -> None:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan())
        state.confirm()

        state.failed("no service manager")
        self.assertEqual("no service manager", removal.removal_view(state)["error"])
        state.cancel()

        self.assertEqual("idle", state.step)


class PurgeChoiceTests(unittest.TestCase):
    def confirming(self) -> removal.RemovalState:
        state = removal.RemovalState()
        state.request()
        state.planned(a_plan(targets=(plain.PurgeTarget("Settings", Path("/home/a/.config/murmly")),)))
        return state

    def test_it_is_off_by_default(self) -> None:
        state = self.confirming()

        self.assertFalse(state.purge)
        view = removal.removal_view(state)
        self.assertFalse(view["purge"])
        self.assertEqual(removal.CONFIRM_LABEL, view["confirmLabel"])
        self.assertEqual(removal.KEPT_NOTE, view["keptNote"])
        self.assertEqual("", view["purgeNote"])

    def test_only_an_explicit_choice_turns_it_on(self) -> None:
        state = self.confirming()

        state.set_purge(True)

        view = removal.removal_view(state)
        self.assertTrue(view["purge"])
        self.assertEqual(removal.CONFIRM_PURGE_LABEL, view["confirmLabel"])
        self.assertEqual(removal.PURGE_NOTE, view["purgeNote"])
        self.assertEqual("", view["keptNote"])

    def test_it_can_be_turned_off_again(self) -> None:
        state = self.confirming()
        state.set_purge(True)

        state.set_purge(False)

        self.assertFalse(state.purge)

    def test_it_cannot_be_set_when_no_confirmation_is_showing(self) -> None:
        state = removal.RemovalState()

        state.set_purge(True)

        self.assertFalse(state.purge)

    def test_it_says_the_transcription_model_is_not_deleted(self) -> None:
        view = removal.removal_view(self.confirming())

        self.assertIn("Hugging Face cache", view["purgeFootnote"])
        self.assertIn("not deleted", view["purgeFootnote"])

    def test_its_paths_are_named_even_while_it_is_off(self) -> None:
        view = removal.removal_view(self.confirming())

        self.assertEqual(
            [{"label": "Settings", "path": str(Path("/home/a/.config/murmly"))}], view["purgeTargets"]
        )


class ConfirmationNamesWhatIsInstalledTests(unittest.TestCase):
    def installer(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.shortcut = AppShortcut(applications_dir=root / "applications")
        self.shortcut.register(Path("/bin/murmly"))
        record = HotkeyRecordStore(root / "config" / "murmly" / "hotkeys.json")
        record.write({"window": "Meta+X"})
        return make_installer(
            service=FakeService(installed=True),
            launcher=FakeLauncher(declared="Meta+X"),
            session_launcher=FakeLauncher(declared="Meta+A", purpose=SESSION_HOTKEY),
            record_store=record,
            app_shortcut=self.shortcut,
        )

    def test_every_recorded_item_is_named_with_where_it_lives(self) -> None:
        installer = self.installer()
        plan = plain.plan_removal(installer)
        state = removal.RemovalState()
        state.request()
        state.planned(plan)

        view = removal.removal_view(state)

        recorded = {(item.label, item.detail) for item in installer.installed_items()}
        shown = {(line["label"], line["detail"]) for line in view["items"]}
        self.assertEqual(recorded, shown)
        labels = [line["label"] for line in view["items"]]
        self.assertIn("The Murmly background service", labels)
        self.assertIn("Your dictation key", labels)
        self.assertIn("Your key for replying to your assistant", labels)
        self.assertIn("The Murmly app in your applications menu", labels)
        self.assertIn(str(self.shortcut.path), [line["detail"] for line in view["items"]])
        self.assertEqual(removal.INTRO_ITEMS, view["intro"])

    def test_an_item_that_is_not_installed_is_not_named(self) -> None:
        installer = make_installer(service=FakeService(installed=False), launcher=FakeLauncher(declared="Meta+X"))
        state = removal.RemovalState()
        state.request()
        state.planned(plain.plan_removal(installer))

        labels = [line["label"] for line in removal.removal_view(state)["items"]]

        self.assertEqual(["Your dictation key"], labels)

    def test_nothing_installed_says_so_instead_of_listing(self) -> None:
        installer = make_installer(service=FakeService(installed=False), launcher=FakeLauncher(declared=None))
        state = removal.RemovalState()
        state.request()
        state.planned(plain.plan_removal(installer))

        view = removal.removal_view(state)

        self.assertEqual([], view["items"])
        self.assertEqual(removal.INTRO_NOTHING, view["intro"])


class DefaultsTests(unittest.TestCase):
    def test_the_model_keeps_what_it_is_given(self) -> None:
        calls = Counting()

        model = removal.RemovalModel(calls.plan, calls.remove)
        model.plan()
        model.remove(True)

        self.assertEqual(1, calls.plan_calls)
        self.assertEqual([True], calls.remove_calls)

    def test_the_default_removal_is_the_one_the_command_runs(self) -> None:
        import unittest.mock

        with (
            unittest.mock.patch.object(removal, "Installer") as installer,
            unittest.mock.patch.object(removal, "remove_murmly") as remove,
        ):
            removal.default_remove(True)

        remove.assert_called_once_with(installer.return_value, purge_user_files=True)


if __name__ == "__main__":
    unittest.main()
