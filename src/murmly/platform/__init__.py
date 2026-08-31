"""The platform layer: one resolution naming what Murmly is running on.

Six subsystems used to each detect their own environment independently --
`is_wayland_session`, `is_plasma_desktop`, `detect_overlay_backend`,
`detect_desktop_session`, `create_focus_observer`, and the paste-injector
probe -- and each had its own notion of what "unsupported" means. That is
why `murmly doctor` could only ever report `session` as `wayland` or `x11`:
there was nothing else for it to ask.

This module replaces the detecting, not the behaviour it feeds. A single
`PlatformProfile` is resolved once, from `sys.platform`, `platform.machine()`
and a supplied environment mapping, and every platform-dependent concern then
selects its own mechanism from that one value through its own small registry.
The registries stay separate per concern rather than merging onto one shared
axis, because the axes genuinely differ: a hotkey backend is chosen by desktop
on Linux and by operating system elsewhere, an overlay backend by display
protocol on Linux and by operating system elsewhere, and a transport by
operating system alone. See
`openspec/changes/all-os-distributions/design.md` for the full reasoning.

This module imports nothing else from `murmly`. Every subsystem that needs a
platform reading imports this one; the dependency never runs the other way,
which is what keeps `resolve_platform` usable from `desktop.py`, `overlay.py`,
`integrations.py`, `focus.py`, `installer.py` and `tts.py` without a cycle.
Where a registry's mechanism is one of those subsystems' own classes or data,
its loader imports that subsystem from inside a function body -- the same
deferred-import shape `installer._current_session` and
`stt._load_model_locked` already use -- so the cycle never opens in the first
place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import os
import platform as stdlib_platform
import sys


class OperatingSystem(StrEnum):
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"
    #: Anything `sys.platform` names that is none of the above -- a BSD, for
    #: instance. Murmly does not target it; it exists so `resolve_platform`
    #: always returns a value rather than raising on a machine it has never
    #: seen.
    OTHER = "other"


#: Phase 1 scope: the platform layer and its registries land with exactly the
#: backends that exist today, behind exactly the operating system that runs
#: them, and "no new operating system is claimed" -- see design.md's Migration
#: Plan. Windows joins in phase 2 and macOS in phase 3, each by adding its
#: value here once its backends exist, not by changing how this check works.
SUPPORTED_OPERATING_SYSTEMS: tuple[OperatingSystem, ...] = (OperatingSystem.LINUX,)


class Desktop(StrEnum):
    PLASMA = "plasma"
    GNOME = "gnome"
    #: Declared something Murmly does not recognise, or declared nothing at
    #: all -- the two are not distinguished, because no backend today treats
    #: them differently.
    OTHER = "other"


#: `platform.machine()` and Windows' `PROCESSOR_ARCHITECTURE` spell the same
#: physical architecture differently. Folded to one name per architecture here
#: so the runtime gap table below is written once, in one spelling, rather
#: than duplicated per alias at every place that reads `architecture`.
_ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "x86-64": "x86_64",
    "aarch64": "arm64",
    "aarch64_be": "arm64",
}


def operating_system_for(sys_platform: str) -> OperatingSystem:
    """The win32/darwin/linux/other mapping, as a pure function of the string.

    Kept separate from `resolve_platform` so this mapping is testable by
    passing strings directly, without patching `sys.platform` to exercise an
    operating system other than the one the test runner is on.
    """
    if sys_platform.startswith("linux"):
        return OperatingSystem.LINUX
    if sys_platform.startswith("win"):
        return OperatingSystem.WINDOWS
    if sys_platform == "darwin":
        return OperatingSystem.MACOS
    return OperatingSystem.OTHER


def _normalized_architecture(machine: str) -> str:
    folded = machine.strip().casefold()
    return _ARCHITECTURE_ALIASES.get(folded, folded)


#: Alpine and every other musl distribution install their dynamic linker at a
#: fixed, versioned path -- musl determines that name at build time and nothing
#: else installs a file there. Checked only as a fallback, because it is a
#: filesystem convention rather than something the C library reports about
#: itself.
_MUSL_LOADER_GLOBS = ("/lib/ld-musl-*.so.*", "/lib64/ld-musl-*.so.*", "/usr/lib/ld-musl-*.so.*")


def _musl_loader_present() -> bool:
    import glob

    return any(glob.glob(pattern) for pattern in _MUSL_LOADER_GLOBS)


def _detected_libc() -> str | None:
    """The C library this process is linked against, where that can be told.

    `platform.libc_ver()` identifies glibc positively -- confirmed on this
    machine, it returns `("glibc", "2.43")` -- by scanning the interpreter
    binary for a `GLIBC_<version>` symbol, and has gained a comparable
    `musl-<version>` signature by the same scan on newer interpreters
    (cpython gh-87414). Not every interpreter version this project supports
    carries that fix, though, and on one that does not, a musl machine reports
    `("", "")` there -- indistinguishable from "could not tell". So musl is
    confirmed a second way when the first says nothing: by the dynamic linker
    musl installs at a fixed, versioned path, which nothing else creates.
    Nothing here guesses -- an interpreter without the first signature and a
    filesystem layout not matching the second both fall through to `None`
    rather than being called "glibc" or "musl" on partial evidence.

    Checked in this order, not the reverse: a glibc machine that happens to
    have a musl cross-toolchain installed alongside it -- Fedora's `musl-libc`
    package puts `ld-musl-x86_64.so.1` under `/lib` on an ordinary glibc
    system -- would otherwise satisfy the filesystem check first and be
    misreported as musl. `libc_ver()` answers "glibc" for that machine before
    the filesystem is ever consulted.
    """
    name, _version = stdlib_platform.libc_ver()
    if name == "glibc":
        return "glibc"
    if name == "musl" or _musl_loader_present():
        return "musl"
    return None


def _desktop_for(current_desktop: str, session_desktop: str) -> Desktop:
    """Which desktop `XDG_CURRENT_DESKTOP`/`XDG_SESSION_DESKTOP` name.

    Plasma is checked before GNOME so a combined string naming both -- some
    distributions' session files do -- keeps `is_plasma_desktop`'s existing
    answer rather than flipping it.
    """
    combined = f"{current_desktop}:{session_desktop}".casefold()
    if any(name in combined for name in ("kde", "plasma")):
        return Desktop.PLASMA
    if "gnome" in combined:
        return Desktop.GNOME
    return Desktop.OTHER


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    """What Murmly is running on, resolved once and read everywhere.

    `session_type`, `wayland_display`, `x11_display` and `desktop` are Linux
    concepts and hold their uninformative defaults elsewhere. They are kept as
    raw observations rather than folded into one canonical "session" reading,
    because the functions built on them do not agree with each other on the
    same environment: given `XDG_SESSION_TYPE=x11` alongside a stale
    `WAYLAND_DISPLAY`, `is_wayland_session` says Wayland (display-variable
    presence wins there) while `detect_overlay_backend` says X11 (the declared
    session type wins there). One derived field cannot carry both answers, so
    both raw facts are carried instead and each reader keeps applying its own
    precedence, unchanged from before this module existed.
    """

    operating_system: OperatingSystem
    architecture: str
    libc: str | None = None
    session_type: str = ""
    wayland_display: bool = False
    x11_display: bool = False
    desktop: Desktop = Desktop.OTHER

    @property
    def supported(self) -> bool:
        return self.operating_system in SUPPORTED_OPERATING_SYSTEMS


def resolve_platform(env: Mapping[str, str] | None = None) -> PlatformProfile:
    """Resolve one profile from `sys.platform`, `platform.machine()` and `env`.

    Takes its environment as a parameter, exactly as `is_wayland_session(env=)`
    and `detect_desktop_session(env=)` already do, so a test can answer for a
    supplied environment without touching the process's own.

    Meant to be resolved once per process and passed on from there -- see
    `cli._dispatch`, which is the call site written for this change. The four
    detectors this module now backs keep resolving on every call, because
    their signatures are frozen so nothing calling them changes yet (task
    1.5); each of those calls is still answered from this one function, so two
    of them can never disagree about the same environment.
    """
    source = env if env is not None else os.environ
    operating_system = operating_system_for(sys.platform)
    return PlatformProfile(
        operating_system=operating_system,
        architecture=_normalized_architecture(stdlib_platform.machine()),
        # The C library is a Linux concept: Windows and macOS each ship their
        # own single runtime library, so there is no comparable "which libc"
        # question to answer there, and asking `_detected_libc` would only add
        # a way to misclassify a Windows or macOS machine as glibc-linked.
        libc=_detected_libc() if operating_system is OperatingSystem.LINUX else None,
        session_type=source.get("XDG_SESSION_TYPE", "").casefold(),
        wayland_display=bool(source.get("WAYLAND_DISPLAY")),
        x11_display=bool(source.get("DISPLAY")),
        desktop=_desktop_for(
            source.get("XDG_CURRENT_DESKTOP", ""), source.get("XDG_SESSION_DESKTOP", "")
        ),
    )


# --------------------------------------------------------------------------
# Per-concern backend registries (task 1.4, 1.6)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendCandidate:
    """One mechanism a registry can select, and the profile it applies to."""

    mechanism: str
    supports: Callable[[PlatformProfile], bool]
    #: Returns the concrete backend -- a class, a function, or the data a
    #: subsystem already defines -- imported from inside the callable's own
    #: body so this module never imports the subsystem at module load time.
    load: Callable[[], object]


@dataclass(frozen=True, slots=True)
class BackendChoice:
    """A registry's answer for one profile: the mechanism, or why none applies.

    Section 6 (diagnostics) is what will render `mechanism`, `reason` and
    `remedy` into `murmly doctor`'s `platform` section; this module only
    decides them.

    `remedy` is the data that carries the distinction the `platform-support`
    spec requires between a mechanism that does not exist on this platform and
    one that exists here but could not be used: empty means "the platform
    offers none" and the report must not name something to install, while a
    non-empty tuple names exactly what to install, enable, or grant to make the
    existing mechanism usable. A reader (or a future registry) decides which
    case it is by testing `remedy`, never by pattern-matching `reason`'s
    wording -- `reason` is prose for a person, `remedy` is the structured
    answer for code.
    """

    mechanism: str | None
    reason: str | None = None
    load: Callable[[], object] | None = None
    remedy: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.mechanism is not None


class BackendRegistry:
    """Chooses one mechanism for a concern from a resolved platform.

    Kept one registry per concern rather than merged onto one shared axis: a
    hotkey backend is chosen by desktop on Linux and by operating system
    elsewhere, an overlay backend by display protocol on Linux and by
    operating system elsewhere, a focus observer by display protocol on Linux
    and by operating system elsewhere, and a transport by operating system
    alone. A single registry could not express that without a class per every
    combination of platform and concern, which is the alternative design.md
    rejects.
    """

    def __init__(
        self,
        concern: str,
        candidates: Sequence[BackendCandidate],
        unavailable_reason: Callable[[PlatformProfile], str],
        #: Defaults to "nothing to install" -- true for every candidate list
        #: below except `OVERLAY`'s, where Plasma being present but undisplayed
        #: is the one case today where the mechanism exists and something can
        #: be named to fix it. Kept as its own callable, alongside
        #: `unavailable_reason` rather than folded into it, so a registry that
        #: has no such case (every other one today) need not return an unused
        #: half of a pair every time.
        unavailable_remedy: Callable[[PlatformProfile], tuple[str, ...]] = lambda profile: (),
    ) -> None:
        self.concern = concern
        self._candidates = tuple(candidates)
        self._unavailable_reason = unavailable_reason
        self._unavailable_remedy = unavailable_remedy

    def select(self, profile: PlatformProfile) -> BackendChoice:
        for candidate in self._candidates:
            if candidate.supports(profile):
                return BackendChoice(candidate.mechanism, load=candidate.load)
        return BackendChoice(
            None,
            reason=self._unavailable_reason(profile),
            remedy=self._unavailable_remedy(profile),
        )


def _is_linux(profile: PlatformProfile) -> bool:
    return profile.operating_system is OperatingSystem.LINUX


def _is_plasma(profile: PlatformProfile) -> bool:
    return _is_linux(profile) and profile.desktop is Desktop.PLASMA


def _is_gnome(profile: PlatformProfile) -> bool:
    return _is_linux(profile) and profile.desktop is Desktop.GNOME


def _wants_wayland(profile: PlatformProfile) -> bool:
    """`is_wayland_session`'s own precedence: display-variable presence wins."""
    return profile.wayland_display or profile.session_type == "wayland"


