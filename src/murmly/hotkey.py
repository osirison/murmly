"""Strict parsing of hotkey strings, split into one neutral parse and one
encoding per platform.

Parsing happens once, into a :class:`HotkeySpec` that names modifiers and the
key by Murmly's own canonical spellings and commits to no platform's
representation. Encoding happens once per platform that has a hotkey backend
today: :func:`encode_for_kde` produces the Qt key-code integer KDE's
``kglobalaccel`` speaks, and :func:`encode_for_gnome` produces the GTK
accelerator string GNOME's ``custom-keybindings`` speaks. A platform encodes a
modifier neither of the four physical keys it is built from has by refusing it
and naming it, rather than dropping or substituting it -- see ``Hyper``, which
GNOME accepts and KDE's Qt encoding has no bit for.

Parsing is deliberately stricter than Qt's own: Qt resolves an unrecognized
name to ``Key_unknown`` instead of failing, which would let a typo install a
binding that looks correct and never fires. Every KDE value here is
cross-checked against the desktop after registration; see
``docs/agent-notes/plasma-global-shortcut-binding.md``.

:func:`parse_hotkey` keeps its original signature and byte-identical KDE
output -- it is `parse_specification` followed by `encode_for_kde` -- so every
call site written before this split needed no change.
"""

from __future__ import annotations

from dataclasses import dataclass


SHIFT_MODIFIER = 0x02000000
CONTROL_MODIFIER = 0x04000000
ALT_MODIFIER = 0x08000000
META_MODIFIER = 0x10000000

# Canonical emission order for KDE's portable text, matching Qt's own.
MODIFIER_ORDER: tuple[tuple[str, int], ...] = (
    ("Meta", META_MODIFIER),
    ("Ctrl", CONTROL_MODIFIER),
    ("Alt", ALT_MODIFIER),
    ("Shift", SHIFT_MODIFIER),
)

#: The bits KDE's Qt-based encoding has. `Hyper` is deliberately absent: Qt's
#: `KeyboardModifier` flags have no Hyper bit, so a spec naming it is refused
#: by `encode_for_kde` rather than silently dropped.
KDE_MODIFIER_BITS: dict[str, int] = dict(MODIFIER_ORDER)

#: Canonical modifier names in the order Murmly emits them, platform-neutral.
#: Superset of `MODIFIER_ORDER`: `Hyper` is a modifier some platforms have and
#: KDE does not, so it sits outside the KDE-specific bit table above.
NEUTRAL_MODIFIER_ORDER: tuple[str, ...] = ("Meta", "Ctrl", "Alt", "Shift", "Hyper")

# Aliases users reach for. Qt's own parser accepts only "Meta"; GTK accelerator
# strings spell the same physical key "<Super>". "Cmd" and "Command" both name
# the GUI-position key -- Windows key, Super key, or Mac's Command key -- which
# is exactly what Murmly's "Meta" already means, so both fold to it.
MODIFIER_ALIASES = {
    "meta": "Meta",
    "super": "Meta",
    "win": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "hyper": "Hyper",
}

#: GTK accelerator tokens for each neutral modifier. GNOME accepts all five;
#: this table exists as data rather than as an inline conditional so a future
#: platform's own gap (like KDE's missing Hyper) is one more lookup miss, not a
#: new branch.
GNOME_MODIFIER_TOKENS: dict[str, str] = {
    "Meta": "<Super>",
    "Ctrl": "<Control>",
    "Alt": "<Alt>",
    "Shift": "<Shift>",
    "Hyper": "<Hyper>",
}
_GNOME_TOKEN_TO_MODIFIER: dict[str, str] = {token: name for name, token in GNOME_MODIFIER_TOKENS.items()}
#: `<Primary>` is GTK's portable alias for Control, resolved to the physical
#: Control key on every platform that is not macOS. Accepted on read; Murmly's
#: own writes always emit `<Control>`.
_GNOME_TOKEN_TO_MODIFIER["<Primary>"] = "Ctrl"

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
_KDE_VALUE_TO_NAMED_KEY: dict[int, str] = {value: name for name, value in _NAMED_KEYS.items()}

