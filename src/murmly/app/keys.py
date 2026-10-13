"""Choosing the dictation keys in the app: plain Python, no Qt.

Used by first run and by Settings' "Your keys". The Qt side only forwards what
the window reports (a key code and its modifiers) and draws what `keys_view`
returns; every rule is here, so the suite tests it on every platform.

Nothing in this module listens to the keyboard. `Recorder.press` is handed a key
event by the one focused field, and ignores it unless the person has asked to
record and that same field has the focus. A combination that passes is bound by
`Installer.install`, the code `murmly install` runs, so the app and the command
refuse the same keys and name the same holder:

- `validate` is the command's own parser (`parse_hotkey`), then the encoder of an
  in-process platform, then one rule the command does not have: a combination
  needs Meta, Ctrl or Alt, because Shift alone would fire on ordinary typing.
- `bind` passes both keys to `install`, as the command's own advice says to when
  one key moves, so changing one never unbinds the other.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from murmly.app.home import KEY_PURPOSES
from murmly.hotkey import (
    ALT_MODIFIER,
    CONTROL_MODIFIER,
    META_MODIFIER,
    SHIFT_MODIFIER,
    Hotkey,
    HotkeyError,
    decode_kde_keycode,
    encode_for_kde,
    parse_hotkey,
)
from murmly.installer import (
    HotkeyConflictError,
    HotkeyNotConfirmedError,
    InstallError,
    InstallOutcome,
    Installer,
    in_process_hotkey_encoder,
)

WINDOW = "window"
SESSION = "session"

#: The combinations first run offers, one press each. Meta+X is the one the manual uses.
SUGGESTIONS: tuple[str, ...] = ("Meta+X", "Meta+Shift+Space", "Ctrl+Alt+D")

HINT_IDLE = "Click here, then press the keys you want"
HINT_CHANGE = "Click here to record different keys"
HINT_RECORDING = "Press your keys now. Escape cancels."
HINT_NEEDS_MODIFIER = "Add Meta, Ctrl or Alt, so ordinary typing never sets it off."
HINT_UNSUPPORTED = "Murmly cannot use that key. Try a letter, a digit, a function key or Space."
# Shift+1 reaches the app as "!", and Qt's key event does not say which key it came
# from, so the digit cannot be recovered; the person is told to leave Shift out. Only
# for the digit row, where the key without Shift is a digit this accepts.
HINT_UNSUPPORTED_SHIFTED = "Murmly cannot use that key with Shift. Try it without Shift, or a letter or a function key."

# ---------------------------------------------------------------------------
# From a Qt key event to Murmly's key string
# ---------------------------------------------------------------------------

# Qt's own numbers. Its modifier flags and key values are the encoding
# `hotkey.py` already speaks (KDE's), so the event decodes with `decode_kde_keycode`.
_MODIFIER_MASK = SHIFT_MODIFIER | CONTROL_MODIFIER | ALT_MODIFIER | META_MODIFIER

_KEY_ESCAPE = 0x01000000
_KEY_TAB = 0x01000001
_KEY_BACKTAB = 0x01000002

#: Keys that are a modifier or a lock being pressed, never the key of a combination.
_MODIFIER_KEYS = frozenset(
    {
        0x01000020,  # Shift
        0x01000021,  # Control
        0x01000022,  # Meta
        0x01000023,  # Alt
        0x01000024,  # CapsLock
        0x01000025,  # NumLock
        0x01000026,  # ScrollLock
        0x01000053,  # Super_L
        0x01000054,  # Super_R
        0x01000056,  # Hyper_L
        0x01000057,  # Hyper_R
        0x0100117E,  # Mode_switch
        0x01001103,  # AltGr
    }
)

#: What a US keyboard's digit row types with Shift, and the digit it types without.
_SHIFTED_DIGITS = {
    0x21: 0x31, 0x40: 0x32, 0x23: 0x33, 0x24: 0x34, 0x25: 0x35,
    0x5E: 0x36, 0x26: 0x37, 0x2A: 0x38, 0x28: 0x39, 0x29: 0x30,
}

_REQUIRED_MODIFIERS = frozenset({"Meta", "Ctrl", "Alt"})


class ModifierRequired(HotkeyError):
    """A combination without Meta, Ctrl or Alt: `murmly install` takes Shift alone, the app does not."""


def display_text(text: str) -> str:
    """`Meta+X` as the design writes it: `Meta + X`."""
    return " + ".join(part for part in text.split("+") if part)


def parts_of(text: str) -> list[str]:
    return [part for part in text.split("+") if part]


def validate(text: str, profile: object | None = None) -> Hotkey:
    """The key as `murmly install` would take it, plus the app's modifier rule.

    Raises `HotkeyError` (`ModifierRequired` for the one rule only the app has),
    worded by the parser that raised it.
    """
    from murmly.platform import hotkey_mechanism_is_in_process, resolve_platform

    hotkey = parse_hotkey(text)
    modifiers = {part for part in hotkey.portable.split("+")[:-1]}
    if not modifiers & _REQUIRED_MODIFIERS:
        raise ModifierRequired(HINT_NEEDS_MODIFIER)
    resolved = profile if profile is not None else resolve_platform()
    if hotkey_mechanism_is_in_process(resolved):
        in_process_hotkey_encoder(resolved)(hotkey.portable)
    return hotkey


@dataclass(frozen=True, slots=True)
class Press:
    """What one key press came to.

    `kind` is "ignored" (not recording, or not this field), "waiting" (a modifier
    on its own: the combination is still being made), "cancelled" (Escape),
    "refused" (`message` says why; recording goes on) or "combo" (`text` is the
    key string, recording stopped). `accepted` is whether the field consumed the
    event, so a key that is not for the recorder still reaches the window.
    """

    kind: str
    accepted: bool
    text: str = ""
    message: str = ""


IGNORED = Press("ignored", False)


def _digit_without_shift(key: int, modifiers: int) -> bool:
    """Whether `key`, typed with Shift, is a digit-row symbol whose digit this would take."""
    digit = _SHIFTED_DIGITS.get(key)
    if digit is None or not modifiers & SHIFT_MODIFIER:
        return False
    try:
        decode_kde_keycode(digit | modifiers & ~SHIFT_MODIFIER)
    except HotkeyError:
        return False
    return True


def read_event(key: int, modifiers: int, profile: object | None = None) -> Press:
    """Turn a Qt key event into a `Press`; the field's focus is the recorder's business."""
    modifiers &= _MODIFIER_MASK
    if key in _MODIFIER_KEYS:
        return Press("waiting", True)
    if key == _KEY_ESCAPE and not modifiers:
        return Press("cancelled", True)
    if key in (_KEY_TAB, _KEY_BACKTAB) and not modifiers & ~SHIFT_MODIFIER:
        # Tab, and Shift+Tab, still move the focus out of the field.
        return Press("ignored", False)
    if not modifiers & (META_MODIFIER | CONTROL_MODIFIER | ALT_MODIFIER):
        return Press("refused", True, message=HINT_NEEDS_MODIFIER)
    try:
        spec = decode_kde_keycode(key | modifiers)
    except HotkeyError:
        shifted = _digit_without_shift(key, modifiers)
        return Press("refused", True, message=HINT_UNSUPPORTED_SHIFTED if shifted else HINT_UNSUPPORTED)
    text = encode_for_kde(spec).portable
    try:
        validate(text, profile)
    except HotkeyError as error:
        return Press("refused", True, message=str(error))
    return Press("combo", True, text=text)


class Recorder:
    """Records a combination only while a field is recording and has the focus.

    Both are needed: a person asks (`start`) and the field they asked from holds
    the focus (`focus_changed`). Losing the focus stops recording, and no other
    state lets a key through, so typing in any other window, or elsewhere in this
    one, is never read.
    """

    def __init__(self, profile: object | None = None) -> None:
        self._profile = profile
        self._recording = ""
        self._focused = ""

    @property
    def recording(self) -> str:
        return self._recording

    def start(self, purpose: str) -> None:
        self._recording = purpose

    def stop(self) -> None:
        self._recording = ""

    def focus_changed(self, purpose: str, focused: bool) -> None:
        if focused:
            self._focused = purpose
            return
        if self._focused == purpose:
            self._focused = ""
        if self._recording == purpose:
            self._recording = ""

    def press(self, purpose: str, key: int, modifiers: int) -> Press:
        if not purpose or self._recording != purpose or self._focused != purpose:
            return IGNORED
        result = read_event(key, modifiers, self._profile)
        if result.kind in ("cancelled", "combo"):
            self._recording = ""
        return result


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------

Install = Callable[[Hotkey, "Hotkey | None"], InstallOutcome]

_HOLDER = re.compile(r"is already used by (?P<names>.+?)\. Choose a different hotkey")

_LABELS = {purpose: label for purpose, label, _helper in KEY_PURPOSES}


def default_install(window: Hotkey, session: Hotkey | None) -> InstallOutcome:
    """What `murmly install` runs, with the permission lines left for the window to ignore."""
    return Installer(announce=lambda _line: None).install(window, session)


@dataclass(frozen=True, slots=True)
class BindResult:
    """What binding a key came to.

    `status` is "bound", "unconfirmed" (written, active from the next sign-in),
    "held" (another application has it; `holder` names it where the platform
    said), "refused" (the rules, or the other key) or "failed". `message` is for
    the person; `detail` is the installer's own words, shown beside it.
    """

    status: str
    purpose: str
    text: str
    message: str = ""
    detail: str = ""
    holder: str = ""

    @property
    def saved(self) -> bool:
        return self.status in ("bound", "unconfirmed")

    @property
    def tone(self) -> str:
        return {"bound": "", "unconfirmed": "amber"}.get(self.status, "crimson")


def bind(
    purpose: str,
    text: str,
    record: Mapping[str, str],
    install: Install = default_install,
    profile: object | None = None,
) -> BindResult:
    """Validate `text` and bind it as `purpose`'s key through the install path.

    Blocking (a service install, a daemon start): run it off the GUI thread.
    `record` is the hotkey record, the keys bound now.
    """
    shown = display_text(text)
    try:
        hotkey = validate(text, profile)
    except HotkeyError as error:
        return BindResult("refused", purpose, text, str(error))

    other = SESSION if purpose == WINDOW else WINDOW
    other_text = record.get(other, "")
    other_key: Hotkey | None = None
    if other_text:
        try:
            other_key = parse_hotkey(other_text)
        except HotkeyError:
            other_key = None
    if other_key is not None and other_key.keycode == hotkey.keycode:
        return BindResult(
            "refused",
            purpose,
            text,
            f"{shown} is already your {_LABELS[other]} key. Choose different keys.",
        )
    if purpose == SESSION and other_key is None:
        return BindResult(
            "refused",
            purpose,
            text,
            f"Choose your {_LABELS[WINDOW]} key first. This one is set alongside it.",
        )

    window_key = hotkey if purpose == WINDOW else other_key
    session_key = hotkey if purpose == SESSION else other_key
    assert window_key is not None  # a session key is refused above without a window key
    try:
        outcome = install(window_key, session_key)
    except HotkeyConflictError as error:
        found = _HOLDER.search(str(error))
        holder = found.group("names") if found else ""
        message = (
            f"{shown} is held by {holder}. Choose different keys."
            if holder
            else f"Another application holds {shown}. Choose different keys."
        )
        return BindResult("held", purpose, text, message, detail=str(error), holder=holder)
    except HotkeyNotConfirmedError as error:
        return BindResult(
            "unconfirmed",
            purpose,
            text,
            f"{shown} is saved. It will start working the next time you sign in: "
            "the desktop did not confirm it in this session.",
            detail=str(error),
        )
    except InstallError as error:
        return BindResult("failed", purpose, text, str(error))
    except Exception as error:  # noqa: BLE001 - shown as the reason the keys were not set
        return BindResult("failed", purpose, text, str(error) or type(error).__name__)

    registered = outcome.hotkey_registered if purpose == WINDOW else outcome.session_hotkey_registered
    if not registered:
        # The desktop cannot bind keys for Murmly (an unsupported session): the
        # install says what to do instead, in its own words.
        said = " ".join(outcome.messages)
        return BindResult("failed", purpose, text, said or f"{shown} was not bound.", detail=said)
    return BindResult("bound", purpose, text, f"{shown} is set.")


class KeysModel:
    """What the key fields read and do, behind two injectable calls.

    `read_record` returns the hotkey record; `install` is the install path. A
    test passes fakes, so no test reads the person's keys or binds a real one.
    Blocking: the controller calls both from its worker thread.
    """

    def __init__(
        self,
        read_record: Callable[[], Mapping[str, str]],
        install: Install = default_install,
        profile: object | None = None,
    ) -> None:
        self._read_record = read_record
        self._install = install
        self.profile = profile

    def read(self) -> dict[str, str]:
        return dict(self._read_record())

    def bind(self, purpose: str, text: str, record: Mapping[str, str]) -> BindResult:
        return bind(purpose, text, record, self._install, self.profile)


# ---------------------------------------------------------------------------
# What the page draws
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class KeysState:
    """What the key fields show: the hotkey record plus what the person is doing."""

    #: The hotkey record (purpose to `Meta+X`), as last read.
    record: dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    #: A combination being bound, by purpose, shown in the field until it is settled.
    pending: dict[str, str] = field(default_factory=dict)
    #: The refusal of the last attempt while recording, by purpose.
    hints: dict[str, str] = field(default_factory=dict)
    #: The outcome of the last bind: purpose, tone, text, detail.
    note: dict[str, str] = field(default_factory=dict)


def keys_view(state: KeysState, recording: str) -> dict[str, object]:
    """Everything the key fields draw, as plain data."""
    rows: dict[str, dict[str, object]] = {}
    for purpose, label, helper in KEY_PURPOSES:
        shown = state.pending.get(purpose) or state.record.get(purpose, "")
        parts = parts_of(shown)
        is_recording = recording == purpose
        busy = purpose in state.pending
        if busy:
            hint = "Setting it…"
        elif is_recording:
            hint = state.hints.get(purpose) or HINT_RECORDING
        else:
            hint = HINT_CHANGE if state.record.get(purpose) else HINT_IDLE
        rows[purpose] = {
            "label": label,
            "helper": helper,
            "parts": parts,
            "display": display_text(shown) if shown else "",
            "set": bool(state.record.get(purpose)),
            "recording": is_recording,
            "busy": busy,
            "hint": hint,
        }
    return {
        "loaded": state.loaded,
        "recording": recording,
        "busy": bool(state.pending),
        "rows": rows,
        "note": dict(state.note),
        "suggestions": [{"label": display_text(text), "text": text} for text in SUGGESTIONS],
        "canContinue": bool(state.record.get(WINDOW)),
    }


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------

# The steps first run walks through. The optional voice step (enrolling the
# owner's voice) is added between these two when voices are built.
STEPS: tuple[dict[str, str], ...] = (
    {"id": "keys", "title": "Your keys"},
    {"id": "accuracy", "title": "How careful"},
)


def should_show_first_run(record: Mapping[str, str]) -> bool:
    """First run is for a Murmly with no dictation key bound.

    `murmly install` writes the hotkey record on every platform and `murmly
    uninstall` removes it, so no `window` entry means nothing has been set up.
    """
    return not record.get(WINDOW)


class FirstRun:
    """Where first run is: shown or not, and which step.

    Changed only by what the person does. Nothing the daemon reports shows or
    hides it, so it can never raise the window or move the focus.
    """

    def __init__(self, show: bool = False) -> None:
        self.visible = show
        self.index = 0

    @property
    def step(self) -> str:
        return STEPS[self.index]["id"]

    @property
    def last(self) -> bool:
        return self.index == len(STEPS) - 1

    def open(self) -> None:
        self.visible = True
        self.index = 0

    def next(self) -> None:
        if self.last:
            self.visible = False
        else:
            self.index += 1

    def back(self) -> None:
        self.index = max(0, self.index - 1)

    def later(self) -> None:
        """"Set up later": closed for this run of the app."""
        self.visible = False

    def view(self) -> dict[str, object]:
        steps = [
            {
                "id": item["id"],
                "title": item["title"],
                "number": number,
                "state": "done" if number - 1 < self.index else "current" if number - 1 == self.index else "todo",
            }
            for number, item in enumerate(STEPS, start=1)
        ]
        return {
            "visible": self.visible,
            "step": self.step,
            "index": self.index,
            "last": self.last,
            "steps": steps,
        }
