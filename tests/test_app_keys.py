"""Choosing the dictation keys, with no Qt: the key a press makes, what the recorder
reads and ignores, what binding a key comes to, and where first run is.

The binding tests use the real `Installer` against the fakes `test_installer`
already has for the desktop, so a refusal here is `murmly install`'s own, and
nothing touches this machine's keys.
"""

from __future__ import annotations

import unittest

from murmly.app import keys
from murmly.hotkey import HotkeyError, parse_hotkey
from murmly.installer import (
    HotkeyConflictError,
    HotkeyNotConfirmedError,
    InstallError,
    InstallOutcome,
)
from murmly.platform import OperatingSystem, PlatformProfile

from test_installer import FakeLauncher, FakeService, OwnerRegistry, make_installer, owner

META = 0x10000000
CTRL = 0x04000000
ALT = 0x08000000
SHIFT = 0x02000000
KEYPAD = 0x20000000

KEY_X = 0x58
KEY_SPACE = 0x20
KEY_EXCLAM = 0x21
KEY_ESCAPE = 0x01000000
KEY_TAB = 0x01000001
KEY_BACKTAB = 0x01000002
KEY_F1 = 0x01000030
KEY_CONTROL = 0x01000021
KEY_META = 0x01000022
KEY_SHIFT = 0x01000020

LINUX = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64")
WINDOWS = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")


def outcome(window: bool = True, session: bool = False, messages: tuple[str, ...] = ()) -> InstallOutcome:
    return InstallOutcome(
        entrypoint=None,
        hotkey=None,
        service_installed=True,
        hotkey_registered=window,
        already_bound=False,
        session_supported=True,
        session_verified=True,
        user_override=None,
        messages=messages,
        session_hotkey_registered=session,
    )