#: The GDK/X11 keysym name for each of the same named keys, for GNOME's
#: accelerator strings. Confirmed against the X11 keysym tables that GDK's
#: `gdk_keyval_name` reads from -- `BackSpace`, `ISO_Left_Tab` and the
#: `XF86Audio*` multimedia names are the ones that do not just match the Qt
#: spelling. Unverified against a live GNOME session; see
#: `docs/agent-notes/gnome-custom-keybindings.md`.
_GNOME_NAMED_KEYS: dict[str, str] = {
    "Escape": "Escape",
    "Tab": "Tab",
    "Backtab": "ISO_Left_Tab",
    "Backspace": "BackSpace",
    "Return": "Return",
    "Enter": "KP_Enter",
    "Insert": "Insert",
    "Delete": "Delete",
    "Pause": "Pause",
    "Print": "Print",
    "SysReq": "Sys_Req",
    "Clear": "Clear",
    "Home": "Home",
    "End": "End",
    "Left": "Left",
    "Up": "Up",
    "Right": "Right",
    "Down": "Down",
    "PgUp": "Page_Up",
    "PgDown": "Page_Down",
    "Space": "space",
    "Volume Down": "XF86AudioLowerVolume",
    "Volume Mute": "XF86AudioMute",
    "Volume Up": "XF86AudioRaiseVolume",
    "Media Play": "XF86AudioPlay",
    "Media Stop": "XF86AudioStop",
    "Media Previous": "XF86AudioPrev",
    "Media Next": "XF86AudioNext",
    "Microphone Mute": "XF86AudioMicMute",
}
_GNOME_KEY_TO_NAMED_KEY: dict[str, str] = {value: name for name, value in _GNOME_NAMED_KEYS.items()}

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
class HotkeySpec:
    """A platform-neutral hotkey: modifiers by canonical name, and the key.

    Commits to no platform's representation. `modifiers` holds names from
    `NEUTRAL_MODIFIER_ORDER`; `key` holds a single uppercase letter or digit,
    `"F1"`-`"F35"`, or one of the multi-word names in `_NAMED_KEYS` -- the same
    canonical spelling every encoder maps from.
    """

    modifiers: frozenset[str]
    key: str


@dataclass(frozen=True, slots=True)
class Hotkey:
    """A hotkey encoded for KDE's `kglobalaccel`.

    ``keycode`` is what the desktop reports back for this combination;
    ``portable`` is the untranslated string KDE's own parser accepts. Kept as
    the type `parse_hotkey` has always returned, byte-identical, so every
    existing call site needed no change when parsing split from encoding.
    """

    keycode: int
    portable: str

    def __str__(self) -> str:
        return self.portable


def parse_specification(text: str) -> HotkeySpec:
    """Parse ``text`` such as ``"Meta+X"`` into a platform-neutral spec.

    Raises :class:`HotkeyError` for anything ambiguous or unrecognized rather
    than guessing. Every rejection here is platform-neutral: a modifier this
    resolved platform's encoder cannot represent is refused later, by that
    encoder, so it can name the platform rather than just the modifier.
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

    modifiers: set[str] = set()
    for name in modifier_names:
        canonical = MODIFIER_ALIASES.get(name.casefold())
        if canonical is None:
            raise HotkeyError(
                f"Hotkey {raw!r} uses an unknown modifier {name!r}. "
                "Supported modifiers are Meta (also Super, Win, Cmd, or Command), "
                "Ctrl, Alt, Shift, and Hyper."
            )
        if canonical in modifiers:
            raise HotkeyError(f"Hotkey {raw!r} repeats the modifier {canonical!r}.")
        modifiers.add(canonical)

    if not modifiers:
        raise HotkeyError(
            f"Hotkey {raw!r} has no modifier. A global hotkey must include at "
            "least one of Meta, Ctrl, Alt, or Shift, such as Meta+X."
        )

    canonical_key = _canonicalize_key(key_name, raw)
    return HotkeySpec(modifiers=frozenset(modifiers), key=canonical_key)


def _canonicalize_key(name: str, raw: str) -> str:
    """Resolve ``name`` to Murmly's canonical, platform-neutral key spelling."""
    if MODIFIER_ALIASES.get(name.casefold()) is not None:
        raise HotkeyError(
            f"Hotkey {raw!r} ends with the modifier {name!r} instead of a key. "
            "Add the key you want, such as Meta+X."
        )

    if len(name) == 1:
        character = name.upper()
        if "A" <= character <= "Z" or "0" <= character <= "9":
            return character

    folded = name.casefold()

    if folded.startswith("f") and folded[1:].isdigit():
        number = int(folded[1:])
        if 1 <= number <= _MAX_FUNCTION_KEY:
            return f"F{number}"
        raise HotkeyError(
            f"Hotkey {raw!r} names function key {name!r}, which is outside the "
            f"supported range F1-F{_MAX_FUNCTION_KEY}."
        )

    collapsed = folded.replace(" ", "").replace("_", "").replace("-", "")
    canonical = _NAMED_KEY_ALIASES.get(collapsed)
    if canonical is not None:
        return canonical

    raise HotkeyError(
        f"Hotkey {raw!r} names an unrecognized key {name!r}. "
        f"Supported keys are {SUPPORTED_KEYS_SUMMARY}."
    )


