"""Global shortcut backends for the desktops Murmly registers hotkeys on.

`PlasmaShortcuts` is a read-only getter: Murmly registers a KDE hotkey by
writing a launcher file and letting Plasma discover it, never by calling a
shortcut setter; see ``docs/agent-notes/plasma-global-shortcut-binding.md`` and
``design.md`` for why the setter path registers a shortcut that never fires.

Only signatures made of plain scalars are permitted on that D-Bus path.
``kglobalacceld`` demarshals an inbound key-sequence struct with four
unconditional reads, so any ``(ai)`` argument whose length is not exactly four
reads past the end and aborts the daemon. :data:`ALLOWED_SIGNATURES` enforces
that no such call can be built.

`GnomeShortcuts` and `GnomeShortcutLauncher` are GNOME's counterpart. GNOME has
no split between a write mechanism and a query mechanism the way Plasma does:
`custom-keybindings` is a live dconf value that is both, so one pair of classes
here plays both the query role (`PlasmaShortcuts`) and the write role
(`installer.ShortcutLauncher`) plays for KDE. See
``docs/agent-notes/gnome-custom-keybindings.md`` for the gsettings shape and
its unverified scope.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
import subprocess

from murmly.hotkey import (
    HotkeyError,
    HotkeySpec,
    decode_kde_keycode,
    encode_for_kde,
    gnome_accelerator,
    parse_gnome_accelerator,
)
from murmly.overlay import OverlayBackend
from murmly.platform import Desktop, PlatformProfile, resolve_platform


logger = logging.getLogger(__name__)


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

BUS_NAME = "org.kde.kglobalaccel"
OBJECT_PATH = "/kglobalaccel"
INTERFACE = "org.kde.KGlobalAccel"
LAUNCH_ACTION = "_launch"
QUERY_TIMEOUT_SECONDS = 5.0

#: Argument signatures Murmly is allowed to send. Scalars only, by design: an
#: inbound "(ai)" struct crashes kglobalacceld unless it holds exactly four
#: integers, and Murmly never needs one.
ALLOWED_SIGNATURES = frozenset({"", "i", "is", "s", "as"})

class DesktopQueryError(RuntimeError):
    """A desktop query could not be answered.

    Distinct from a query that answered "not present": callers must not treat a
    failed lookup as evidence that a hotkey is free.
    """


@dataclass(frozen=True, slots=True)
class ShortcutOwner:
    action_unique: str
    action_friendly: str
    component_unique: str
    component_friendly: str

    @property
    def label(self) -> str:
        """A human-readable owner name for a refusal message."""
        friendly = self.component_friendly or self.component_unique
        if self.action_friendly and self.action_friendly != friendly:
            return f"{friendly} ({self.action_friendly})"
        return friendly


#: Desktops Murmly can register a hotkey on at all. Anything else is this
#: desktop's limitation, not the platform's -- see task 4.6 and the
#: `desktop-integration` spec's "The platform registers hotkeys but this
#: desktop does not" scenario, which this table is what makes that
#: distinction possible once more than one desktop is supported.
HOTKEY_CAPABLE_DESKTOPS: frozenset[Desktop] = frozenset({Desktop.PLASMA, Desktop.GNOME})

#: Desktop, session-type combinations verified end-to-end against a live
#: session. Every other combination among `HOTKEY_CAPABLE_DESKTOPS` still runs
#: the same code path -- GNOME's mechanism is a documented convention, not
#: something observed on a real `gnome-settings-daemon` -- but Murmly reports
#: it as unverified rather than claiming it. See
#: `docs/agent-notes/gnome-custom-keybindings.md`.
VERIFIED_DESKTOP_SESSIONS: frozenset[tuple[Desktop, OverlayBackend]] = frozenset(
    {(Desktop.PLASMA, OverlayBackend.X11)}
)


def _desktop_label(desktop: Desktop) -> str:
    return "KDE Plasma" if desktop is Desktop.PLASMA else "GNOME"


def _display_backend(profile: PlatformProfile) -> OverlayBackend | None:
    """Whether this session has a usable display, and which protocol it is.

    Deliberately not `overlay.detect_overlay_backend`: that function also
    gates on Plasma, because GTK4 overlay rendering is Plasma-only today. A
    hotkey needs a live session, not an overlay, so this asks the narrower
    question on its own -- the same display-presence logic, without the
    overlay-specific desktop gate.
    """
    if profile.session_type == "wayland":
        return OverlayBackend.WAYLAND if profile.wayland_display else None
    if profile.session_type == "x11":
        return OverlayBackend.X11 if profile.x11_display else None
    if not profile.session_type:
        if profile.wayland_display:
            return OverlayBackend.WAYLAND
        if profile.x11_display:
            return OverlayBackend.X11
    return None


@dataclass(frozen=True, slots=True)
class DesktopSession:
    """What Murmly can do about hotkeys in the current session."""

    is_plasma: bool
    session_type: str
    backend: OverlayBackend | None
    supported: bool
    verified: bool
    detail: str
    # Defaulted and last, so a construction written before GNOME support
    # existed -- none does, but the pattern this dataclass sets is what
    # matters -- keeps working.
    desktop: Desktop = Desktop.OTHER


def detect_desktop_session(environment: dict[str, str] | None = None) -> DesktopSession:
    source = environment if environment is not None else os.environ
    profile = resolve_platform(source)
    session_type = profile.session_type or "unknown"
    desktop = profile.desktop
    is_plasma = desktop is Desktop.PLASMA

    if desktop not in HOTKEY_CAPABLE_DESKTOPS:
        return DesktopSession(
            is_plasma=False,
            session_type=session_type,
            backend=None,
            supported=False,
            verified=False,
            detail="Hotkey registration requires KDE Plasma or GNOME.",
            desktop=desktop,
        )

    label = _desktop_label(desktop)
    backend = _display_backend(profile)
    if backend is None:
        return DesktopSession(
            is_plasma=is_plasma,
            session_type=session_type,
            backend=None,
            supported=False,
            verified=False,
            detail=f"{label} was detected but no graphical display was found in the environment.",
            desktop=desktop,
        )

    verified = (desktop, backend) in VERIFIED_DESKTOP_SESSIONS
    detail = (
        f"{label} on {backend.value}."
        if verified
        else (
            f"{label} on {backend.value}. Hotkey registration is unverified on this "
            "session; Murmly will register the hotkey and then check whether it took effect."
        )
    )
    return DesktopSession(
        is_plasma=is_plasma,
        session_type=session_type,
        backend=backend,
        supported=True,
        verified=verified,
        detail=detail,
        desktop=desktop,
    )


class PlasmaShortcuts:
    """Read-only view of the running Plasma shortcut registry."""

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        timeout: float = QUERY_TIMEOUT_SECONDS,
        busctl: str = "busctl",
    ) -> None:
        self._run_command = run_command
        self._timeout = timeout
        self._busctl = busctl

    def is_available(self, keycode: int) -> bool:
        """Whether ``keycode`` is free for a new owner.

        A hotkey already held by Murmly reports as unavailable, so callers must
        check ownership before treating this as a conflict.
        """
        payload = self._call("isGlobalShortcutAvailable", "is", str(keycode), "")
        value = payload["data"][0]
        if not isinstance(value, bool):
            raise DesktopQueryError("Shortcut availability query returned a non-boolean result.")
        return value

    def owners_of(self, keycode: int) -> list[ShortcutOwner]:
        """Every registered owner of ``keycode``.

        More than one owner means a silent double-bind: Plasma refcounts the
        grab and delivers the key to the lowest serial, so the newcomer never
        fires.
        """
        payload = self._call("getGlobalShortcutsByKey", "i", str(keycode))
        rows = payload["data"][0]
        owners: list[ShortcutOwner] = []
        for row in rows:
            if len(row) < 4:
                raise DesktopQueryError("Shortcut owner query returned a malformed row.")
            owners.append(
                ShortcutOwner(
                    action_unique=str(row[0]),
                    action_friendly=str(row[1]),
                    component_unique=str(row[2]),
                    component_friendly=str(row[3]),
                )
            )
        return owners

    def component_exists(self, component: str) -> bool:
        """Whether Plasma has built a shortcut component for ``component``.

        An absent component is reported as ``False``; a query that could not be
        answered raises, so a poll never mistakes a broken bus for "gone".
        """
        result = self._run(["call", BUS_NAME, OBJECT_PATH, INTERFACE, "getComponent", "s", component])
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").strip()
        if "doesn't exist" in stderr or "does not exist" in stderr:
            return False
        raise DesktopQueryError(f"Unable to query component {component!r}: {stderr or 'unknown error'}")

    def registered_keys(self, component: str) -> list[int]:
        """The key codes Plasma currently has registered for ``component``.

        Reading a key sequence back is safe; only *sending* one is not.
        """
        payload = self._call(
            "shortcutKeys",
            "as",
            "4",
            component,
            LAUNCH_ACTION,
            "",
            "",
        )
        sequences = payload["data"][0]
        keys: list[int] = []
        for sequence in sequences:
            # Each sequence is a struct holding one array of four key
            # combinations, zero-padded for the slots a shortcut does not use.
            if not sequence or not isinstance(sequence[0], list):
                raise DesktopQueryError("Registered key query returned a malformed sequence.")
            keys.extend(int(value) for value in sequence[0] if int(value) != 0)
        return keys

    def _call(self, method: str, signature: str, *arguments: str) -> dict[str, object]:
        if signature not in ALLOWED_SIGNATURES:
            raise DesktopQueryError(
                f"Refusing to call {method} with signature {signature!r}. Murmly sends only "
                "scalar arguments; a key-sequence struct aborts the Plasma shortcut daemon."
            )
        result = self._run(
            ["call", BUS_NAME, OBJECT_PATH, INTERFACE, method, signature, *arguments]
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise DesktopQueryError(f"{method} failed: {stderr or 'unknown error'}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise DesktopQueryError(f"{method} returned unreadable output: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
            raise DesktopQueryError(f"{method} returned an unexpected payload.")
        return payload

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [self._busctl, "--user", "--json=short", *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopQueryError(f"Unable to reach the Plasma shortcut daemon: {error}") from error


GNOME_MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_CUSTOM_KEYBINDING_SCHEMA = f"{GNOME_MEDIA_KEYS_SCHEMA}.custom-keybinding"
GNOME_CUSTOM_KEYBINDINGS_KEY = "custom-keybindings"
GNOME_CUSTOM_KEYBINDINGS_BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
GNOME_QUERY_TIMEOUT_SECONDS = 5.0

GNOME_WM_KEYBINDINGS_SCHEMA = "org.gnome.desktop.wm.keybindings"
GNOME_SHELL_KEYBINDINGS_SCHEMA = "org.gnome.shell.keybindings"

#: GNOME's own fixed shortcuts, scanned in addition to `custom-keybindings`.
#: `GNOME_MEDIA_KEYS_SCHEMA` appears twice in this module for two different
#: reasons: as the *base* schema here, it carries GNOME's own built-in
#: volume/brightness/screenshot bindings directly; the *relocatable*
#: `GNOME_CUSTOM_KEYBINDING_SCHEMA` above is what each `custom-keybindings`
#: entry is addressed through, and is never in this tuple. A key one of these
#: schemas holds is a GNOME shortcut Murmly did not create and cannot rebind;
#: taking it anyway leaves the new hotkey silently unfired, since GNOME does
#: not arbitrate two claimants any more than KDE's `kglobalaccel` does. See
#: ``docs/agent-notes/gnome-custom-keybindings.md``.
GNOME_FIXED_SCHEMAS: tuple[str, ...] = (
    GNOME_WM_KEYBINDINGS_SCHEMA,
    GNOME_SHELL_KEYBINDINGS_SCHEMA,
    GNOME_MEDIA_KEYS_SCHEMA,
)


def gnome_binding_path(slug: str) -> str:
    """The dconf path for one of Murmly's own custom-keybinding entries.

    `slug` is a purpose's own short name (``"window"``, ``"session"``), not the
    KDE-flavoured `HotkeyPurpose.desktop_id` -- dconf path segments are kept to
    lowercase letters and hyphens, which every purpose's `key` already is,
    rather than reusing an identifier shaped for a `.desktop` filename.
    """
    return f"{GNOME_CUSTOM_KEYBINDINGS_BASE}murmly-{slug}/"


def _quote_gvariant_string(value: str) -> str:
    return repr(value)


def _parse_gvariant_string_list(raw: str) -> list[str]:
    """Parse a `gsettings get` result for an `as` (array of string) key.

    An empty array prints with a type annotation because a bare ``[]`` is
    ambiguous GVariant text -- ``@as []`` -- which is stripped before the rest
    is read as the Python list-of-strings syntax GVariant's text format
    happens to share for plain strings. See
    ``docs/agent-notes/gnome-custom-keybindings.md``.
    """
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:]
    if not text:
        return []
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError) as error:
        raise DesktopQueryError(f"{GNOME_CUSTOM_KEYBINDINGS_KEY} returned an unreadable value: {text!r}") from error
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DesktopQueryError(f"{GNOME_CUSTOM_KEYBINDINGS_KEY} returned an unexpected shape: {text!r}")
    return value


def _format_gvariant_string_list(paths: list[str]) -> str:
    """The inverse of `_parse_gvariant_string_list`.

    Writing a bare ``[]`` for an empty list is a documented ambiguous case for
    `gsettings set`; the typed empty array must be written instead.
    """
    if not paths:
        return "@as []"
    return "[" + ", ".join(repr(path) for path in paths) + "]"


def _tolerant_accelerator_candidates(raw: str) -> list[str]:
    """Every string `raw` could hold, for a key this code does not own.

    `gsettings list-recursively` walks a whole fixed schema, most of whose keys
    are not accelerators at all -- ints, bools, enums, the `custom-keybindings`
    path list itself. Unlike `_parse_gvariant_string_list`, which raises on an
    unexpected shape because its caller already knows the key is `as`, this
    accepts either an array of strings (how every fixed-schema accelerator key
    is typed today) or a bare string (in case a future or older schema still
    uses the single-accelerator shape `custom-keybinding.binding` does), and
    answers "nothing to check" for anything else -- a value that is not a
    string is never mistaken for one, and `parse_gnome_accelerator` still gets
    the final say on whether a candidate is a real accelerator.
    """
    text = raw.strip()
    if text.startswith("@as "):
        text = text[4:]
    if not text:
        return []
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def _unquote_gvariant_string(raw: str) -> str:
    text = raw.strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    return value if isinstance(value, str) else text


class GnomeShortcuts:
    """Global shortcuts on GNOME's `custom-keybindings` mechanism.

    Plays both the role `PlasmaShortcuts` plays for KDE (a query surface other
    code asks about ownership) and the role `installer.ShortcutLauncher` plays
    (writing the binding) -- GNOME has no split between the two the way Plasma
    does, because `custom-keybindings` is a single live dconf value that is
    both. `GnomeShortcutLauncher` is the per-purpose object other code holds;
    this class is what it shares with its sibling purpose, and what it asks for
    conflict and verification queries.

    Unverified against a live GNOME session -- see
    ``docs/agent-notes/gnome-custom-keybindings.md``.
    """

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        gsettings: str = "gsettings",
        timeout: float = GNOME_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self._run_command = run_command
        self._gsettings = gsettings
        self._timeout = timeout
        # Populated by each `GnomeShortcutLauncher` sharing this instance, so
        # `owners_of` can label Murmly's own entries with the same
        # `desktop_id` that Installer's conflict logic already compares
        # against -- without this class hardcoding Murmly's own ids itself.
        self._known: dict[str, str] = {}

    def _note(self, desktop_id: str, path: str) -> None:
        self._known[path] = desktop_id

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [self._gsettings, *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DesktopQueryError(f"Unable to reach gsettings: {error}") from error

    def _schema_arg(self, path: str | None) -> str:
        return f"{GNOME_CUSTOM_KEYBINDING_SCHEMA}:{path}" if path else GNOME_MEDIA_KEYS_SCHEMA

    def _get_raw(self, key: str, path: str | None = None) -> str:
        result = self._run(["get", self._schema_arg(path), key])
        if result.returncode != 0:
            raise DesktopQueryError(
                f"gsettings get {key!r} failed: {(result.stderr or '').strip() or 'unknown error'}"
            )
        return (result.stdout or "").strip()

    def _set_raw(self, key: str, value: str, path: str | None = None) -> None:
        result = self._run(["set", self._schema_arg(path), key, value])
        if result.returncode != 0:
            raise DesktopQueryError(
                f"gsettings set {key!r} failed: {(result.stderr or '').strip() or 'unknown error'}"
            )

    def _reset_recursively(self, path: str) -> None:
        """Clear a purpose's `name`/`command`/`binding` after removal.

        Best effort: `custom-keybindings` no longer naming `path` is what makes
        the binding gone, so a failure here is logged rather than raised.
        """
        result = self._run(["reset-recursively", f"{GNOME_CUSTOM_KEYBINDING_SCHEMA}:{path}"])
        if result.returncode != 0:
            logger.debug(
                "gsettings reset-recursively for %s reported an error: %s",
                path,
                (result.stderr or "").strip(),
            )

    def _list_paths(self) -> list[str]:
        return _parse_gvariant_string_list(self._get_raw(GNOME_CUSTOM_KEYBINDINGS_KEY))

    def _write_paths(self, paths: list[str]) -> None:
        self._set_raw(GNOME_CUSTOM_KEYBINDINGS_KEY, _format_gvariant_string_list(paths))

    def read(self, path: str, key: str) -> str | None:
        """One key's value at `path`, or `None` if it could not be read.

        A query that could not be answered is not evidence a value is absent;
        callers that need to tell the two apart should call `_get_raw`
        directly rather than this convenience wrapper.
        """
        try:
            raw = self._get_raw(key, path=path)
        except DesktopQueryError:
            return None
        value = _unquote_gvariant_string(raw)
        return value or None

    def claim(self, desktop_id: str, path: str, name: str, command: str, accelerator: str) -> None:
        """Write one purpose's binding and read it back to confirm it took.

        Always a read-modify-write of `custom-keybindings`: appending `path`
        only if it is not already there, and never replacing the list with one
        built from scratch, so another application's own custom shortcut is
        never lost.
        """
        self._note(desktop_id, path)
        paths = self._list_paths()
        if path not in paths:
            self._write_paths([*paths, path])
        self._set_raw("name", _quote_gvariant_string(name), path=path)
        self._set_raw("command", _quote_gvariant_string(command), path=path)
        self._set_raw("binding", _quote_gvariant_string(accelerator), path=path)
        confirmed = self.read(path, "binding")
        if confirmed != accelerator:
            raise DesktopQueryError(
                f"GNOME did not confirm the binding {accelerator!r} at {path}; "
                f"it reports {confirmed!r}."
            )

    def release(self, desktop_id: str, path: str) -> bool:
        """Remove exactly `path` from `custom-keybindings`.

        Reports whether anything was there to remove. Filters `path` out of
        whatever the list currently holds rather than writing a list Murmly
        constructed itself, so entries other applications added are untouched.
        """
        self._note(desktop_id, path)
        paths = self._list_paths()
        if path not in paths:
            return False
        self._write_paths([entry for entry in paths if entry != path])
        self._reset_recursively(path)
        return True

    def owners_of(self, keycode: int) -> list[ShortcutOwner]:
        """Every registered owner of the physical key `keycode` names.

        Two scans: every entry in `custom-keybindings` -- covers other
        applications' and the user's own custom shortcuts, which live in the
        same schema -- and every key in `GNOME_FIXED_SCHEMAS`, GNOME's own
        built-in shortcuts (window management, the Shell, and the media-keys
        plugin's own volume/brightness/screenshot bindings). See
        ``docs/agent-notes/gnome-custom-keybindings.md``.

        A fixed schema that cannot be read raises rather than being treated as
        holding no collision: this is the query a refusal decision is based
        on, and reporting "clear" from a query that was never answered is
        exactly the silent double-bind this method exists to prevent.
        """
        target = decode_kde_keycode(keycode)
        owners: list[ShortcutOwner] = []
        for path in self._list_paths():
            binding = self.read(path, "binding")
            if not binding:
                continue
            spec = parse_gnome_accelerator(binding)
            if spec is None or spec != target:
                continue
            name = self.read(path, "name") or ""
            component_unique = self._known.get(path, path)
            owners.append(
                ShortcutOwner(
                    action_unique="_launch",
                    action_friendly=name,
                    component_unique=component_unique,
                    component_friendly=name or component_unique,
                )
            )
        owners.extend(self._fixed_owners_of(target))
        return owners

    def _fixed_owners_of(self, target: HotkeySpec) -> list[ShortcutOwner]:
        owners: list[ShortcutOwner] = []
        for schema in GNOME_FIXED_SCHEMAS:
            for key, raw_value in self._list_recursively(schema):
                for candidate in _tolerant_accelerator_candidates(raw_value):
                    spec = parse_gnome_accelerator(candidate)
                    if spec is None or spec != target:
                        continue
                    owners.append(
                        ShortcutOwner(
                            action_unique=key,
                            action_friendly=key,
                            component_unique=schema,
                            component_friendly=schema,
                        )
                    )
        return owners

    def _list_recursively(self, schema: str) -> list[tuple[str, str]]:
        """Every `(key, raw value)` pair `gsettings list-recursively` reports
        for `schema`, each line split on the first two spaces: schema id and
        key name are always plain tokens, and everything after them is the
        value, which may itself contain spaces (an array of accelerators).
        """
        result = self._run(["list-recursively", schema])
        if result.returncode != 0:
            raise DesktopQueryError(
                f"Unable to read {schema!r} for a hotkey collision: "
                f"{(result.stderr or '').strip() or 'unknown error'}. Refusing to treat "
                "an unreadable schema as free of one."
            )
        pairs: list[tuple[str, str]] = []
        for line in (result.stdout or "").splitlines():
            parts = line.split(" ", 2)
            if len(parts) != 3:
                continue
            _schema, key, raw_value = parts
            pairs.append((key, raw_value))
        return pairs

    def is_available(self, keycode: int) -> bool:
        return not self.owners_of(keycode)

    def registered_keys(self, component: str) -> list[int]:
        """The KDE-encoded keycode registered for `component`, if any.

        `component` is a `HotkeyPurpose.desktop_id`, the same identifier
        `PlasmaShortcuts.registered_keys` takes -- returned as a KDE key code
        even though GNOME never stores one, so `Installer._verify` can compare
        against `hotkey.keycode` unchanged regardless of which desktop's
        backend answered.
        """
        path = next((candidate for candidate, desktop_id in self._known.items() if desktop_id == component), None)
        if path is None:
            return []
        binding = self.read(path, "binding")
        if not binding:
            return []
        spec = parse_gnome_accelerator(binding)
        if spec is None:
            return []
        try:
            return [encode_for_kde(spec).keycode]
        except HotkeyError:
            # A binding using a modifier (Hyper) that has no KDE keycode --
            # not something this pipeline writes, but a foreign edit could.
            # Reported as "nothing Murmly's own currency can name" rather than
            # raising, since this is a read path.
            return []


class GnomeShortcutLauncher:
    """One purpose's binding, backed by a shared `GnomeShortcuts`.

    Implements the same duck-typed surface `installer.ShortcutLauncher` does
    (`purpose`, `declared_hotkey`, `declared_entrypoint`, `user_override`,
    `register`, `unregister`), so `Installer` drives either without knowing
    which desktop it is talking to.
    """

    def __init__(self, shortcuts: GnomeShortcuts, purpose) -> None:
        self._shortcuts = shortcuts
        self._purpose = purpose
        self._path = gnome_binding_path(purpose.key)
        shortcuts._note(purpose.desktop_id, self._path)

    @property
    def purpose(self):
        return self._purpose

    @property
    def path(self) -> str:
        return self._path

    def declared_hotkey(self) -> str | None:
        """The hotkey this entry declares, as KDE portable text.

        GNOME stores a GTK accelerator, not KDE's text; decoding it back to
        Murmly's canonical key spec and re-encoding for KDE is what lets every
        other reader of `declared_hotkey()` -- written for a single,
        KDE-flavoured `Hotkey.portable` currency -- work unchanged.
        """
        binding = self._shortcuts.read(self._path, "binding")
        if not binding:
            return None
        spec = parse_gnome_accelerator(binding)
        if spec is None:
            return None
        try:
            return encode_for_kde(spec).portable
        except HotkeyError:
            # A modifier (Hyper) KDE's currency cannot represent. Not
            # something this pipeline writes; reported as unreadable rather
            # than raising, since this is a read path.
            return None

    def declared_entrypoint(self) -> str | None:
        return self._shortcuts.read(self._path, "command")

    def user_override(self) -> str | None:
        """GNOME has no override layer distinct from what Murmly itself wrote.

        Plasma's `[services][id]` group is a separate value the desktop's own
        settings UI can set ahead of `X-KDE-Shortcuts`. GNOME's Keyboard
        Shortcuts panel, editing one of Murmly's own custom shortcuts, writes
        the very same `binding` key `declared_hotkey` already reads -- so
        there is no second value to report here.
        """
        return None

    def register(self, entrypoint, hotkey) -> None:
        accelerator = gnome_accelerator(hotkey.portable)
        command = f"{entrypoint} {self._purpose.command}"
        self._shortcuts.claim(
            self._purpose.desktop_id,
            self._path,
            name=self._purpose.name,
            command=command,
            accelerator=accelerator,
        )

    def unregister(self) -> bool:
        return self._shortcuts.release(self._purpose.desktop_id, self._path)