def _has_overlay_display(profile: PlatformProfile) -> bool:
    """Whether Plasma has a display to draw on -- `detect_overlay_backend`'s check."""
    if profile.session_type == "wayland":
        return profile.wayland_display
    if profile.session_type == "x11":
        return profile.x11_display
    if not profile.session_type:
        return profile.wayland_display or profile.x11_display
    return False


def _load_unix_socket_family() -> object:
    """The socket family the UNIX-socket command channel is built on.

    Read from the live `socket` module where it has the attribute at all --
    Linux and macOS both do, and that is the value actually used wherever this
    candidate is genuinely selected. It falls back to the POSIX address-family
    enumeration's own constant (`AF_UNIX` has been `1` there since long before
    either platform existed) only where the running interpreter's `socket`
    module has no such attribute -- Windows', which is exactly why task 18.1's
    exhaustive sweep can reach this loader while running there: that test
    constructs every (concern, operating system) pair `from any machine` and
    requires every candidate's `load()` to answer rather than raise, the same
    contract `_load_named_pipe_server` already meets on Linux by never
    touching a `pywin32` name outside the function that runs on Windows.
    """
    import socket

    return getattr(socket, "AF_UNIX", 1)


def _is_windows(profile: PlatformProfile) -> bool:
    return profile.operating_system is OperatingSystem.WINDOWS