def _spec_text(spec: HotkeySpec) -> str:
    """Reconstruct a readable specification for an encoder's own messages,
    for the case an encoder runs without the caller's original raw text (a
    spec built directly, as a test does, rather than parsed from text)."""
    ordered = [name for name in NEUTRAL_MODIFIER_ORDER if name in spec.modifiers]
    return "+".join([*ordered, spec.key])


def _kde_key_value(key: str) -> int:
    if len(key) == 1:
        return ord(key)
    if key.startswith("F") and key[1:].isdigit():
        return _FUNCTION_KEY_BASE + int(key[1:]) - 1
    return _NAMED_KEYS[key]


def encode_for_kde(spec: HotkeySpec, raw: str | None = None) -> Hotkey:
    """Encode ``spec`` into the Qt key-code integer KDE's `kglobalaccel` reads.

    Refuses a modifier the Qt `KeyboardModifier` flags have no bit for --
    `Hyper` is the one case among today's neutral modifiers -- naming it rather
    than dropping it.
    """
    label = raw if raw is not None else _spec_text(spec)
    modifiers = 0
    for name in NEUTRAL_MODIFIER_ORDER:
        if name not in spec.modifiers:
            continue
        bit = KDE_MODIFIER_BITS.get(name)
        if bit is None:
            raise HotkeyError(
                f"Hotkey {label!r} uses {name!r}, which KDE Plasma has no key for. "
                "Supported modifiers on this platform are Meta (also Super or Win), "
                "Ctrl, Alt, and Shift."
            )
        modifiers |= bit
    key_value = _kde_key_value(spec.key)
    portable = "+".join([name for name, bit in MODIFIER_ORDER if modifiers & bit] + [spec.key])
    return Hotkey(keycode=modifiers | key_value, portable=portable)


def parse_hotkey(text: str) -> Hotkey:
    """Parse ``text`` such as ``"Meta+X"`` into a :class:`Hotkey` for KDE.

    Equivalent to ``encode_for_kde(parse_specification(text))``, kept as one
    call so every site written before the parse/encode split needed no change.
    """
    spec = parse_specification(text)
    return encode_for_kde(spec, raw=text.strip())


def _gnome_key_name(key: str) -> str:
    if len(key) == 1:
        return key.lower()
    if key.startswith("F") and key[1:].isdigit():
        # GDK's function-key keysym names match Qt's spelling exactly:
        # both go "F1".."F35".
        return key
    return _GNOME_NAMED_KEYS[key]


def encode_for_gnome(spec: HotkeySpec, raw: str | None = None) -> str:
    """Encode ``spec`` into a GTK accelerator string, such as ``"<Super>x"``.

    GTK accepts every neutral modifier Murmly knows, so nothing is refused
    here today; the table is written as data so a future platform's own gap
    costs one lookup entry, not a new branch. Unverified against a live GNOME
    session -- see `docs/agent-notes/gnome-custom-keybindings.md`.
    """
    label = raw if raw is not None else _spec_text(spec)
    tokens: list[str] = []
    for name in NEUTRAL_MODIFIER_ORDER:
        if name not in spec.modifiers:
            continue
        token = GNOME_MODIFIER_TOKENS.get(name)
        if token is None:
            raise HotkeyError(
                f"Hotkey {label!r} uses {name!r}, which GNOME has no key for. "
                "Supported modifiers on this platform are Meta (also Super, Win, "
                "Cmd, or Command), Ctrl, Alt, Shift, and Hyper."
            )
        tokens.append(token)
    return "".join(tokens) + _gnome_key_name(spec.key)


