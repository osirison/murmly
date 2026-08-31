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