def _load_named_pipe_server() -> object:
    from murmly.win_pipe import NamedPipeServer

    return NamedPipeServer


def _load_systemd_service() -> object:
    from murmly.installer import UserService

    return UserService


def _load_plasma_hotkeys() -> object:
    from murmly.desktop import PlasmaShortcuts

    return PlasmaShortcuts


def _load_gnome_hotkeys() -> object:
    from murmly.desktop import GnomeShortcuts

    return GnomeShortcuts


def _load_windows_hotkey_registrar() -> object:
    from murmly.win_hotkey import WindowsHotkeyRegistrar

    return WindowsHotkeyRegistrar


def _load_windows_service() -> object:
    from murmly.installer import WindowsUserService

    return WindowsUserService


def _load_wayland_clipboard() -> object:
    from murmly.integrations import choose_clipboard_copy_command

    return choose_clipboard_copy_command


def _load_x11_clipboard() -> object:
    from murmly.integrations import choose_clipboard_copy_command

    return choose_clipboard_copy_command


def _load_wayland_injectors() -> object:
    from murmly.integrations import WAYLAND_INJECTORS

    return WAYLAND_INJECTORS


def _load_x11_injectors() -> object:
    from murmly.integrations import X11_INJECTORS

    return X11_INJECTORS


def _load_x11_focus_observer() -> object:
    from murmly.focus import X11FocusObserver

    return X11FocusObserver