#: `RegisterHotKey`'s `fsModifiers` bits (`winuser.h`). `MOD_NOREPEAT` is not a
#: physical modifier -- it is folded into every registration `win_hotkey.py`
#: makes, not into this table, because a spec never names it and refusing it
#: by name would make no sense to a person who never wrote it.
WINDOWS_MOD_ALT = 0x0001
WINDOWS_MOD_CONTROL = 0x0002
WINDOWS_MOD_SHIFT = 0x0004
WINDOWS_MOD_WIN = 0x0008
WINDOWS_MOD_NOREPEAT = 0x4000

#: The bits Windows' `RegisterHotKey` has. `Hyper` is absent for the same
#: reason it is absent from `KDE_MODIFIER_BITS`: there is no fifth modifier bit
#: to give it, so a spec naming it is refused by name rather than dropped.
WINDOWS_MODIFIER_BITS: dict[str, int] = {
    "Meta": WINDOWS_MOD_WIN,
    "Ctrl": WINDOWS_MOD_CONTROL,
    "Alt": WINDOWS_MOD_ALT,
    "Shift": WINDOWS_MOD_SHIFT,
}

#: Virtual-key codes (`winuser.h`) for the named keys Windows can register.
#: Four of Murmly's named keys have no entry and are refused by
#: `_windows_key_value` instead: `SysReq` and `Backtab` are not standalone
#: virtual keys on Windows (the first is conventionally Alt+PrintScreen, the
#: second is Shift+Tab), and `Microphone Mute` has no assigned virtual-key
#: code at all. `Enter` and `Return` both map to `VK_RETURN` -- Windows does
#: not distinguish the main and numpad Enter keys at the virtual-key level.
_WINDOWS_NAMED_KEYS: dict[str, int] = {
    "Escape": 0x1B,
    "Tab": 0x09,
    "Backspace": 0x08,
    "Return": 0x0D,
    "Enter": 0x0D,
    "Insert": 0x2D,
    "Delete": 0x2E,
    "Pause": 0x13,
    "Print": 0x2C,
    "Clear": 0x0C,
    "Home": 0x24,
    "End": 0x23,
    "Left": 0x25,
    "Up": 0x26,
    "Right": 0x27,
    "Down": 0x28,
    "PgUp": 0x21,
    "PgDown": 0x22,
    "Space": 0x20,
    "Volume Down": 0xAE,
    "Volume Mute": 0xAD,
    "Volume Up": 0xAF,
    "Media Play": 0xB3,
    "Media Stop": 0xB2,
    "Media Previous": 0xB1,
    "Media Next": 0xB0,
}

#: `VK_F1`. Windows defines virtual keys only up to `VK_F24` (`0x87`), unlike
#: KDE's Qt encoding and GNOME's keysyms, which both go to F35 -- so a spec
#: naming F25 or higher parses fine and is refused here, by this platform,
#: rather than by the platform-neutral parse that has no way to know Windows'
#: own ceiling is lower than the other two.
_WINDOWS_FUNCTION_KEY_BASE = 0x70
_WINDOWS_MAX_FUNCTION_KEY = 24


@dataclass(frozen=True, slots=True)
class WindowsHotkey:
    """A hotkey encoded for `RegisterHotKey`.

    `modifiers` is the `fsModifiers` bitmask (never including
    `WINDOWS_MOD_NOREPEAT`, which is not a property of the key combination
    itself -- see that constant's docstring) and `vk` is the virtual-key code.
    `portable` is the same untranslated text `Hotkey.portable` produces, so a
    binding this encoder produced round-trips through `HotkeyRecordStore`
    exactly like a KDE or GNOME one does.
    """

    modifiers: int
    vk: int
    portable: str


def _windows_key_value(key: str, label: str) -> int:
    if len(key) == 1:
        return ord(key.upper())
    if key.startswith("F") and key[1:].isdigit():
        number = int(key[1:])
        if number <= _WINDOWS_MAX_FUNCTION_KEY:
            return _WINDOWS_FUNCTION_KEY_BASE + number - 1
        raise HotkeyError(
            f"Hotkey {label!r} names {key!r}, which is outside the range Windows can "
            f"register: F1-F{_WINDOWS_MAX_FUNCTION_KEY}."
        )
    value = _WINDOWS_NAMED_KEYS.get(key)
    if value is not None:
        return value
    raise HotkeyError(f"Hotkey {label!r} names {key!r}, which Windows has no key for.")