class FakeInstall:
    """The install path as `bind` calls it, remembering what it was given."""

    def __init__(self, result: InstallOutcome | Exception | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._result = result if result is not None else outcome(True, True)

    def __call__(self, window, session) -> InstallOutcome:
        self.calls.append((window.portable, session.portable if session is not None else None))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class ReadEventTests(unittest.TestCase):
    def read(self, key: int, modifiers: int = 0, profile=LINUX) -> keys.Press:
        return keys.read_event(key, modifiers, profile)

    def test_a_combination_with_meta_is_taken_as_murmlys_key_string(self) -> None:
        press = self.read(KEY_X, META)

        self.assertEqual(("combo", True, "Meta+X"), (press.kind, press.accepted, press.text))

    def test_the_canvas_suggestions_round_trip_through_their_key_events(self) -> None:
        cases = {
            "Meta+Shift+Space": (KEY_SPACE, META | SHIFT),
            "Ctrl+Alt+D": (0x44, CTRL | ALT),
            "Meta+X": (KEY_X, META),
        }
        for text, (key, modifiers) in cases.items():
            with self.subTest(text):
                self.assertEqual(text, self.read(key, modifiers).text)

    def test_the_keypad_flag_is_not_part_of_the_combination(self) -> None:
        self.assertEqual("Meta+X", self.read(KEY_X, META | KEYPAD).text)

    def test_a_key_with_no_meta_ctrl_or_alt_is_refused_with_the_modifier_hint(self) -> None:
        for modifiers in (0, SHIFT):
            with self.subTest(modifiers=hex(modifiers)):
                press = self.read(KEY_X, modifiers)

                self.assertEqual("refused", press.kind)
                self.assertTrue(press.accepted)
                self.assertEqual(keys.HINT_NEEDS_MODIFIER, press.message)

    def test_a_modifier_pressed_alone_is_still_waiting_for_the_key(self) -> None:
        for key in (KEY_META, KEY_CONTROL, KEY_SHIFT):
            with self.subTest(key=hex(key)):
                press = self.read(key, META)

                self.assertEqual(("waiting", True, ""), (press.kind, press.accepted, press.text))

    def test_escape_on_its_own_cancels_and_escape_with_a_modifier_is_a_key(self) -> None:
        self.assertEqual("cancelled", self.read(KEY_ESCAPE).kind)
        self.assertEqual("Ctrl+Escape", self.read(KEY_ESCAPE, CTRL).text)

    def test_tab_is_left_to_move_the_focus(self) -> None:
        for key, modifiers in ((KEY_TAB, 0), (KEY_BACKTAB, SHIFT)):
            with self.subTest(key=hex(key)):
                press = self.read(key, modifiers)

                self.assertEqual(("ignored", False), (press.kind, press.accepted))

    def test_a_shifted_symbol_is_refused_rather_than_guessed(self) -> None:
        press = self.read(KEY_EXCLAM, META | SHIFT)

        self.assertEqual(("refused", keys.HINT_UNSUPPORTED_SHIFTED), (press.kind, press.message))
        self.assertIn("without Shift", press.message)

    def test_the_hint_for_a_key_with_no_shift_still_offers_the_digit(self) -> None:
        press = self.read(KEY_EXCLAM, META)

        self.assertEqual(keys.HINT_UNSUPPORTED, press.message)

    def test_a_key_this_platform_cannot_register_is_refused_in_the_installers_words(self) -> None:
        f25 = KEY_F1 + 24

        self.assertEqual("Ctrl+F25", self.read(f25, CTRL, LINUX).text)
        refused = self.read(f25, CTRL, WINDOWS)

        self.assertEqual("refused", refused.kind)
        self.assertIn("F25", refused.message)


class ValidateTests(unittest.TestCase):
    def test_it_accepts_what_murmly_install_accepts_with_a_modifier(self) -> None:
        self.assertEqual("Meta+Shift+Space", keys.validate("Meta+Shift+Space", LINUX).portable)

    def test_shift_alone_passes_install_but_not_the_app(self) -> None:
        parse_hotkey("Shift+X")  # the command takes it

        with self.assertRaises(keys.ModifierRequired):
            keys.validate("Shift+X", LINUX)

    def test_install_own_refusals_are_the_apps(self) -> None:
        for text in ("", "Meta+", "Meta+Frobnicate", "Meta+X,Meta+Y", "Hyper+X"):
            with self.subTest(text):
                with self.assertRaises(HotkeyError):
                    keys.validate(text, LINUX)


class RecorderTests(unittest.TestCase):
    def recorder(self) -> keys.Recorder:
        return keys.Recorder(LINUX)

    def test_keystrokes_are_ignored_while_not_recording(self) -> None:
        recorder = self.recorder()
        recorder.focus_changed("window", True)

        press = recorder.press("window", KEY_X, META)

        self.assertEqual(("ignored", False), (press.kind, press.accepted))

    def test_keystrokes_are_ignored_while_the_field_lacks_the_focus(self) -> None:
        recorder = self.recorder()
        recorder.start("window")

        press = recorder.press("window", KEY_X, META)

        self.assertEqual(("ignored", False), (press.kind, press.accepted))
        self.assertEqual("window", recorder.recording)

    def test_keystrokes_for_another_field_are_ignored(self) -> None:
        recorder = self.recorder()
        recorder.start("window")
        recorder.focus_changed("session", True)

        self.assertEqual("ignored", recorder.press("session", KEY_X, META).kind)
        self.assertEqual("ignored", recorder.press("window", KEY_X, META).kind)

    def test_a_combination_is_read_once_and_recording_then_stops(self) -> None:
        recorder = self.recorder()
        recorder.start("window")
        recorder.focus_changed("window", True)

        first = recorder.press("window", KEY_X, META)
        second = recorder.press("window", KEY_X, META)

        self.assertEqual(("combo", "Meta+X"), (first.kind, first.text))
        self.assertEqual("", recorder.recording)
        self.assertEqual("ignored", second.kind)

    def test_losing_the_focus_stops_recording(self) -> None:
        recorder = self.recorder()
        recorder.start("window")
        recorder.focus_changed("window", True)

        recorder.focus_changed("window", False)

        self.assertEqual("", recorder.recording)
        self.assertEqual("ignored", recorder.press("window", KEY_X, META).kind)

    def test_another_field_losing_the_focus_does_not_stop_this_one(self) -> None:
        recorder = self.recorder()
        recorder.focus_changed("window", True)
        recorder.start("session")
        recorder.focus_changed("window", False)
        recorder.focus_changed("session", True)

        self.assertEqual("session", recorder.recording)
        self.assertEqual("combo", recorder.press("session", KEY_X, META).kind)

    def test_a_refused_key_keeps_recording_and_escape_stops_it(self) -> None:
        recorder = self.recorder()
        recorder.start("window")
        recorder.focus_changed("window", True)

        self.assertEqual("refused", recorder.press("window", KEY_X, 0).kind)
        self.assertEqual("window", recorder.recording)
        self.assertEqual("waiting", recorder.press("window", KEY_META, META).kind)
        self.assertEqual("window", recorder.recording)
        self.assertEqual("cancelled", recorder.press("window", KEY_ESCAPE, 0).kind)
        self.assertEqual("", recorder.recording)


class BindTests(unittest.TestCase):
    def bind(self, purpose, text, record=None, install=None, profile=LINUX):
        install = install if install is not None else FakeInstall()
        return keys.bind(purpose, text, record or {}, install, profile), install

    def test_a_valid_combination_calls_the_install_path_with_murmlys_key_string(self) -> None:
        result, install = self.bind("window", "Meta+X")

        self.assertEqual([("Meta+X", None)], install.calls)
        self.assertEqual(("bound", True, "Meta + X is set."), (result.status, result.saved, result.message))

    def test_changing_dictate_keeps_the_reply_key_bound(self) -> None:
        _result, install = self.bind("window", "Ctrl+Alt+D", {"window": "Meta+X", "session": "Meta+A"})

        self.assertEqual([("Ctrl+Alt+D", "Meta+A")], install.calls)

    def test_setting_the_reply_key_passes_the_dictate_key_with_it(self) -> None:
        result, install = self.bind("session", "Meta+A", {"window": "Meta+X"})

        self.assertEqual([("Meta+X", "Meta+A")], install.calls)
        self.assertEqual("bound", result.status)

    def test_the_reply_key_cannot_be_set_before_the_dictate_key(self) -> None:
        result, install = self.bind("session", "Meta+A", {})

        self.assertEqual("refused", result.status)
        self.assertIn("Dictate", result.message)
        self.assertEqual([], install.calls)

    def test_a_key_already_used_for_the_other_purpose_is_refused(self) -> None:
        result, install = self.bind("session", "Meta+X", {"window": "Meta+X"})

        self.assertEqual("refused", result.status)
        self.assertIn("Dictate", result.message)
        self.assertEqual([], install.calls)

    def test_a_combination_without_meta_ctrl_or_alt_is_refused_and_never_bound(self) -> None:
        for text in ("Shift+X", "X"):
            with self.subTest(text):
                result, install = self.bind("window", text)

                self.assertEqual("refused", result.status)
                self.assertFalse(result.saved)
                self.assertEqual([], install.calls)
        self.assertEqual(keys.HINT_NEEDS_MODIFIER, self.bind("window", "Shift+X")[0].message)

    def test_a_held_key_is_refused_naming_the_holder_the_desktop_reports(self) -> None:
        service, launcher = FakeService(), FakeLauncher()
        shortcuts = OwnerRegistry(owners={268435544: [owner("plasmashell", "Klipper")]})
        installer = make_installer(service=service, launcher=launcher, shortcuts=shortcuts)

        result, _ = self.bind("window", "Meta+X", install=lambda w, s: installer.install(w, s))

        self.assertEqual(("held", "Klipper", False), (result.status, result.holder, result.saved))
        self.assertEqual("Meta + X is held by Klipper. Choose different keys.", result.message)
        self.assertIn("Klipper", result.detail)
        # The refusal wrote nothing.
        self.assertEqual([], service.installs)
        self.assertEqual([], launcher.registrations)

    def test_a_held_key_is_refused_generically_when_the_platform_names_no_holder(self) -> None:
        refusal = HotkeyConflictError("Meta+X could not be bound: the platform refused the registration.")

        result, _ = self.bind("window", "Meta+X", install=FakeInstall(refusal))

        self.assertEqual(("held", ""), (result.status, result.holder))
        self.assertEqual("Another application holds Meta + X. Choose different keys.", result.message)

    def test_a_key_the_desktop_did_not_confirm_is_saved_for_the_next_sign_in(self) -> None:
        result, _ = self.bind("window", "Meta+X", install=FakeInstall(HotkeyNotConfirmedError("not in time")))

        self.assertEqual(("unconfirmed", True, "amber"), (result.status, result.saved, result.tone))
        self.assertIn("next time you sign in", result.message)

    def test_an_install_that_fails_says_why(self) -> None:
        result, _ = self.bind("window", "Meta+X", install=FakeInstall(InstallError("no entrypoint")))

        self.assertEqual(("failed", "no entrypoint", False), (result.status, result.message, result.saved))

    def test_a_desktop_that_cannot_bind_keys_reports_what_install_said(self) -> None:
        said = outcome(False, False, ("No hotkeys here.", "Bind it yourself."))

        result, _ = self.bind("window", "Meta+X", install=FakeInstall(said))

        self.assertEqual(("failed", "No hotkeys here. Bind it yourself."), (result.status, result.message))


class ViewTests(unittest.TestCase):
    def test_the_view_shows_the_keys_bound_and_what_is_being_set(self) -> None:
        state = keys.KeysState(record={"window": "Meta+X"}, loaded=True, pending={"session": "Meta+A"})

        view = keys.keys_view(state, "")

        self.assertEqual(["Meta", "X"], view["rows"]["window"]["parts"])
        self.assertTrue(view["rows"]["window"]["set"])
        self.assertEqual(["Meta", "A"], view["rows"]["session"]["parts"])
        self.assertFalse(view["rows"]["session"]["set"])
        self.assertTrue(view["rows"]["session"]["busy"])
        self.assertTrue(view["canContinue"])

    def test_the_hint_says_what_to_press_while_recording_and_the_last_refusal_after_a_wrong_key(self) -> None:
        state = keys.KeysState(loaded=True)

        self.assertEqual(keys.HINT_IDLE, keys.keys_view(state, "")["rows"]["window"]["hint"])
        self.assertEqual(keys.HINT_RECORDING, keys.keys_view(state, "window")["rows"]["window"]["hint"])
        state.hints["window"] = keys.HINT_NEEDS_MODIFIER
        self.assertEqual(keys.HINT_NEEDS_MODIFIER, keys.keys_view(state, "window")["rows"]["window"]["hint"])
        self.assertFalse(keys.keys_view(state, "")["canContinue"])


class FirstRunTests(unittest.TestCase):
    def test_it_shows_when_no_dictation_key_is_bound(self) -> None:
        self.assertTrue(keys.should_show_first_run({}))
        self.assertTrue(keys.should_show_first_run({"session": "Meta+A"}))
        self.assertFalse(keys.should_show_first_run({"window": "Meta+X"}))

    def test_it_walks_the_steps_and_closes_after_the_last(self) -> None:
        first_run = keys.FirstRun(show=True)

        self.assertEqual(("keys", False), (first_run.step, first_run.last))
        first_run.next()
        self.assertEqual(("accuracy", True), (first_run.step, first_run.last))
        first_run.back()
        self.assertEqual("keys", first_run.step)
        first_run.next()
        first_run.next()
        self.assertFalse(first_run.visible)

    def test_set_up_later_closes_it_and_open_brings_it_back_to_the_first_step(self) -> None:
        first_run = keys.FirstRun(show=True)
        first_run.next()

        first_run.later()
        self.assertFalse(first_run.visible)
        first_run.open()

        self.assertEqual((True, "keys"), (first_run.visible, first_run.step))

    def test_the_view_numbers_the_steps_and_marks_where_the_person_is(self) -> None:
        first_run = keys.FirstRun(show=True)
        first_run.next()

        steps = first_run.view()["steps"]

        self.assertEqual(
            [("keys", 1, "done"), ("accuracy", 2, "current")],
            [(step["id"], step["number"], step["state"]) for step in steps],
        )

    def test_it_is_not_showing_unless_asked(self) -> None:
        self.assertFalse(keys.FirstRun().visible)


if __name__ == "__main__":
    unittest.main()