def _load_windows_focus_observer() -> object:
    from murmly.win_focus import WindowsFocusObserver

    return WindowsFocusObserver


def _load_windows_clipboard() -> object:
    from murmly.win_clipboard import WindowsClipboardPaster

    return WindowsClipboardPaster


def _load_windows_injector() -> object:
    from murmly.win_clipboard import SEND_INPUT_METHOD

    return SEND_INPUT_METHOD


def _load_gtk4_overlay() -> object:
    from murmly.overlay import OverlayController

    return OverlayController


def _load_kokoro_synthesis() -> object:
    from murmly.tts import KokoroSynthesizer

    return KokoroSynthesizer


def _no_backend_for_operating_system(concern: str) -> Callable[[PlatformProfile], str]:
    return lambda profile: f"No {concern} backend exists for {profile.operating_system.value} yet."


COMMAND_CHANNEL = BackendRegistry(
    "command channel",
    candidates=(
        BackendCandidate("unix-socket", _is_linux, _load_unix_socket_family),
        # CPython exposes no `AF_UNIX` on Windows (design.md's "The command
        # channel"): the named pipe is Windows' own channel, built by
        # `win_pipe.py` with a security descriptor whose DACL grants only the
        # creating user's SID (task 7.2), never `multiprocessing.connection`,
        # whose Windows pipes carry the OS default DACL instead.
        BackendCandidate("named-pipe", _is_windows, _load_named_pipe_server),
    ),
    unavailable_reason=_no_backend_for_operating_system("command channel"),
)

SERVICE_MANAGEMENT = BackendRegistry(
    "service management",
    candidates=(
        BackendCandidate("systemd", _is_linux, _load_systemd_service),
        # Task Scheduler over the Startup folder or `HKCU\...\Run`: it is the
        # only one of the three with CLI verbs for start, stop, status,
        # enable and disable (design.md's "Service management" table), which
        # `installer.WindowsUserService.is_active()`/`status()` need to
        # answer the same questions `UserService`'s systemd equivalents do.
        BackendCandidate("task-scheduler", _is_windows, _load_windows_service),
    ),
    unavailable_reason=_no_backend_for_operating_system("service management"),
)