def encode_for_windows(spec: HotkeySpec, raw: str | None = None) -> WindowsHotkey:
    """Encode `spec` for `RegisterHotKey`.

    Refuses `Hyper` and any key Windows cannot register (`_windows_key_value`),
    naming it rather than dropping or substituting it, matching
    `encode_for_kde` and `encode_for_gnome`.
    """
    label = raw if raw is not None else _spec_text(spec)
    modifiers = 0
    for name in NEUTRAL_MODIFIER_ORDER:
        if name not in spec.modifiers:
            continue
        bit = WINDOWS_MODIFIER_BITS.get(name)
        if bit is None:
            raise HotkeyError(
                f"Hotkey {label!r} uses {name!r}, which Windows has no key for. "
                "Supported modifiers on this platform are Meta (also Super or Win), "
                "Ctrl, Alt, and Shift."
            )
        modifiers |= bit
    vk = _windows_key_value(spec.key, label)
    return WindowsHotkey(modifiers=modifiers, vk=vk, portable=_spec_text(spec))


def windows_hotkey_for_portable(portable: str) -> WindowsHotkey:
    """The Windows encoding for a hotkey already stored as portable text.

    `portable` is `Hotkey.portable` / `HotkeyRecordStore`'s own currency --
    parsed back to a neutral spec and re-encoded, the same shape
    `gnome_accelerator` already uses for GNOME's own storage.
    """
    return encode_for_windows(parse_specification(portable))


#: Carbon `Events.h`'s `EventModifiers` bits, used by `RegisterEventHotKey`
#: (task 13.5). Kept separate from `WINDOWS_MODIFIER_BITS`: the numeric values
#: differ and mean a different bitfield (`EventModifiers`, not `fsModifiers`).
#: `Hyper` is absent for the same reason it is absent from the other two
#: platform tables -- there is no fifth Carbon modifier bit to give it, so a
#: spec naming it is refused by name rather than dropped.
MACOS_CMD_KEY = 0x0100
MACOS_SHIFT_KEY = 0x0200
MACOS_OPTION_KEY = 0x0800
MACOS_CONTROL_KEY = 0x1000

MACOS_MODIFIER_BITS: dict[str, int] = {
    "Meta": MACOS_CMD_KEY,
    "Ctrl": MACOS_CONTROL_KEY,
    "Alt": MACOS_OPTION_KEY,
    "Shift": MACOS_SHIFT_KEY,
}

#: `HIToolbox/Events.h`'s `kVK_ANSI_*` virtual-key codes for the letters and
#: digits -- physical positions on a US ANSI keyboard, not characters. See
#: `encode_for_macos`'s docstring for why that distinction matters here and
#: nowhere else in this module. Transcribed from Apple's published header and
#: not confirmed on a real Mac -- the same caveat `_WINDOWS_NAMED_KEYS` and
#: `_GNOME_NAMED_KEYS` carry for their own tables.
_MACOS_LETTER_DIGIT_KEYS: dict[str, int] = {
    "A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05,
    "Z": 0x06, "X": 0x07, "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C,
    "W": 0x0D, "E": 0x0E, "R": 0x0F, "Y": 0x10, "T": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17,
    "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
    "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23,
    "L": 0x25, "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E,
}

#: `kVK_*` codes for the subset of Murmly's named keys a Mac keyboard has.
#: Every key absent here (`Insert`, `Pause`, `Print`, `SysReq`, and the four
#: media keys) has no Carbon virtual-key code at all -- Apple keyboards do not
#: have the first four, and the media keys are delivered as `NX_KEYTYPE`
#: special-key events rather than ordinary `kVK_` presses -- so `_macos_key_
#: value` refuses them by name exactly as `_windows_key_value` refuses
#: `SysReq`/`Backtab`/`Microphone Mute`.
_MACOS_NAMED_KEYS: dict[str, int] = {
    "Escape": 0x35,
    "Tab": 0x30,
    # kVK_Delete: the key every Apple keyboard itself labels "delete" and
    # every other platform Murmly supports calls Backspace.
    "Backspace": 0x33,
    # kVK_ForwardDelete: what Windows and X11 call Delete.
    "Delete": 0x75,
    "Return": 0x24,
    # kVK_ANSI_KeypadEnter -- the numpad key, distinct from Return exactly as
    # Windows' and GNOME's own encodings distinguish the two.
    "Enter": 0x4C,
    "Clear": 0x47,
    "Home": 0x73,
    "End": 0x77,
    "Left": 0x7B,
    "Up": 0x7E,
    "Right": 0x7C,
    "Down": 0x7D,
    "PgUp": 0x74,
    "PgDown": 0x79,
    "Space": 0x31,
    "Volume Down": 0x49,
    "Volume Mute": 0x4A,
    "Volume Up": 0x48,
}

