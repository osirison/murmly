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

    Section 6 (diagnostics) is what will render `mechanism` and `reason` into
    `murmly doctor`'s `platform` section; this module only decides them.
    """

    mechanism: str | None
    reason: str | None = None
    load: Callable[[], object] | None = None

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
    ) -> None:
        self.concern = concern
        self._candidates = tuple(candidates)
        self._unavailable_reason = unavailable_reason

    def select(self, profile: PlatformProfile) -> BackendChoice:
        for candidate in self._candidates:
            if candidate.supports(profile):
                return BackendChoice(candidate.mechanism, load=candidate.load)
        return BackendChoice(None, reason=self._unavailable_reason(profile))


def _is_linux(profile: PlatformProfile) -> bool:
    return profile.operating_system is OperatingSystem.LINUX


def _is_plasma(profile: PlatformProfile) -> bool:
    return _is_linux(profile) and profile.desktop is Desktop.PLASMA


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
    import socket

    return socket.AF_UNIX


def _load_systemd_service() -> object:
    from murmly.installer import UserService

    return UserService


def _load_plasma_hotkeys() -> object:
    from murmly.desktop import PlasmaShortcuts

    return PlasmaShortcuts


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
    candidates=(BackendCandidate("unix-socket", _is_linux, _load_unix_socket_family),),
    unavailable_reason=_no_backend_for_operating_system("command channel"),
)

SERVICE_MANAGEMENT = BackendRegistry(
    "service management",
    candidates=(BackendCandidate("systemd", _is_linux, _load_systemd_service),),
    unavailable_reason=_no_backend_for_operating_system("service management"),
)


def _hotkey_unavailable_reason(profile: PlatformProfile) -> str:
    if not _is_linux(profile):
        return f"No hotkey backend exists for {profile.operating_system.value} yet."
    return "Hotkey registration requires KDE Plasma."


HOTKEY_REGISTRATION = BackendRegistry(
    "hotkey registration",
    candidates=(BackendCandidate("plasma", _is_plasma, _load_plasma_hotkeys),),
    unavailable_reason=_hotkey_unavailable_reason,
)

CLIPBOARD = BackendRegistry(
    "clipboard",
    candidates=(
        BackendCandidate(
            "wayland", lambda profile: _is_linux(profile) and _wants_wayland(profile), _load_wayland_clipboard
        ),
        BackendCandidate("x11", _is_linux, _load_x11_clipboard),
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
    ),
    unavailable_reason=_no_backend_for_operating_system("paste injection"),
)


def _focus_unavailable_reason(profile: PlatformProfile) -> str:
    if not _is_linux(profile):
        return f"No focus observation backend exists for {profile.operating_system.value} yet."
    return "Delivery target verification requires an X11 session."


FOCUS_OBSERVATION = BackendRegistry(
    "focus observation",
    candidates=(
        BackendCandidate(
            "x11", lambda profile: _is_linux(profile) and not _wants_wayland(profile), _load_x11_focus_observer
        ),
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


OVERLAY = BackendRegistry(
    "overlay",
    candidates=(
        BackendCandidate(
            "gtk4", lambda profile: _is_plasma(profile) and _has_overlay_display(profile), _load_gtk4_overlay
        ),
    ),
    unavailable_reason=_overlay_unavailable_reason,
)

SPEECH_SYNTHESIS = BackendRegistry(
    "speech synthesis",
    candidates=(BackendCandidate("kokoro", _is_linux, _load_kokoro_synthesis),),
    unavailable_reason=_no_backend_for_operating_system("speech synthesis"),
)

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