def _hotkey_unavailable_reason(profile: PlatformProfile) -> str:
    # Windows always matches the `windows-hotkey` candidate below, so this
    # branch is unreached for it; kept as a plain Linux/Windows split rather
    # than three branches, since only macOS and "other" still fall through to
    # "no backend at all" here.
    if not _is_linux(profile):
        return f"No hotkey backend exists for {profile.operating_system.value} yet."
    return "Hotkey registration requires KDE Plasma or GNOME."


HOTKEY_REGISTRATION = BackendRegistry(
    "hotkey registration",
    candidates=(
        BackendCandidate("plasma", _is_plasma, _load_plasma_hotkeys),
        BackendCandidate("gnome", _is_gnome, _load_gnome_hotkeys),
        # Registers in Murmly's own process rather than in any desktop-held
        # state (design.md's "Four hotkey backends"): see
        # `IN_PROCESS_HOTKEY_MECHANISMS` below, and `win_hotkey.py`.
        BackendCandidate("windows-hotkey", _is_windows, _load_windows_hotkey_registrar),
    ),
    unavailable_reason=_hotkey_unavailable_reason,
)

CLIPBOARD = BackendRegistry(
    "clipboard",
    candidates=(
        BackendCandidate(
            "wayland", lambda profile: _is_linux(profile) and _wants_wayland(profile), _load_wayland_clipboard
        ),
        BackendCandidate("x11", _is_linux, _load_x11_clipboard),
        # `CF_UNICODETEXT` through the Win32 API, never `clip.exe` (task 9.1,
        # design.md's "Clipboard and paste injection") -- `clip.exe` reads
        # stdin through the console's own OEM/ANSI codepage and mangles
        # anything outside it.
        BackendCandidate("windows", _is_windows, _load_windows_clipboard),
    ),
    unavailable_reason=_no_backend_for_operating_system("clipboard"),
)

PASTE_INJECTION = BackendRegistry(
    "paste injection",
    candidates=(
        BackendCandidate(
            "wayland", lambda profile: _is_linux(profile) and _wants_wayland(profile), _load_wayland_injectors
        ),
        BackendCandidate("x11", _is_linux, _load_x11_injectors),
        # `SendInput` (task 9.2), registered as unconfirmable (task 9.3): UIPI
        # silently discards synthetic input aimed at a higher-integrity
        # window, so `win_clipboard.WindowsClipboardPaster` never restores the
        # clipboard over what it just wrote -- see that module's docstring.
        BackendCandidate("windows", _is_windows, _load_windows_injector),
    ),
    unavailable_reason=_no_backend_for_operating_system("paste injection"),
)


def _focus_unavailable_reason(profile: PlatformProfile) -> str:
    # Windows always matches the `windows` candidate below, so this branch is
    # unreached for it, the same shape `_hotkey_unavailable_reason` already
    # has for the same reason.
    if not _is_linux(profile):
        return f"No focus observation backend exists for {profile.operating_system.value} yet."
    return "Delivery target verification requires an X11 session."


FOCUS_OBSERVATION = BackendRegistry(
    "focus observation",
    candidates=(
        BackendCandidate(
            "x11", lambda profile: _is_linux(profile) and not _wants_wayland(profile), _load_x11_focus_observer
        ),
        # `GetForegroundWindow`/`GetWindowThreadProcessId`/`QueryFullProcessImageName`
        # (task 9.4) -- none needs a permission for a process the same user
        # owns, which is exactly why design.md picks them.
        BackendCandidate("windows", _is_windows, _load_windows_focus_observer),
    ),
    unavailable_reason=_focus_unavailable_reason,
)


def _overlay_unavailable_reason(profile: PlatformProfile) -> str:
    if not _is_linux(profile):
        return f"No overlay backend exists for {profile.operating_system.value} yet."
    if not _is_plasma(profile):
        return "Overlay requires KDE Plasma."
    # Plasma is confirmed present here -- the one thing this session lacks is a
    # usable display. Saying "requires KDE Plasma" in this branch, as the
    # desktop-only check above does, would tell a person to go get the one
    # thing they already have and hide the actual, different reason. That
    # reason is not always "neither variable is set": `_has_overlay_display`
    # also refuses a declared session type without its matching display
    # variable (Wayland without `WAYLAND_DISPLAY`, X11 without `DISPLAY`) and a
    # session type it does not recognise at all, such as `tty` from an SSH
    # login carrying a forwarded `DISPLAY` -- so the reason names the
    # requirement rather than asserting which variable this one session left
    # unset.
    return "Overlay requires an X11 or Wayland session with its display available."


