from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
from shutil import which as shutil_which
import subprocess
import time
from typing import Protocol

from murmly.platform import OperatingSystem, PlatformProfile, resolve_platform


logger = logging.getLogger(__name__)


Which = Callable[[str], str | None]
Run = Callable[..., subprocess.CompletedProcess]

PROBE_TIMEOUT_SECONDS = 2.0
YDOTOOL_SOCKET_NAME = ".ydotool_socket"


class MissingToolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InjectorCandidate:
    """One way of injecting a paste, and a no-op invocation that proves it works.

    The probe exists because a tool being installed says nothing about whether the
    session can run it: `wtype` needs the compositor to offer the virtual keyboard
    protocol, and `ydotool` needs a daemon this user can reach.

    `requires` names an environment variable the method cannot work without, and
    `confirms_delivery` is false for a method that reports success whether or not the
    keystroke reached the focused window.
    """

    method: str
    command: tuple[str, ...]
    probe: tuple[str, ...] | None = None
    requires: str | None = None
    confirms_delivery: bool = True


@dataclass(frozen=True, slots=True)
class PasteInjection:
    """Whether a paste can be injected in this session, and what to do when it cannot."""

    method: str | None
    command: tuple[str, ...] | None
    reason: str | None = None
    remedy: tuple[str, ...] = ()
    confirms_delivery: bool = True
    advisory: str | None = None

    @property
    def available(self) -> bool:
        return self.command is not None


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    injected: bool
    reason: str | None = None


class Paster(Protocol):
    """What `daemon.SpeechSession` and `cli._run_spike` need from a paster,
    satisfied structurally by `ClipboardPaster` (Linux) and
    `win_clipboard.WindowsClipboardPaster` (Windows) without either
    subclassing this -- the same shape `focus.FocusObserver` gives the two
    focus-observer implementations. `create_clipboard_paster` is what chooses
    between them.
    """

    @property
    def injection(self) -> PasteInjection: ...

    def copy(self, text: str) -> None: ...

    def copy_and_paste(self, text: str) -> DeliveryOutcome: ...


WAYLAND_INJECTORS = (
    # wtype first: on a compositor that offers the virtual keyboard protocol it is
    # the native route and needs nothing else. Then xdotool, which reaches Wayland
    # windows on compositors that bridge XTEST into their own input handling - KWin
    # does, through the EIS socket it hands XWayland. ydotool last: it works
    # anywhere but wants a daemon with access to /dev/uinput.
    InjectorCandidate("wtype", ("wtype", "-M", "ctrl", "v", "-m", "ctrl"), ("wtype", "")),
    InjectorCandidate(
        "xdotool",
        ("xdotool", "key", "--clearmodifiers", "ctrl+v"),
        # Opens an X connection without issuing an XTEST request, so probing cannot
        # trip the compositor's input-control consent prompt as a side effect.
        ("xdotool", "getdisplaygeometry"),
        requires="DISPLAY",
        # XWayland falls back to plain XTEST when the compositor refuses the EIS
        # connection, and xdotool exits 0 either way, so success here is not proof
        # the keystroke reached a Wayland-native window.
        confirms_delivery=False,
    ),
    InjectorCandidate("ydotool", ("ydotool", "key", "29:1", "47:1", "47:0", "29:0"), ("ydotool", "type", "")),
)
X11_INJECTORS = (InjectorCandidate("xdotool", ("xdotool", "key", "--clearmodifiers", "ctrl+v")),)


def is_wayland_session(env: dict[str, str] | None = None) -> bool:
    environment = env or os.environ
    profile = resolve_platform(environment)
    return profile.wayland_display or profile.session_type == "wayland"