#: `kVK_F1`..`kVK_F20`. Positional, not linear like KDE/GNOME's `base + n` or
#: Windows' contiguous `VK_F1..VK_F24` -- Carbon assigns function-key codes in
#: the order Apple added the physical keys, not numeric order, which is
#: exactly the kind of table a wrong recall corrupts silently; see
#: `SamePhysicalMacosKeyTests` for the invariants pinned against it.
_MACOS_FUNCTION_KEYS: dict[int, int] = {
    1: 0x7A, 2: 0x78, 3: 0x63, 4: 0x76, 5: 0x60, 6: 0x61, 7: 0x62, 8: 0x64,
    9: 0x65, 10: 0x6D, 11: 0x67, 12: 0x6F, 13: 0x69, 14: 0x6B, 15: 0x71,
    16: 0x6A, 17: 0x40, 18: 0x4F, 19: 0x50, 20: 0x5A,
}
#: `Events.h` defines Carbon function-key codes only up to `kVK_F20` -- lower
#: than KDE's/GNOME's F35 and Windows' own F24 -- so a spec naming F21 or
#: higher parses fine and is refused here, by this platform, rather than by
#: the platform-neutral parse that has no way to know Carbon's own ceiling is
#: the lowest of the three.
_MACOS_MAX_FUNCTION_KEY = 20


@dataclass(frozen=True, slots=True)
class MacosHotkey:
    """A hotkey encoded for Carbon's `RegisterEventHotKey`.

    `modifiers` is the `EventModifiers` bitmask `RegisterEventHotKey`'s third
    argument takes, built from `MACOS_MODIFIER_BITS`; `key_code` is the
    `UInt32` virtual-key code its fourth argument takes. `portable` is the same
    untranslated text `Hotkey.portable`/`WindowsHotkey.portable` produce, so a
    binding this encoder produced round-trips through `HotkeyRecordStore`
    exactly like a KDE, GNOME, or Windows one does.
    """

    modifiers: int
    key_code: int
    portable: str


def _macos_key_value(key: str, label: str) -> int:
    if key in _MACOS_LETTER_DIGIT_KEYS:
        return _MACOS_LETTER_DIGIT_KEYS[key]
    if key.startswith("F") and key[1:].isdigit():
        number = int(key[1:])
        value = _MACOS_FUNCTION_KEYS.get(number)
        if value is not None:
            return value
        raise HotkeyError(
            f"Hotkey {label!r} names {key!r}, which is outside the range macOS can "
            f"register: F1-F{_MACOS_MAX_FUNCTION_KEY}."
        )
    value = _MACOS_NAMED_KEYS.get(key)
    if value is not None:
        return value
    raise HotkeyError(f"Hotkey {label!r} names {key!r}, which macOS has no key for.")


def encode_for_macos(spec: HotkeySpec, raw: str | None = None) -> MacosHotkey:
    """Encode `spec` for Carbon's `RegisterEventHotKey`.

    `_MACOS_LETTER_DIGIT_KEYS` looks a letter or digit up by its physical
    position on a US ANSI keyboard, unlike `_kde_key_value`/`_windows_key_
    value`, which both derive the code with `ord()` because Qt's and Win32's
    own key constants already track whatever keyboard-layout remapping the OS
    applied. Carbon's `kVK_*` codes predate that remapping: they name a
    physical key regardless of layout, so a spec built from one of Murmly's
    letter names is translated here by table lookup rather than by `ord()`,
    and would resolve to a different physical key than the letter it names on
    a non-QWERTY-derived ANSI layout such as Dvorak.

    Refuses `Hyper` and any key macOS cannot register (`_macos_key_value`),
    naming it rather than dropping or substituting it, matching
    `encode_for_kde`, `encode_for_gnome` and `encode_for_windows`.
    """
    label = raw if raw is not None else _spec_text(spec)
    modifiers = 0
    for name in NEUTRAL_MODIFIER_ORDER:
        if name not in spec.modifiers:
            continue
        bit = MACOS_MODIFIER_BITS.get(name)
        if bit is None:
            raise HotkeyError(
                f"Hotkey {label!r} uses {name!r}, which macOS has no key for. "
                "Supported modifiers on this platform are Meta (also Cmd or "
                "Command), Ctrl, Alt, and Shift."
            )
        modifiers |= bit
    key_code = _macos_key_value(spec.key, label)
    return MacosHotkey(modifiers=modifiers, key_code=key_code, portable=_spec_text(spec))