def _overlay_unavailable_remedy(profile: PlatformProfile) -> tuple[str, ...]:
    """What to name for the overlay's one exists-but-could-not-be-used case.

    Plasma present without a usable display is the only branch of this
    registry's refusal where the mechanism exists on this platform -- every
    other branch (not Linux, not Plasma) means no overlay backend has been
    built for that desktop or operating system at all, which the spec's "does
    not exist" scenario says must not name something to install. Reusing
    `_is_plasma` here, the same predicate `_overlay_unavailable_reason` already
    branches on, keeps the two callables from disagreeing about which case
    this is.
    """
    if _is_plasma(profile):
        return ("Start an X11 or Wayland session with its display available.",)
    return ()


OVERLAY = BackendRegistry(
    "overlay",
    candidates=(
        BackendCandidate(
            "gtk4", lambda profile: _is_plasma(profile) and _has_overlay_display(profile), _load_gtk4_overlay
        ),
    ),
    unavailable_reason=_overlay_unavailable_reason,
    unavailable_remedy=_overlay_unavailable_remedy,
)

SPEECH_SYNTHESIS = BackendRegistry(
    "speech synthesis",
    candidates=(BackendCandidate("kokoro", _is_linux, _load_kokoro_synthesis),),
    unavailable_reason=_no_backend_for_operating_system("speech synthesis"),
)

#: Hotkey mechanisms that register inside Murmly's own process rather than in
#: the desktop's own persisted state. Neither `plasma` (a launcher file the
#: desktop discovers) nor `gnome` (a dconf value the settings daemon watches)
#: is one -- both are desktop-held state a fresh session already has without
#: Murmly doing anything. `windows-hotkey` (section 8) is the first member;
#: macOS's Carbon `RegisterEventHotKey` (section 13) is the intended second,
#: once its candidate exists. See `hotkey_record.py` for what reads this, and
#: `win_hotkey.WindowsHotkeyRegistrar` for what satisfies its `rebind`
#: contract on this mechanism.
IN_PROCESS_HOTKEY_MECHANISMS: frozenset[str] = frozenset({"windows-hotkey"})


def hotkey_mechanism_is_in_process(
    profile: PlatformProfile,
    registry: "BackendRegistry | None" = None,
    in_process: frozenset[str] | None = None,
) -> bool:
    """Whether `profile`'s hotkey mechanism needs `hotkey_record.py`'s
    rebind-at-startup path, rather than the desktop's own persisted state.

    `registry` and `in_process` default to the real registry and the real
    table; both take a parameter for the same reason `runtime_gaps_for` does:
    a test exercises the "in-process" branch against a constructed registry
    with a fake candidate, without a real in-process backend existing yet.
    """
    active_registry = registry if registry is not None else HOTKEY_REGISTRATION
    active_in_process = in_process if in_process is not None else IN_PROCESS_HOTKEY_MECHANISMS
    return active_registry.select(profile).mechanism in active_in_process


#: Every registry, keyed by the concern name used throughout the tasks and
#: (later) the `platform` diagnostics section. Iterating this is how a test or
#: a future report visits "every platform-dependent concern" without naming
#: each registry twice.
BACKEND_REGISTRIES: Mapping[str, BackendRegistry] = {
    "command_channel": COMMAND_CHANNEL,
    "service_management": SERVICE_MANAGEMENT,
    "hotkey_registration": HOTKEY_REGISTRATION,
    "clipboard": CLIPBOARD,
    "paste_injection": PASTE_INJECTION,
    "focus_observation": FOCUS_OBSERVATION,
    "overlay": OVERLAY,
    "speech_synthesis": SPEECH_SYNTHESIS,
}


# --------------------------------------------------------------------------
# Permissions a platform gates a capability behind (task 6.4)
# --------------------------------------------------------------------------