def choose_clipboard_copy_command(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
) -> list[str]:
    if is_wayland_session(env):
        if which("wl-copy"):
            return ["wl-copy"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard"]
    elif which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if is_wayland_session(env):
        raise MissingToolError("No Wayland clipboard command found; install wl-clipboard or xclip.")
    raise MissingToolError("No X11 clipboard command found; install xclip.")


def choose_clipboard_read_command(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
) -> list[str] | None:
    if is_wayland_session(env):
        if which("wl-paste"):
            return ["wl-paste", "--no-newline"]
        if which("xclip"):
            return ["xclip", "-selection", "clipboard", "-o"]
    elif which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"]
    return None


def ydotool_socket_path(env: dict[str, str] | None = None) -> str:
    """Where the `ydotool` client will look for its daemon, resolved as it resolves it."""
    environment = env or os.environ
    configured = environment.get("YDOTOOL_SOCKET")
    if configured:
        return configured
    return f"{environment.get('XDG_RUNTIME_DIR') or '/tmp'}/{YDOTOOL_SOCKET_NAME}"


def injector_remedy(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """What the user has to do themselves. Murmly changes no system state.

    Ordered by cost to the user, not by elegance: a compositor that bridges XTEST
    into its own input handling - KWin does - needs nothing but xdotool and one
    click, so that goes first and the root-owned uinput daemon goes last.
    """
    environment = env or os.environ
    if not is_wayland_session(environment):
        return ("sudo dnf install xdotool",)
    return (
        "sudo dnf install xdotool    # KDE Plasma: KWin passes it through to Wayland windows;",
        "                            # the first paste asks once to allow input control, and",
        "                            # ticking 'Always allow' on that dialog makes it permanent",
        "sudo dnf install wtype      # wlroots compositors (Sway, river, Hyprland)",
        "                            # Neither works? ydotool needs one-time root setup:",
        "                            # see the Wayland paste section in Murmly's README",
    )


def input_consent_advisory(method: str, env: dict[str, str] | None = None) -> str | None:
    """Warn when KDE will gate this method behind its input-control dialog.

    KWin re-asks each time XWayland opens a fresh EIS connection, which it does
    again after roughly ten minutes idle. The paste that raises the dialog is lost -
    the tool has exited by the time anyone clicks - so it reaches the clipboard and
    no further. Ticking "Always allow" writes the app into kwinrc and ends it.
    """
    environment = env or os.environ
    if method != "xdotool" or not is_wayland_session(environment):
        return None
    config_home = environment.get("XDG_CONFIG_HOME") or f"{environment.get('HOME', '')}/.config"
    kwinrc = Path(config_home) / "kwinrc"
    try:
        settings = kwinrc.read_text(encoding="utf-8")
    except OSError:
        # No kwinrc: not a KWin session, so this dialog is not in the way.
        return None
    for line in settings.splitlines():
        if not line.startswith("XwaylandEisNoPromptApps"):
            continue
        granted = [entry.strip() for entry in line.partition("=")[2].split(",")]
        if method in granted:
            return None
        break
    return (
        f"KDE asks once per connection before letting {method} control input, and the "
        "paste that raises that dialog reaches the clipboard only. Tick "
        f"\"Always allow apps claiming to be {method}\" on it to stop it recurring."
    )


def probe_injector(
    candidate: InjectorCandidate,
    env: dict[str, str] | None = None,
    run: Run = subprocess.run,
) -> str | None:
    """Run the tool's own no-op invocation. Returns None when it can inject here."""
    if candidate.probe is None:
        return None
    try:
        result = run(
            list(candidate.probe),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=env or os.environ,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return str(error)
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return detail[0] if detail else f"exited with status {result.returncode}"


def select_paste_injection(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
    run: Run = subprocess.run,
    excluded: Iterable[str] = (),
    profile: PlatformProfile | None = None,
    macos_accessibility_trusted: Callable[[], bool] | None = None,
) -> PasteInjection:
    """Pick an injection method this session can actually execute.

    A tool that is installed but cannot run here is never selected: preferring it
    on presence alone is what leaves a transcript undelivered with nothing said.

    `profile` defaults to resolving from `env` (real `sys.platform`, exactly as
    `is_wayland_session` does), and takes an explicit value only so a test can
    supply a `PlatformProfile` naming an operating system this machine is not
    running on -- `env` alone cannot do that, since it carries session and
    desktop variables, never the operating system itself.

    Windows has exactly one method, `SendInput`, which `win_clipboard.py`
    documents as always present and never confirming delivery (task 9.2, 9.3):
    there is nothing to detect, so this returns immediately rather than
    running the Wayland/X11 candidate lists' installed-and-probed machinery
    against a platform those tools do not exist on.

    macOS also has exactly one method, `CGEventPost`, but unlike Windows'
    `SendInput` it is gated behind a real, checkable permission -- the
    Accessibility grant `AXIsProcessTrusted()` reports (task 14.4). Task 18.12
    and the `transcript-delivery` spec's "a method the platform gates behind a
    permission MUST NOT be reported as available while that permission is
    ungranted" is what `macos_accessibility_trusted` exists to make testable
    from Linux: it defaults to the real `AXIsProcessTrusted()` call, and a
    test supplies a fake instead, exactly as `_windows_microphone_permission_
    check`'s own `read_registry_value` parameter does for a different
    platform's permission.
    """
    environment = env or os.environ
    resolved = profile if profile is not None else resolve_platform(environment)
    if resolved.operating_system is OperatingSystem.WINDOWS:
        from murmly.win_clipboard import SEND_INPUT_METHOD

        if SEND_INPUT_METHOD in set(excluded):
            return PasteInjection(
                None,
                None,
                reason=f"{SEND_INPUT_METHOD} failed to inject a paste earlier in this session",
            )
        return PasteInjection(SEND_INPUT_METHOD, (), confirms_delivery=False)
    if resolved.operating_system is OperatingSystem.MACOS:
        from murmly.mac_clipboard import CGEVENT_POST_METHOD, _real_is_process_trusted
        from murmly.platform import MACOS_ACCESSIBILITY_PERMISSION, PERMISSIONS

        if CGEVENT_POST_METHOD in set(excluded):
            return PasteInjection(
                None,
                None,
                reason=f"{CGEVENT_POST_METHOD} failed to inject a paste earlier in this session",
            )
        is_trusted = macos_accessibility_trusted if macos_accessibility_trusted is not None else _real_is_process_trusted
        try:
            trusted = is_trusted()
        except (OSError, ValueError, AttributeError):
            # `_real_is_process_trusted` raises when `ApplicationServices`
            # cannot even be loaded -- every real macOS host has it, so this
            # is only reachable when a caller (a test, this function's own
            # exhaustive cross-platform sweep) resolves a macOS profile on a
            # host that is not one, the same hazard `platform.py`'s own
            # `_real_macos_accessibility_trusted` already guards its one
            # documented caller against. Collapsed to "not trusted", not
            # "available": a check that cannot tell is never treated as a
            # grant, the same rule every other permission check in this
            # codebase applies to its own silence.
            trusted = False
        if not trusted:
            permission = PERMISSIONS[MACOS_ACCESSIBILITY_PERMISSION]
            # Named as the permission this genuinely is (distinct from "no
            # injector is installed", the absent case the branches below
            # report, and from "installed but cannot inject in this session",
            # the X11/Wayland probe-failure case) -- 18.12 and the
            # `transcript-delivery` spec's own scenario both require this
            # case to be told apart from the other two, not folded into
            # `reason`'s free text alone.
            return PasteInjection(
                None,
                None,
                reason="The Accessibility permission has not been granted to Murmly.",
                remedy=(f"Grant it in {permission.grant_location}.",),
            )
        return PasteInjection(CGEVENT_POST_METHOD, (), confirms_delivery=False)
    wayland = is_wayland_session(environment)
    candidates = WAYLAND_INJECTORS if wayland else X11_INJECTORS
    remedy = injector_remedy(environment)
    skipped = set(excluded)

    installed = [
        candidate
        for candidate in candidates
        if which(candidate.command[0])
        and (candidate.requires is None or environment.get(candidate.requires))
    ]
    if not installed:
        session = "Wayland" if wayland else "X11"
        return PasteInjection(
            None,
            None,
            reason=f"No {session} paste injector is installed.",
            remedy=remedy,
        )

    reasons: list[str] = []
    for candidate in installed:
        if candidate.method in skipped:
            reasons.append(f"{candidate.method} failed to inject a paste earlier in this session")
            continue
        detail = probe_injector(candidate, environment, run)
        if detail is None:
            return PasteInjection(
                candidate.method,
                candidate.command,
                confirms_delivery=candidate.confirms_delivery,
                advisory=input_consent_advisory(candidate.method, environment),
            )
        reasons.append(f"{candidate.method} is installed but cannot inject in this session: {detail}")
    return PasteInjection(None, None, reason="; ".join(reasons), remedy=remedy)


class ClipboardPaster:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        which: Which = shutil_which,
        restore_clipboard: bool = True,
        restore_delay_ms: int = 200,
        profile: PlatformProfile | None = None,
    ) -> None:
        self._env = env or os.environ
        self._which = which
        # Threaded through to `select_paste_injection` below, not re-resolved
        # independently: this class is `create_clipboard_paster`'s Linux
        # branch (its own docstring: "must not change what it returns"), so
        # its own injector choice must agree with whichever profile decided
        # to construct it here, rather than asking `resolve_platform` a
        # second time and risking a different, real-host answer -- the one
        # case that can disagree is a caller exercising this branch from a
        # non-Linux machine via `profile=`, exactly as `create_clipboard_
        # paster`'s own docstring says that parameter is for.
        self._profile = profile
        self._copy_command = choose_clipboard_copy_command(self._env, which)
        self._read_command = choose_clipboard_read_command(self._env, which)
        # Resolved but not required: a session that cannot inject a paste can still
        # copy, and a transcript on the clipboard is one the user still has.
        self._injection = select_paste_injection(self._env, which, profile=self._profile)
        self._failed_methods: set[str] = set()
        self._restore_clipboard = restore_clipboard
        self._restore_delay_ms = restore_delay_ms

    @property
    def injection(self) -> PasteInjection:
        return self._injection

    def copy_and_paste(self, text: str) -> DeliveryOutcome:
        injection = self._injection
        if not injection.available:
            self.copy(text)
            return DeliveryOutcome(False, injection.reason)
        # Not read at all when the method cannot confirm delivery: restoring over a
        # transcript that may never have arrived would destroy the only copy of it.
        restoring = self._restore_clipboard and injection.confirms_delivery
        previous = self._read_clipboard() if restoring else None
        self.copy(text)
        try:
            self._run(list(injection.command or ()))
        except (OSError, subprocess.SubprocessError) as error:
            self._demote(injection.method)
            return DeliveryOutcome(False, f"{injection.method} failed to inject the paste: {error}")
        # Only a delivered transcript is restored over: an undelivered one is all the
        # user has left of what they said.
        if previous is not None:
            time.sleep(self._restore_delay_ms / 1000)
            self.copy(previous)
        return DeliveryOutcome(True)

    def _demote(self, method: str | None) -> None:
        """Stop choosing a method that proved it cannot deliver in this session."""
        if method is None:
            return
        logger.warning("Paste injector %s failed; it will not be used again this session.", method)
        self._failed_methods.add(method)
        self._injection = select_paste_injection(
            self._env,
            self._which,
            excluded=self._failed_methods,
            profile=self._profile,
        )

    def copy(self, text: str) -> None:
        self._run(self._copy_command, text)

    def _read_clipboard(self) -> str | None:
        if not self._read_command:
            return None
        result = subprocess.run(
            self._read_command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env,
        )
        return result.stdout

    def _run(self, command: list[str], stdin_text: str | None = None) -> None:
        subprocess.run(
            command,
            input=stdin_text,
            text=True,
            encoding="utf-8",
            check=True,
            env=self._env,
        )


def create_clipboard_paster(
    env: dict[str, str] | None = None,
    which: Which = shutil_which,
    restore_clipboard: bool = True,
    restore_delay_ms: int = 200,
    profile: PlatformProfile | None = None,
) -> Paster:
    """Choose the platform's own clipboard-and-paste implementation.

    Linux keeps `ClipboardPaster`'s subprocess-based `xclip`/`wl-copy` and
    injector stack, constructed exactly as every existing caller already
    constructs it -- this is the one branch that must not change what it
    returns. Windows goes to `win_clipboard.WindowsClipboardPaster`, imported
    from inside this function so the module stays importable without
    `pywin32` on every other platform, the same deferred-import shape
    `BackendRegistry`'s loaders use.

    `profile` takes the place `env` cannot fill: `resolve_platform` reads the
    operating system from `sys.platform`, not from `env`, so a caller
    exercising the Windows branch from a non-Windows machine supplies a
    `PlatformProfile` directly rather than an environment mapping.
    """
    environment = env or os.environ
    resolved = profile if profile is not None else resolve_platform(environment)
    if resolved.operating_system is OperatingSystem.WINDOWS:
        from murmly.win_clipboard import WindowsClipboardPaster

        return WindowsClipboardPaster(
            restore_clipboard=restore_clipboard, restore_delay_ms=restore_delay_ms
        )
    if resolved.operating_system is OperatingSystem.MACOS:
        from murmly.mac_clipboard import MacosClipboardPaster

        return MacosClipboardPaster(
            restore_clipboard=restore_clipboard, restore_delay_ms=restore_delay_ms
        )
    return ClipboardPaster(
        env=environment,
        which=which,
        restore_clipboard=restore_clipboard,
        restore_delay_ms=restore_delay_ms,
        profile=resolved,
    )