def macos_hotkey_for_portable(portable: str) -> MacosHotkey:
    """The macOS encoding for a hotkey already stored as portable text.

    `portable` is `Hotkey.portable` / `HotkeyRecordStore`'s own currency --
    parsed back to a neutral spec and re-encoded, the same shape
    `windows_hotkey_for_portable` already uses for its own storage.
    """
    return encode_for_macos(parse_specification(portable))


def gnome_accelerator(portable: str) -> str:
    """The GTK accelerator for a hotkey already encoded as KDE portable text.

    `portable` is `Hotkey.portable`: canonical, platform-neutral text that
    reparses to the same physical key. A second platform's backend derives its
    own representation this way rather than `Hotkey` growing a field only that
    platform reads.
    """
    return encode_for_gnome(parse_specification(portable))


def decode_kde_keycode(keycode: int) -> HotkeySpec:
    """The spec that encodes to ``keycode`` under `encode_for_kde`.

    KDE's Qt-based encoding is a lossless (modifiers, key) encoding, so this is
    exactly its inverse. Used to compare a keycode a caller already holds
    against a binding read back from a different platform's own storage,
    without that platform needing to know about Qt key codes at all.
    """
    modifiers = frozenset(name for name, bit in MODIFIER_ORDER if keycode & bit)
    value = keycode
    for _name, bit in MODIFIER_ORDER:
        value &= ~bit
    if 0 <= value < 0x110000 and chr(value).isascii() and (chr(value).isalpha() or chr(value).isdigit()):
        character = chr(value)
        if character.isalpha():
            return HotkeySpec(modifiers=modifiers, key=character.upper())
        return HotkeySpec(modifiers=modifiers, key=character)
    if _FUNCTION_KEY_BASE <= value < _FUNCTION_KEY_BASE + _MAX_FUNCTION_KEY:
        return HotkeySpec(modifiers=modifiers, key=f"F{value - _FUNCTION_KEY_BASE + 1}")
    name = _KDE_VALUE_TO_NAMED_KEY.get(value)
    if name is not None:
        return HotkeySpec(modifiers=modifiers, key=name)
    raise HotkeyError(f"{keycode!r} is not a key value Murmly's KDE encoding produces.")


def gnome_accelerator_for_keycode(keycode: int) -> str:
    """The GTK accelerator for a KDE-encoded keycode, via `decode_kde_keycode`."""
    return encode_for_gnome(decode_kde_keycode(keycode))


def parse_gnome_accelerator(text: str) -> HotkeySpec | None:
    """Parse a GTK accelerator string such as ``"<Super>x"`` into a spec.

    Lenient rather than strict, unlike `parse_specification`: this reads
    bindings GNOME reports back, which may belong to another application
    Murmly has no reason to refuse understanding. Returns ``None`` for
    anything not recognized -- empty, unparseable, or naming a key outside
    Murmly's own table -- rather than raising, so a caller scanning every
    registered binding for a conflict can skip what it cannot read instead of
    aborting the scan.
    """
    if not text:
        return None
    remainder = text
    modifiers: set[str] = set()
    while remainder.startswith("<"):
        end = remainder.find(">")
        if end == -1:
            return None
        token = remainder[: end + 1]
        canonical = _GNOME_TOKEN_TO_MODIFIER.get(token)
        if canonical is None:
            return None
        modifiers.add(canonical)
        remainder = remainder[end + 1 :]
    if not remainder:
        return None
    if len(remainder) == 1 and (remainder.isalpha() or remainder.isdigit()):
        return HotkeySpec(modifiers=frozenset(modifiers), key=remainder.upper())
    if remainder.startswith("F") and remainder[1:].isdigit():
        number = int(remainder[1:])
        if 1 <= number <= _MAX_FUNCTION_KEY:
            return HotkeySpec(modifiers=frozenset(modifiers), key=remainder)
        return None
    name = _GNOME_KEY_TO_NAMED_KEY.get(remainder)
    if name is not None:
        return HotkeySpec(modifiers=frozenset(modifiers), key=name)
    return None