class PermissionState(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    #: What a `Permission.check` answers where the platform offers no way to
    #: read whether the grant was given, and what `platform_diagnostics`
    #: (`cli.py`) substitutes when `check` itself raises. Never rendered as
    #: `GRANTED`: a check that cannot tell is not evidence a capability works,
    #: and the `platform-support` spec forbids reporting one on the strength of
    #: the mechanism alone when its permission state is unknown.
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class Permission:
    """One grant a person must give before a platform lets a mechanism work.

    This type and `PERMISSIONS` exist so Windows' microphone privacy setting
    (section 9, the first member) and macOS's microphone, Accessibility, and
    Input Monitoring grants (sections 12, 14) have a shape to register into
    rather than each inventing its own, the same role `IN_PROCESS_HOTKEY_MECHANISMS`
    plays for hotkeys.

    `capability` names what the permission gates (e.g. "paste injection"), not
    the permission's own name, because that is what a denied-permission report
    must say was lost. `grant_location` is where a person goes to change it --
    a System Settings pane, a Group Policy path -- named specifically enough to
    act on, the same bar `BackendChoice.reason` is held to.

    `applies` is what keeps `PERMISSIONS` one flat table across every platform
    without a Linux report growing a `permissions` entry for a grant Linux does
    not gate anything behind: `platform_diagnostics` (`cli.py`) filters this
    mapping through it before rendering, the same role `BackendCandidate.supports`
    plays for a registry candidate. Defaults to "applies everywhere" only
    because nothing yet needs a permission every platform shares; every real
    entry today names the one operating system that gates it.
    """

    name: str
    capability: str
    grant_location: str
    check: Callable[[PlatformProfile], PermissionState]
    applies: Callable[[PlatformProfile], bool] = lambda profile: True


#: The two per-user consent-store paths that can each independently deny
#: microphone capture to an unpackaged desktop executable like Murmly: the
#: master "Microphone access" toggle at the parent key, and the "Allow
#: desktop apps to access your microphone" toggle at its `NonPackaged`
#: subkey. Checking only the second is the bug a person who flipped the
#: *master* toggle off would fall through -- read as no denial there, and
#: reported `GRANTED`, which is exactly what the `platform-support` spec
#: forbids. Read from Microsoft's own documentation of the consent store's
#: shape, not confirmed on a Windows machine; see
#: `_windows_microphone_permission_check`.
_WINDOWS_MICROPHONE_CONSENT_KEYS = (
    r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone",
    r"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone\NonPackaged",
)


def _windows_microphone_consent_values(
    read_registry_value: Callable[[str, str], str | None],
) -> tuple[str | None, ...]:
    """Each consent-store key's raw readable value, in `_WINDOWS_MICROPHONE_CONSENT_KEYS` order.

    Read once into a tuple rather than answered as a single yes/no, so the
    caller can tell "every key read, none said deny" (a positive `GRANTED`)
    apart from "at least one key could not be read at all" (`UNDETERMINED`,
    per the `platform-support` spec's "does not claim it is granted" rule) --
    a boolean collapses exactly that distinction.
    """
    return tuple(read_registry_value(key_path, "Value") for key_path in _WINDOWS_MICROPHONE_CONSENT_KEYS)


def _real_read_registry_value(key_path: str, value_name: str) -> str | None:
    """`winreg.QueryValueEx`, or `None` for any reason the value cannot be read.

    Every failure mode -- the key does not exist, the value does not exist,
    access is refused -- collapses to `None` rather than being told apart,
    because `_windows_microphone_permission_check` already treats any
    unreadable key as `UNDETERMINED`, never as `GRANTED`: it has no case that
    would answer differently for one reason over another.
    """
    import winreg

    hive_name, _, subkey = key_path.partition("\\")
    hive = getattr(winreg, hive_name)
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _type = winreg.QueryValueEx(key, value_name)
            return str(value)
    except OSError:
        return None


def _windows_microphone_permission_check(
    profile: PlatformProfile,
    read_registry_value: Callable[[str, str], str | None] = _real_read_registry_value,
) -> PermissionState:
    """Task 9.5: the microphone privacy setting's state, where Windows lets it be read.

    A readable "Deny" at either key is `DENIED` regardless of whether the
    other key could be read -- there is no case where one says "deny" and the
    other's "allow" overrides it. Short of that, any key that could not be
    read at all (absent on a Windows version that predates it, or access
    refused) answers `UNDETERMINED`, never `GRANTED`: an unreadable
    `NonPackaged` key might be hiding a denial this check cannot see, and the
    `platform-support` spec forbids claiming a grant on that silence. Only
    when both keys were read, and neither said "deny", is this `GRANTED`.
    This alone is what lets `murmly doctor` tell a blocked microphone apart
    from an absent device (task 9.5): a denied permission shows up here, in
    its own field, while an absent device shows up wherever capture itself
    fails to open one -- the two are never the same report line, so nothing
    here needs to enumerate audio devices to keep them distinct.
    """
    values = _windows_microphone_consent_values(read_registry_value)
    if any(value is not None and value.casefold() == "deny" for value in values):
        return PermissionState.DENIED
    if any(value is None for value in values):
        return PermissionState.UNDETERMINED
    # Read from Microsoft's documented consent-store shape, not confirmed
    # against a real Windows registry: an "Allow" value read from both keys
    # is granted here. If a live Windows machine shows a case this collapses
    # wrongly, this is the function to correct.
    return PermissionState.GRANTED


WINDOWS_MICROPHONE_PERMISSION = "windows-microphone"

#: Every permission Murmly currently knows to ask about, keyed by `name`.
#: `platform_diagnostics` (`cli.py`) renders this mapping into the `platform`
#: section's `permissions` field regardless of whether it holds anything, so
#: that field's shape does not change the day a permission is added, filtered
#: through each entry's `applies` first so a platform this permission does not
#: gate anything on never grows an entry for it.
PERMISSIONS: Mapping[str, Permission] = {
    WINDOWS_MICROPHONE_PERMISSION: Permission(
        name=WINDOWS_MICROPHONE_PERMISSION,
        capability="microphone capture",
        grant_location="Settings > Privacy & security > Microphone",
        check=_windows_microphone_permission_check,
        applies=_is_windows,
    ),
}


# --------------------------------------------------------------------------
# The machine-capability check (task 1.8)
# --------------------------------------------------------------------------


#: Transcription is what Murmly is: a gap naming this capability refuses the
#: daemon outright rather than degrading it. See `transcription_runtime_gap`.
TRANSCRIPTION_CAPABILITY = "transcription"


@dataclass(frozen=True, slots=True)
class RuntimeGap:
    """One combination of operating system, processor, or C library with no
    build of a runtime Murmly needs.

    `matches` decides membership from a resolved profile so the table in
    design.md's "CUDA loading, and what has no build where" has exactly one
    encoding, rather than the same three conditions re-derived at every place
    that would otherwise ask "can this machine transcribe".
    """

    runtime: str
    characteristic: str
    capability: str
    matches: Callable[[PlatformProfile], bool]


#: The table in design.md under "CUDA loading, and what has no build where".
#: All three gaps take out transcription: `ctranslate2` is missing outright
#: for musl and for Windows on ARM64, and Intel macOS is missing
#: `onnxruntime`, which `faster-whisper` needs for voice activity detection --
#: not only speech output's own use of it -- so that gap is transcription too.
RUNTIME_GAPS: tuple[RuntimeGap, ...] = (
    RuntimeGap(
        runtime="ctranslate2",
        characteristic="a musl C library",
        capability=TRANSCRIPTION_CAPABILITY,
        matches=lambda profile: profile.libc == "musl",
    ),
    RuntimeGap(
        runtime="ctranslate2",
        characteristic="Windows on the ARM64 architecture",
        capability=TRANSCRIPTION_CAPABILITY,
        matches=lambda profile: (
            profile.operating_system is OperatingSystem.WINDOWS and profile.architecture == "arm64"
        ),
    ),
    RuntimeGap(
        runtime="onnxruntime",
        characteristic="Intel macOS -- faster-whisper needs onnxruntime too, not only speech output",
        capability=TRANSCRIPTION_CAPABILITY,
        matches=lambda profile: (
            profile.operating_system is OperatingSystem.MACOS and profile.architecture == "x86_64"
        ),
    ),
)


def runtime_gaps_for(
    profile: PlatformProfile,
    gaps: Sequence[RuntimeGap] = RUNTIME_GAPS,
) -> tuple[RuntimeGap, ...]:
    """Every gap in `gaps` that applies to `profile`.

    `gaps` defaults to the real table and takes a parameter so a test can
    exercise the "a capability outside the core is unavailable" branch with a
    gap the real table has no example of, without inventing a machine that
    does not exist.
    """
    return tuple(gap for gap in gaps if gap.matches(profile))


def transcription_runtime_gap(
    profile: PlatformProfile,
    gaps: Sequence[RuntimeGap] = RUNTIME_GAPS,
) -> RuntimeGap | None:
    """The gap that takes out transcription for `profile`, or None.

    Singled out because the daemon's response to it differs from every other
    gap: transcription is what Murmly is, so this one refuses to start rather
    than starting and reporting the capability unavailable.
    """
    for gap in runtime_gaps_for(profile, gaps):
        if gap.capability == TRANSCRIPTION_CAPABILITY:
            return gap
    return None
