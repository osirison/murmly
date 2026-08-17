"""Strict parsing of hotkey strings into the integer encoding KDE uses.

A key combination is a single integer: the Qt key value OR-ed with the Qt
modifier bits. Parsing is deliberately stricter than Qt's own: Qt resolves an
unrecognized name to ``Key_unknown`` instead of failing, which would let a typo
install a binding that looks correct and never fires. Every value here is
cross-checked against the desktop after registration; see
``docs/agent-notes/plasma-global-shortcut-binding.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


SHIFT_MODIFIER = 0x02000000
CONTROL_MODIFIER = 0x04000000
ALT_MODIFIER = 0x08000000
META_MODIFIER = 0x10000000

# Canonical emission order, matching Qt's own portable text.
MODIFIER_ORDER: tuple[tuple[str, int], ...] = (
    ("Meta", META_MODIFIER),
    ("Ctrl", CONTROL_MODIFIER),
    ("Alt", ALT_MODIFIER),
    ("Shift", SHIFT_MODIFIER),
)

# Aliases users reach for. Qt's parser accepts only "Meta".
MODIFIER_ALIASES = {
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "cmd": "Meta",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}

_NAMED_KEYS: dict[str, int] = {
    "Escape": 0x01000000,
    "Tab": 0x01000001,
    "Backtab": 0x01000002,
    "Backspace": 0x01000003,
    "Return": 0x01000004,
    "Enter": 0x01000005,
    "Insert": 0x01000006,
    "Delete": 0x01000007,
    "Pause": 0x01000008,
    "Print": 0x01000009,
    "SysReq": 0x0100000A,
    "Clear": 0x0100000B,
    "Home": 0x01000010,
    "End": 0x01000011,
    "Left": 0x01000012,
    "Up": 0x01000013,
    "Right": 0x01000014,
    "Down": 0x01000015,
    "PgUp": 0x01000016,
    "PgDown": 0x01000017,
    "Space": 0x20,
    "Volume Down": 0x01000070,
    "Volume Mute": 0x01000071,
    "Volume Up": 0x01000072,
    "Media Play": 0x01000080,
    "Media Stop": 0x01000081,
    "Media Previous": 0x01000082,
    "Media Next": 0x01000083,
    "Microphone Mute": 0x01000113,
}

# Accepted spellings for the named keys above, plus a few common synonyms.
_NAMED_KEY_ALIASES: dict[str, str] = {
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "backtab": "Backtab",
    "backspace": "Backspace",
    "return": "Return",
    "enter": "Enter",
    "insert": "Insert",
    "ins": "Insert",
    "delete": "Delete",
    "del": "Delete",
    "pause": "Pause",
    "print": "Print",
    "printscreen": "Print",
    "sysreq": "SysReq",
    "clear": "Clear",
    "home": "Home",
    "end": "End",
    "left": "Left",
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "pgup": "PgUp",
    "pageup": "PgUp",
    "pgdown": "PgDown",
    "pagedown": "PgDown",
    "pgdn": "PgDown",
    "space": "Space",
    "volumedown": "Volume Down",
    "volumemute": "Volume Mute",
    "volumeup": "Volume Up",
    "mediaplay": "Media Play",
    "mediastop": "Media Stop",
    "mediaprevious": "Media Previous",
    "medianext": "Media Next",
    "microphonemute": "Microphone Mute",
    "micmute": "Microphone Mute",
}

_FUNCTION_KEY_BASE = 0x01000030
_MAX_FUNCTION_KEY = 35

SUPPORTED_KEYS_SUMMARY = (
    "A-Z, 0-9, F1-F35, and "
    + ", ".join(sorted(_NAMED_KEYS))
)


class HotkeyError(ValueError):
    """A hotkey string that Murmly refuses to interpret."""


@dataclass(frozen=True, slots=True)
class Hotkey:
    """A parsed hotkey.

    ``keycode`` is what the desktop reports back for this combination;
    ``portable`` is the untranslated string KDE's own parser accepts.
    """

    keycode: int
    portable: str

    def __str__(self) -> str:
        return self.portable


def parse_hotkey(text: str) -> Hotkey:
    """Parse ``text`` such as ``"Meta+X"`` into a :class:`Hotkey`.

    Raises :class:`HotkeyError` for anything ambiguous or unrecognized rather
    than guessing.
    """
    if not isinstance(text, str) or not text.strip():
        raise HotkeyError("No hotkey was given. Example: Meta+X")

    raw = text.strip()
    if "," in raw:
        raise HotkeyError(
            f"Hotkey {raw!r} contains a comma. A comma separates alternative "
            "shortcuts on this desktop, so it cannot appear in a hotkey. "
            "Use a single combination such as Meta+X."
        )

    parts = [part.strip() for part in raw.split("+")]
    if any(not part for part in parts):
        raise HotkeyError(
            f"Hotkey {raw!r} has an empty part. Write modifiers and the key "
            "separated by single '+' characters, such as Meta+Shift+X."
        )

    modifier_names = parts[:-1]
    key_name = parts[-1]

    modifiers = 0
    seen: set[str] = set()
    for name in modifier_names:
        canonical = MODIFIER_ALIASES.get(name.casefold())
        if canonical is None:
            raise HotkeyError(
                f"Hotkey {raw!r} uses an unknown modifier {name!r}. "
                "Supported modifiers are Meta (also Super or Win), Ctrl, Alt, and Shift."
            )
        if canonical in seen:
            raise HotkeyError(f"Hotkey {raw!r} repeats the modifier {canonical!r}.")
        seen.add(canonical)
        modifiers |= dict(MODIFIER_ORDER)[canonical]

    if not modifiers:
        raise HotkeyError(
            f"Hotkey {raw!r} has no modifier. A global hotkey must include at "
            "least one of Meta, Ctrl, Alt, or Shift, such as Meta+X."
        )

    key_value, canonical_key = _resolve_key(key_name, raw)
    portable = "+".join(
        [name for name, bit in MODIFIER_ORDER if modifiers & bit] + [canonical_key]
    )
    return Hotkey(keycode=modifiers | key_value, portable=portable)


def _resolve_key(name: str, raw: str) -> tuple[int, str]:
    if MODIFIER_ALIASES.get(name.casefold()) is not None:
        raise HotkeyError(
            f"Hotkey {raw!r} ends with the modifier {name!r} instead of a key. "
            "Add the key you want, such as Meta+X."
        )

    if len(name) == 1:
        character = name.upper()
        if "A" <= character <= "Z" or "0" <= character <= "9":
            return ord(character), character

    folded = name.casefold()

    if folded.startswith("f") and folded[1:].isdigit():
        number = int(folded[1:])
        if 1 <= number <= _MAX_FUNCTION_KEY:
            return _FUNCTION_KEY_BASE + number - 1, f"F{number}"
        raise HotkeyError(
            f"Hotkey {raw!r} names function key {name!r}, which is outside the "
            f"supported range F1-F{_MAX_FUNCTION_KEY}."
        )

    collapsed = folded.replace(" ", "").replace("_", "").replace("-", "")
    canonical = _NAMED_KEY_ALIASES.get(collapsed)
    if canonical is not None:
        return _NAMED_KEYS[canonical], canonical

    raise HotkeyError(
        f"Hotkey {raw!r} names an unrecognized key {name!r}. "
        f"Supported keys are {SUPPORTED_KEYS_SUMMARY}."
    )
