"""Installing Murmly into a desktop session.

Two pieces of state, both owned by Murmly and both removable:

* a systemd user unit anchored on ``graphical-session.target``, so the daemon
  runs for the lifetime of the graphical session rather than from boot, and
* the hotkey binding itself, on whichever mechanism the resolved desktop uses
  -- a launcher entry carrying ``X-KDE-Shortcuts`` on KDE Plasma, an entry
  Murmly appends to GNOME's ``custom-keybindings`` list on GNOME.

Murmly writes nothing else beyond the one entry each binding owns. In
particular it never edits the user's global shortcut configuration wholesale;
see ``docs/agent-notes/plasma-global-shortcut-binding.md`` and
``docs/agent-notes/gnome-custom-keybindings.md``.

``Installer`` decides which desktop's shapes to build only when the caller has
not pinned them -- see `Installer._backend_for`. Everything below that names
``PlasmaShortcuts``/``ShortcutLauncher`` by their KDE-specific names because
that is what this module has always driven directly; GNOME's counterparts
(`desktop.GnomeShortcuts`, `desktop.GnomeShortcutLauncher`) implement the same
duck-typed surface and are driven through the very same code.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path, PureWindowsPath
import plistlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from murmly.desktop import DesktopQueryError, PlasmaShortcuts
from murmly.hotkey_record import HotkeyRecordStore, default_hotkey_record_path
from murmly.hotkey import (
    Hotkey,
    HotkeyError,
    macos_hotkey_for_portable,
    parse_hotkey,
    windows_hotkey_for_portable,
)
from murmly.integrations import PasteInjection, select_paste_injection

if TYPE_CHECKING:
    from murmly.config import MurmlyConfig
    from murmly.platform import PlatformProfile


logger = logging.getLogger(__name__)

RunCommand = Callable[..., subprocess.CompletedProcess[str]]

SERVICE_NAME = "murmly.service"
ENTRYPOINT_NAME = "murmly"
COMMAND_TIMEOUT_SECONDS = 30.0

#: The launcher file name is also the shortcut component name Plasma uses. The
#: "net.local." prefix matches what the System Settings shortcut editor writes.
DESKTOP_ID = "net.local.murmly.desktop"
APPLICATION_NAME = "murmly"
#: The second hotkey needs a second component. One launcher file carries one
#: Exec line and one X-KDE-Shortcuts line, and the desktop cannot tell two
#: bindings on one component apart, so each purpose gets its own entry.
SESSION_DESKTOP_ID = "net.local.murmly-session.desktop"
SESSION_APPLICATION_NAME = "murmly speech session"
REGISTRATION_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.2
#: How long an in-process-hotkey install (task 8.6) waits for the daemon it
#: just started to accept a command, and how often it polls -- longer than
#: `REGISTRATION_TIMEOUT_SECONDS`, which only waits for a desktop to notice a
#: file, because this waits for a whole process (models and all) to start.
WINDOWS_DAEMON_START_TIMEOUT_SECONDS = 10.0
WINDOWS_DAEMON_POLL_INTERVAL_SECONDS = 0.2

#: X-KDE-Shortcuts is the line that binds the key. X-KDE-GlobalAccel-CommandShortcut
#: is cosmetic: only the System Settings editor reads it, to label the entry as a
#: command rather than an application.
LAUNCHER_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name={name}
Exec={exec_line}
NoDisplay=true
StartupNotify=false
X-KDE-GlobalAccel-CommandShortcut=true
X-KDE-Shortcuts={shortcut}
"""

SERVICE_UNIT_TEMPLATE = """\
[Unit]
Description=murmly local voice-to-text daemon
Documentation=https://github.com/osirison/murmly
PartOf=graphical-session.target
After=graphical-session.target
# Ordered after the audio server so systemd stops Murmly before it, rather than
# alongside it. PortAudio's JACK backend aborts the process if it is torn down
# after the server has gone, and a logout stops both in no particular order.
After=pipewire.service wireplumber.service

[Service]
Type=simple
ExecStart={exec_start} daemon
Restart=on-failure
RestartSec=2
SyslogIdentifier=murmly

[Install]
WantedBy=graphical-session.target
"""


class InstallError(RuntimeError):
    """Installation could not be completed."""


class HotkeyNotConfirmedError(InstallError):
    """The launcher was written but the desktop did not pick it up in time.

    Distinct from other failures because the binding is already persisted: it
    will be in effect at the next login even though it is not active now.

    Carries the keys it concerns, which is not always every key requested: an
    install of two can fail on either, and naming both tells the person that a
    key which is bound, confirmed and working right now is not active.
    """

    def __init__(
        self, message: str, hotkeys: "Hotkey | tuple[Hotkey, ...] | None" = None
    ) -> None:
        super().__init__(message)
        if hotkeys is None:
            self.hotkeys: tuple[Hotkey, ...] = ()
        elif isinstance(hotkeys, tuple):
            self.hotkeys = hotkeys
        else:
            self.hotkeys = (hotkeys,)


class HotkeyConflictError(InstallError):
    """The requested hotkey belongs to another application.

    Plasma does not arbitrate this: a second claimant registers without error
    and never receives the key, so Murmly must refuse rather than bind.
    """


def resolve_entrypoint(executable: str | None = None) -> Path:
    """Locate the ``murmly`` console script beside the running interpreter.

    The installed unit and the hotkey both invoke Murmly by absolute path, so
    neither depends on the search path in effect at session start. The search
    path is deliberately not consulted here: ``which`` would resolve to whatever
    happens to be first for the installing shell, which is not necessarily the
    installation performing the install.
    """
    interpreter = Path(executable or sys.executable)
    if not interpreter.name:
        raise InstallError("Unable to determine the running Python interpreter.")

    # A pure function of the interpreter path's own shape, not of
    # `sys.platform`: `uv`'s Windows venvs put `python.exe` in `Scripts/`
    # beside `murmly.exe`, the console script `uv sync` generates there, so an
    # interpreter ending in `.exe` is what says which name to look for --
    # testable from Linux with `executable=` alone.
    name = f"{ENTRYPOINT_NAME}.exe" if interpreter.suffix.casefold() == ".exe" else ENTRYPOINT_NAME
    candidate = interpreter.with_name(name)
    if not candidate.exists():
        raise InstallError(
            f"Could not find the murmly entrypoint at {candidate}. Install the project "
            "into its environment first, for example with 'uv sync'."
        )
    if not candidate.is_file():
        raise InstallError(f"The murmly entrypoint at {candidate} is not a file.")
    if not os.access(candidate, os.X_OK):
        raise InstallError(f"The murmly entrypoint at {candidate} is not executable.")
    return candidate.resolve()


def service_unit_text(entrypoint: Path) -> str:
    """The unit body.

    ``PartOf`` stops the daemon at logout, ``After`` orders it behind the
    session, and ``WantedBy`` is what actually activates it. The unit shipped
    before this change had only the ordering, which is why it never started.

    ``entrypoint.as_posix()``, not ``str(entrypoint)``: a systemd unit is a
    Linux artifact regardless of which host renders its text, and `Path`
    renders in whatever flavour the *host* is -- backslashes on a Windows
    runner exercising this Linux-only code path in the test suite, which
    would spell a path systemd itself would never accept.
    """
    return SERVICE_UNIT_TEMPLATE.format(exec_start=entrypoint.as_posix())


def write_atomically(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def default_unit_dir(env: dict[str, str] | None = None) -> Path:
    environment = env if env is not None else os.environ
    config_home = environment.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "systemd" / "user"


def default_applications_dir(env: dict[str, str] | None = None) -> Path:
    environment = env if env is not None else os.environ
    data_home = environment.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "applications"


def default_launch_agents_dir(env: dict[str, str] | None = None) -> Path:
    """`~/Library/LaunchAgents`, the per-user launchd agent directory
    (design.md's "Service management" table). Not an XDG path -- macOS has no
    concept of one -- so this reads `HOME` directly rather than following
    `default_unit_dir`'s `XDG_CONFIG_HOME` precedent, the same way
    `Path.home()` is `default_unit_dir`'s own fallback when nothing is set.
    """
    environment = env if env is not None else os.environ
    home = environment.get("HOME")
    base = Path(home) if home else Path.home()
    return base / "Library" / "LaunchAgents"


def default_shortcut_config_path(env: dict[str, str] | None = None) -> Path:
    environment = env if env is not None else os.environ
    config_home = environment.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "kglobalshortcutsrc"


@dataclass(frozen=True, slots=True)
class HotkeyPurpose:
    """What one bound hotkey is for, and the desktop state that carries it.

    Every rule in this module about claiming, binding, verifying, releasing and
    reporting applies to each purpose on its own, so a failure affecting one is
    never reported as a failure of the other.
    """

    key: str
    desktop_id: str
    name: str
    command: str
    description: str


WINDOW_HOTKEY = HotkeyPurpose(
    key="window",
    desktop_id=DESKTOP_ID,
    name=APPLICATION_NAME,
    command="toggle",
    description="dictate into the focused window",
)
SESSION_HOTKEY = HotkeyPurpose(
    key="session",
    desktop_id=SESSION_DESKTOP_ID,
    name=SESSION_APPLICATION_NAME,
    command="toggle-session",
    description="dictate into the open speech session",
)
HOTKEY_PURPOSES = (WINDOW_HOTKEY, SESSION_HOTKEY)

#: Task 13.7: the one real limitation Carbon's `RegisterEventHotKey` carries
#: that no other hotkey backend in this codebase does -- design.md's own
#: text for it, reported rather than hidden. Surfaced in `_status_in_process`'s
#: report whenever the resolved platform is macOS, unconditionally rather
#: than only when a binding is currently unheld: the limitation is a standing
#: property of the mechanism itself (a combination can be held by Murmly and
#: still never fire, because the frontmost application consumed it first),
#: not a symptom `held`/`detail` above already captures.
MACOS_HOTKEY_MECHANISM_LIMITATION = (
    "RegisterEventHotKey does not fire when the frontmost application consumes the "
    "key combination itself, and it cannot express a modifier-only chord (there is "
    "no key code for \"no key\", only for a modifier combined with one)."
)


def launcher_text(
    entrypoint: Path,
    hotkey: Hotkey,
    name: str = APPLICATION_NAME,
    command: str = "toggle",
) -> str:
    """The launcher body.

    A literal ``%`` is doubled, matching how the desktop's own shortcut editor
    escapes an Exec value.

    ``entrypoint.as_posix()``, not ``str(entrypoint)``, for the same reason
    `service_unit_text` renders it that way: a `.desktop` launcher is a Linux
    artifact, and `Path` otherwise renders in the host's own flavour rather
    than the target's.
    """
    exec_line = f"{entrypoint.as_posix()} {command}".replace("%", "%%")
    return LAUNCHER_TEMPLATE.format(name=name, exec_line=exec_line, shortcut=hotkey.portable)


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    installed: bool
    active: bool
    entrypoint: str | None
    detail: str


class UserService:
    """The Murmly systemd user unit."""

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        unit_dir: Path | None = None,
        env: dict[str, str] | None = None,
        systemctl: str = "systemctl",
    ) -> None:
        self._run_command = run_command
        self._unit_dir = unit_dir if unit_dir is not None else default_unit_dir(env)
        self._binary = systemctl

    @property
    def unit_path(self) -> Path:
        return self._unit_dir / SERVICE_NAME

    @property
    def is_installed(self) -> bool:
        return self.unit_path.is_file()

    def install(self, entrypoint: Path) -> None:
        """Write the unit and bring the service up.

        ``graphical-session.target`` sets ``RefuseManualStart``, so the service
        is started directly rather than by starting the target it is wanted by.
        """
        write_atomically(self.unit_path, service_unit_text(entrypoint))
        self._systemctl_checked("daemon-reload")
        self._systemctl_checked("enable", SERVICE_NAME)
        self._systemctl_checked("start", SERVICE_NAME)

    def remove(self) -> bool:
        """Tear the service down. Succeeds when any part is already absent."""
        existed = self.is_installed
        # Best effort: a unit that is already stopped, already disabled, or was
        # never known to systemd must not fail an uninstall.
        self._systemctl("stop", SERVICE_NAME)
        self._systemctl("disable", SERVICE_NAME)
        self.unit_path.unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return existed

    def start(self) -> bool:
        return self._systemctl("start", SERVICE_NAME).returncode == 0

    def is_active(self) -> bool:
        result = self._systemctl("is-active", SERVICE_NAME)
        return result.returncode == 0 and (result.stdout or "").strip() == "active"

    def recorded_entrypoint(self) -> str | None:
        """The ``ExecStart`` path in the installed unit, if any."""
        if not self.is_installed:
            return None
        try:
            content = self.unit_path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            if line.startswith("ExecStart="):
                value = line.removeprefix("ExecStart=").strip()
                return value.removesuffix(" daemon").strip() or None
        return None

    def status(self) -> ServiceStatus:
        if not self.is_installed:
            return ServiceStatus(
                installed=False,
                active=False,
                entrypoint=None,
                detail="Murmly is not installed. Run 'murmly install <hotkey>' to install it.",
            )
        active = self.is_active()
        return ServiceStatus(
            installed=True,
            active=active,
            entrypoint=self.recorded_entrypoint(),
            detail=(
                "The Murmly service is installed and running."
                if active
                else "The Murmly service is installed but not running."
            ),
        )

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [self._binary, "--user", *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallError(f"Unable to run '{' '.join(command)}': {error}") from error

    def _systemctl_checked(self, *arguments: str) -> None:
        result = self._systemctl(*arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"systemctl --user {' '.join(arguments)} failed: {detail or 'unknown error'}")


#: The Task Scheduler task name. Not `SERVICE_NAME` (`murmly.service`):
#: Task Scheduler task names conventionally carry no file-extension-shaped
#: suffix, and giving the two platforms visibly different names is a small
#: mercy to anyone reading a task list who has never heard of systemd units.
WINDOWS_TASK_NAME = "MurmlyDaemon"


def _quoted_for_schtasks(value: str) -> str:
    """Quote `value` only if it needs it -- `schtasks /tr` splits on spaces
    outside quotes, so a bare `C:\\Program Files\\murmly.exe` would be read as
    two arguments without this."""
    return f'"{value}"' if " " in value else value


def _windows_path_text(path: Path) -> str:
    """`path` rendered the way `schtasks` itself expects: backslashes,
    always -- never whatever separator the *host* building this string
    happens to spell paths with.

    `str(path)` alone renders in `Path`'s own flavour, which follows the host
    (`os.name`), not the platform this text is destined for: a `WindowsPath`
    on a real Windows machine already renders this way, but the very same
    entrypoint constructed as a `PosixPath` -- every one of this class's own
    tests, run on the Linux and macOS CI runners this suite also runs on --
    renders with forward slashes instead, which is not a command `schtasks`
    would accept. Routing the string through `PureWindowsPath` first is what
    makes the two agree on every host.
    """
    return str(PureWindowsPath(str(path)))


def macos_launchd_agent_plist(
    label: str,
    program_arguments: Sequence[str],
    associated_bundle_identifiers: Sequence[str] | None = None,
) -> dict[str, object]:
    """The property-list content for a per-user launchd agent (design.md's
    service-management table): `Label`, `ProgramArguments`, `RunAtLoad`,
    `KeepAlive`, and -- task 12.3's remedy for the TCC microphone risk --
    `AssociatedBundleIdentifiers` when one is given.

    A bare Python process started by launchd has no bundle and no
    `NSMicrophoneUsageDescription`, so TCC has nothing to attribute a
    microphone request to and the reported failure is silent: no dialog, no
    exception, a stream delivering zeroes (design.md's largest risk). Setting
    `AssociatedBundleIdentifiers` to a bundle identifier that already holds
    the grant is the first thing task 12.3 asks to try before task 12.4's
    fallback -- a minimal `.app` wrapper of Murmly's own. Murmly ships no
    signed bundle of its own yet, so this only has something real to name
    once one exists (12.4) or once some other already-granted bundle is
    deliberately reused for the spike; until then the parameter is left
    unset by every caller that has not confirmed a value works, which is why
    it defaults to `None` and is omitted from the plist entirely rather than
    written as an empty list -- an empty `AssociatedBundleIdentifiers` is not
    documented as "no association" the way omitting the key is.

    This function only shapes the dictionary `plistlib.dump` writes -- it
    does not call `launchctl` and does not write a file. `docs/`'s macOS
    microphone spike script is the first caller; `LaunchdUserService` (task
    13.3, below) is the second. Kept here, next to
    `WindowsUserService`, rather than in `murmly.platform`: it is a
    service-management shape exactly like that class and `UserService`, not
    a platform-resolution concern that module's own docstring restricts
    itself to.

    `program_arguments` is taken as given -- the caller decides the
    interpreter and every argument, matching `WindowsUserService`'s
    `_run_line` building its own command line rather than this function
    guessing at one. It is stored as a plain `list` because `plistlib`
    requires a concrete sequence type it recognises, not an arbitrary
    `Sequence`.
    """
    plist: dict[str, object] = {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    if associated_bundle_identifiers:
        plist["AssociatedBundleIdentifiers"] = list(associated_bundle_identifiers)
    return plist


#: The launchd label -- `Label` in the plist and the last path component of
#: every `launchctl` domain target this class builds. Not `SERVICE_NAME`
#: (`murmly.service`): a launchd label is conventionally reverse-DNS, matching
#: `DESKTOP_ID`'s own "net.local." prefix rather than a systemd unit's `.service`
#: suffix, which would read as a copy-paste from the wrong platform to anyone
#: who has seen a launchd plist before.
LAUNCHD_LABEL = "net.local.murmly"


class LaunchdUserService:
    """The Murmly launchd agent (task 13.3), driven by `launchctl bootstrap`,
    `print`, `kickstart -k` and `bootout` -- never `load` (task 13.4).

    `load` has been deprecated since the 10.10 launchd rewrite, and its
    failure mode is the disqualifying one for this codebase's own rule that a
    binding must be verified before success is reported (design.md's "Service
    management" section, this class's own docstring history): on a malformed
    plist it exits 0 and does nothing, which would make `install()` report
    success for a service that will never run. `bootstrap` fails loudly on
    the same malformed plist instead, and `print`'s parsed `state = running`
    line is what `install()` checks before it reports success at all --
    `is_active()` reads the very same line, so a `status()` call after
    install can never disagree with what install itself just verified.

    Every method takes the same shape `UserService` and `WindowsUserService`
    do -- `run_command` injected, real `subprocess.run` by default -- so
    `Installer._default_service` (`SERVICE_MANAGEMENT`'s own selection) can
    construct this the same way it constructs either of them,
    and so a test drives this class exactly like every other backend in the
    codebase: against a fake runner and a scratch `agents_dir`, never a real
    `launchctl`, which does not exist on this machine.

    `launchctl print`'s text output is explicitly undocumented and not a
    stable interface (`man launchctl` itself carries no format contract for
    it) -- confirmed only by inspecting real `launchctl print` output on a
    Mac, which this development machine cannot do. `is_active`/`recorded_
    entrypoint` parse it conservatively (`state = running`, `ProgramArguments`
    read back from the plist file itself rather than reparsing `print`'s own
    `arguments = { ... }` block) for the same reason `WindowsUserService`'s
    own docstring flags `schtasks`' localized text as a real gap this class
    does not close either.
    """

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        agents_dir: Path | None = None,
        env: dict[str, str] | None = None,
        launchctl: str = "launchctl",
        label: str = LAUNCHD_LABEL,
        uid: int | None = None,
    ) -> None:
        self._run_command = run_command
        self._agents_dir = agents_dir if agents_dir is not None else default_launch_agents_dir(env)
        self._binary = launchctl
        self._label = label
        # `getattr(os, "getuid", None)`, called only where it exists, not the
        # bare name: this class is only ever *meaningfully* constructed for a
        # resolved macOS profile, where `os.getuid` always exists, but
        # `Installer.__init__` calls `_default_service()` -- and so this
        # constructor -- eagerly, the instant `SERVICE_MANAGEMENT` selects
        # `launchd` for `self._profile`, which only requires the *profile* to
        # say macOS, not the host. A test that resolves a macOS profile on a
        # real Windows interpreter (to keep `Installer`'s dispatch exercised
        # on every host, the same reason `peer_identity_mechanism_for` keeps
        # a macOS branch exercised there) would otherwise hit a bare
        # `AttributeError` while merely building the object, before ever
        # calling `launchctl`. `_domain_target` below is what actually needs
        # a real uid, and raises there instead.
        local_getuid = getattr(os, "getuid", None)
        self._uid = uid if uid is not None else (local_getuid() if local_getuid is not None else None)

    @property
    def _domain_target(self) -> str:
        if self._uid is None:
            raise InstallError(
                "launchd is only usable on macOS, which always provides os.getuid(); "
                "this interpreter does not."
            )
        return f"gui/{self._uid}"

    @property
    def _service_target(self) -> str:
        return f"{self._domain_target}/{self._label}"

    @property
    def plist_path(self) -> Path:
        return self._agents_dir / f"{self._label}.plist"

    @property
    def is_installed(self) -> bool:
        return self.plist_path.is_file()

    def install(self, entrypoint: Path) -> None:
        """Write the plist and bring the agent up, verifying it before
        returning (task 13.4's own "never `load`" reasoning, applied here).

        `bootout` runs first, best-effort: `bootstrap` refuses to replace a
        domain target that is already bootstrapped, so a reinstall (a moved
        checkout, a repeated `murmly install`) has to clear any previous
        registration before making a fresh one, exactly as `UserService.install`'s
        `daemon-reload` step is what lets systemd notice a rewritten unit file
        rather than keep serving its old, cached copy.
        """
        plist = macos_launchd_agent_plist(self._label, [entrypoint.as_posix(), "daemon"])
        write_atomically(self.plist_path, plistlib.dumps(plist).decode("utf-8"))
        self._launchctl("bootout", self._service_target)
        self._launchctl_checked("bootstrap", self._domain_target, str(self.plist_path))
        self._launchctl_checked("kickstart", "-k", self._service_target)
        if not self.is_active():
            raise InstallError(
                f"launchctl reports {self._service_target} is not running after "
                f"bootstrap; run 'launchctl print {self._service_target}' to see why."
            )

    def remove(self) -> bool:
        """Tear the agent down. Succeeds when it is already absent."""
        existed = self.is_installed
        # Best effort: an agent already booted out, or never bootstrapped at
        # all, must not fail an uninstall -- `UserService.remove`'s own rule.
        self._launchctl("bootout", self._service_target)
        self.plist_path.unlink(missing_ok=True)
        return existed

    def start(self) -> bool:
        """`kickstart -k`, falling back to a fresh `bootstrap` first.

        Task 13.4 names exactly four verbs -- `bootstrap`, `print`,
        `kickstart -k`, `bootout` -- with no lighter-weight "stop but stay
        registered" verb among them the way `systemctl stop`/`schtasks /end`
        each are for their own platforms. `stop()` below uses `bootout`,
        which removes the domain target entirely, so a `kickstart -k` after
        it fails (there is nothing left to kick) until this method
        re-bootstraps from the plist already on disk.
        """
        if self._launchctl("kickstart", "-k", self._service_target).returncode == 0:
            return True
        if not self.is_installed:
            return False
        self._launchctl("bootstrap", self._domain_target, str(self.plist_path))
        return self._launchctl("kickstart", "-k", self._service_target).returncode == 0

    def stop(self) -> bool:
        return self._launchctl("bootout", self._service_target).returncode == 0

    def is_active(self) -> bool:
        result = self._launchctl("print", self._service_target)
        if result.returncode != 0:
            return False
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("state = "):
                return stripped.removeprefix("state = ").strip() == "running"
        return False

    def recorded_entrypoint(self) -> str | None:
        """The first `ProgramArguments` entry in the installed plist, if any.

        Read from the plist file this class itself wrote, not from `launchctl
        print`'s own `arguments = { ... }` block: the plist's shape is this
        class's own contract (`macos_launchd_agent_plist`), while `print`'s is
        undocumented and not confirmed on a real Mac -- see this class's own
        docstring.
        """
        if not self.is_installed:
            return None
        try:
            data = plistlib.loads(self.plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        arguments = data.get("ProgramArguments")
        if not arguments:
            return None
        value = str(arguments[0]).strip()
        return value or None

    def status(self) -> ServiceStatus:
        if not self.is_installed:
            return ServiceStatus(
                installed=False,
                active=False,
                entrypoint=None,
                detail="Murmly is not installed. Run 'murmly install <hotkey>' to install it.",
            )
        active = self.is_active()
        return ServiceStatus(
            installed=True,
            active=active,
            entrypoint=self.recorded_entrypoint(),
            detail=(
                "The Murmly service is installed and running."
                if active
                else "The Murmly service is installed but not running."
            ),
        )

    def _launchctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [self._binary, *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallError(f"Unable to run '{' '.join(command)}': {error}") from error

    def _launchctl_checked(self, *arguments: str) -> None:
        result = self._launchctl(*arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"launchctl {' '.join(arguments)} failed: {detail or 'unknown error'}")


class WindowsUserService:
    """The Murmly Task Scheduler task (task 8.1).

    Task Scheduler over the Startup folder or `HKCU\\...\\Run`: it is the only
    one of the three with CLI verbs for start, stop, status, enable and
    disable (design.md's "Service management" table), which `is_active()` and
    `status()` below have to answer the same way `UserService`'s systemd
    equivalents do -- a shortcut or a registry value can only say whether it
    exists, never whether the process it names is currently running.

    `install()` passes neither `/ru` nor `/rl HIGHEST`: omitting `/rl`
    defaults the task to `LIMITED` -- the same privilege level the installing
    user's own logon session has -- which is what keeps registration and
    startup free of an administrative prompt (task 8.2). That claim can only
    be confirmed by actually running `schtasks` on Windows; this class's own
    tests confirm only that the invocation never *asks* for elevation, against
    a fake `run_command`.

    Every method takes the same shape `UserService` does -- `run_command`
    injected, real `subprocess.run` by default -- so `Installer._backend_for`
    can select between the two the same way it already selects a hotkey
    backend, and so a test drives this class exactly like every other backend
    in the codebase: against a fake runner, never a real `schtasks.exe`, which
    does not exist on this machine.

    `schtasks`' text output is localized, and this class's `/query` parsing
    (`is_active`, `recorded_entrypoint`) matches only the English column
    headers and status text (`"Status:"`, `"Running"`, `"Task To Run:"`) --
    confirmed correct only on an English-language Windows install; a
    non-English one is a real gap this class does not close.
    """

    def __init__(
        self,
        run_command: RunCommand = subprocess.run,
        task_name: str = WINDOWS_TASK_NAME,
        schtasks: str = "schtasks",
    ) -> None:
        self._run_command = run_command
        self._task_name = task_name
        self._binary = schtasks

    @property
    def is_installed(self) -> bool:
        return self._schtasks("/query", "/tn", self._task_name).returncode == 0

    def install(self, entrypoint: Path) -> None:
        """Create the logon-trigger task and start it.

        `/f` overwrites a task of this name from a previous install rather
        than failing on one, matching `UserService.install`'s
        `write_atomically` + `daemon-reload` -- both are "make it look like
        this from now on", not "fail if it already existed".
        """
        run_line = f"{_quoted_for_schtasks(_windows_path_text(entrypoint))} daemon"
        self._schtasks_checked(
            "/create", "/tn", self._task_name, "/tr", run_line, "/sc", "onlogon", "/f"
        )
        self.start()

    def remove(self) -> bool:
        """Tear the task down. Succeeds when it is already absent."""
        existed = self.is_installed
        # Best effort, like `UserService.remove`: a task already stopped or
        # never registered must not fail an uninstall.
        self._schtasks("/end", "/tn", self._task_name)
        self._schtasks("/delete", "/tn", self._task_name, "/f")
        return existed

    def start(self) -> bool:
        return self._schtasks("/run", "/tn", self._task_name).returncode == 0

    def stop(self) -> bool:
        return self._schtasks("/end", "/tn", self._task_name).returncode == 0

    def enable(self) -> bool:
        return self._schtasks("/change", "/tn", self._task_name, "/enable").returncode == 0

    def disable(self) -> bool:
        return self._schtasks("/change", "/tn", self._task_name, "/disable").returncode == 0

    def start_command_text(self) -> str:
        """What a person can run to start the service by hand.

        Named in diagnostics (task 8.7's "name the command that starts the
        service") rather than a hotkey press: there is no press to recover
        from on this platform (`Installer._status_in_process`'s own
        docstring), so the report has to name something else.
        """
        return f"{self._binary} /run /tn {self._task_name}"

    def is_active(self) -> bool:
        result = self._schtasks("/query", "/tn", self._task_name, "/fo", "LIST")
        if result.returncode != 0:
            return False
        for line in (result.stdout or "").splitlines():
            if line.startswith("Status:"):
                return line.removeprefix("Status:").strip().casefold() == "running"
        return False

    def recorded_entrypoint(self) -> str | None:
        """The command `/tr` names in the installed task, if any."""
        result = self._schtasks("/query", "/tn", self._task_name, "/fo", "LIST", "/v")
        if result.returncode != 0:
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("Task To Run:"):
                value = line.removeprefix("Task To Run:").strip()
                value = value.removesuffix(" daemon").strip()
                if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                    value = value[1:-1]
                return value or None
        return None

    def status(self) -> ServiceStatus:
        if not self.is_installed:
            return ServiceStatus(
                installed=False,
                active=False,
                entrypoint=None,
                detail="Murmly is not installed. Run 'murmly install <hotkey>' to install it.",
            )
        active = self.is_active()
        return ServiceStatus(
            installed=True,
            active=active,
            entrypoint=self.recorded_entrypoint(),
            detail=(
                "The Murmly service is installed and running."
                if active
                else "The Murmly service is installed but not running."
            ),
        )

    def _schtasks(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run `schtasks`, decoding its output without trusting the locale default.

        With `text=True` and no `encoding=`, `subprocess` decodes with
        `locale.getpreferredencoding()` -- the process's ANSI codepage on
        Windows, e.g. `cp1252` -- while a legacy console tool like
        `schtasks.exe` actually writes its output in the console's *OEM*
        codepage, a different table this process has no reliable way to ask
        for. Pinning `encoding="utf-8"` does not make that mismatch correct
        either, but paired with `errors="replace"` it guarantees this can
        never raise `UnicodeDecodeError` on a byte no codec recognises --
        exactly the failure a bare `.read_text()` produced elsewhere in this
        codebase on real Windows CI. Every caller of this method only checks
        `returncode` or logs `stdout`/`stderr` as a diagnostic, so a
        replacement character in place of an unmappable byte costs nothing
        this code relies on.
        """
        command = [self._binary, *arguments]
        try:
            return self._run_command(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallError(f"Unable to run '{' '.join(command)}': {error}") from error

    def _schtasks_checked(self, *arguments: str) -> None:
        result = self._schtasks(*arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"schtasks {' '.join(arguments)} failed: {detail or 'unknown error'}")


class ShortcutLauncher:
    """The launcher entry whose ``X-KDE-Shortcuts`` line binds Murmly's hotkey."""

    def __init__(
        self,
        shortcuts: PlasmaShortcuts | None = None,
        run_command: RunCommand = subprocess.run,
        applications_dir: Path | None = None,
        shortcut_config_path: Path | None = None,
        env: dict[str, str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = REGISTRATION_TIMEOUT_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        cache_builder: str = "kbuildsycoca6",
        purpose: HotkeyPurpose = WINDOW_HOTKEY,
    ) -> None:
        self._purpose = purpose
        self._shortcuts = shortcuts if shortcuts is not None else PlasmaShortcuts()
        self._run_command = run_command
        self._applications_dir = (
            applications_dir if applications_dir is not None else default_applications_dir(env)
        )
        self._shortcut_config_path = (
            shortcut_config_path if shortcut_config_path is not None else default_shortcut_config_path(env)
        )
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._cache_builder = cache_builder

    @property
    def purpose(self) -> HotkeyPurpose:
        return self._purpose

    @property
    def launcher_path(self) -> Path:
        return self._applications_dir / self._purpose.desktop_id

    @property
    def is_present(self) -> bool:
        return self.launcher_path.is_file()

    def declared_hotkey(self) -> str | None:
        """The hotkey the launcher file declares, if it exists."""
        if not self.is_present:
            return None
        try:
            content = self.launcher_path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            if line.startswith("X-KDE-Shortcuts="):
                return line.removeprefix("X-KDE-Shortcuts=").strip() or None
        return None

    def declared_entrypoint(self) -> str | None:
        """The command the launcher file runs, if it exists.

        Read so that reinstalling after the checkout moved rewrites the
        launcher. Comparing only the hotkey would find it already bound and skip
        the write, leaving `Exec=` pointing at a path that no longer exists.
        """
        if not self.is_present:
            return None
        try:
            content = self.launcher_path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            if line.startswith("Exec="):
                return line.removeprefix("Exec=").strip().replace("%%", "%") or None
        return None

    def user_override(self) -> str | None:
        """A hotkey the user has set through the desktop's own settings.

        Such an entry takes precedence over ``X-KDE-Shortcuts``. Murmly reads it
        to report the override and then leaves it alone; it never writes this
        file, because the shortcut daemon rewrites the group from memory and
        would destroy an external edit.
        """
        try:
            content = self._shortcut_config_path.read_text(encoding="utf-8")
        except OSError:
            return None
        header = f"[services][{self._purpose.desktop_id}]"
        in_group = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_group = stripped == header
                continue
            if in_group and stripped.startswith("_launch="):
                return stripped.removeprefix("_launch=").strip() or None
        return None

    def register(self, entrypoint: Path, hotkey: Hotkey) -> None:
        """Write the launcher and wait for the desktop to bind it.

        Always a remove-then-add: the desktop skips components it already knows,
        so rewriting the file in place would leave the previous hotkey live for
        the rest of the session while the file claimed otherwise.
        """
        self.unregister()
        write_atomically(
            self.launcher_path,
            launcher_text(
                entrypoint, hotkey, name=self._purpose.name, command=self._purpose.command
            ),
        )
        self._rebuild_cache()
        if not self._wait_until(lambda: self._shortcuts.component_exists(self._purpose.desktop_id)):
            raise HotkeyNotConfirmedError(
                f"The desktop did not register {hotkey.portable} within "
                f"{self._timeout:g} seconds.",
                hotkey,
            )

    def unregister(self) -> bool:
        """Remove the launcher and wait for the desktop to release the hotkey.

        Reports whether anything was there to remove. Never touches the user's
        shortcut configuration: a purely declarative binding writes no entry
        there, so there is none to clean up.
        """
        if not self.is_present:
            return False
        self.launcher_path.unlink(missing_ok=True)
        self._rebuild_cache()
        if not self._wait_until(
            lambda: not self._shortcuts.component_exists(self._purpose.desktop_id)
        ):
            raise InstallError(
                "The desktop still holds the previous Murmly hotkey. It will be released "
                "at the next login."
            )
        return True

    def _rebuild_cache(self) -> None:
        """Nudge the desktop service cache.

        The cache also rebuilds itself when the applications directory changes,
        so a failure here is not fatal; the poll decides the outcome.
        """
        try:
            result = self._run_command(
                [self._cache_builder],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.debug("Desktop service cache rebuild did not run: %s", error)
            return
        if result.returncode != 0:
            logger.debug("Desktop service cache rebuild reported an error: %s", (result.stderr or "").strip())

    def _wait_until(self, predicate: Callable[[], bool]) -> bool:
        deadline = self._clock() + self._timeout
        while True:
            if predicate():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(self._poll_interval)


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """What an install or uninstall actually did."""

    entrypoint: Path | None
    hotkey: Hotkey | None
    service_installed: bool
    hotkey_registered: bool
    already_bound: bool
    session_supported: bool
    session_verified: bool
    user_override: str | None
    messages: tuple[str, ...]
    # Last and defaulted, so every existing construction of this record keeps
    # working: an installation that binds one hotkey is still an installation.
    session_hotkey: Hotkey | None = None
    session_hotkey_registered: bool = False


class Installer:
    """Orchestrates the service and the hotkey as one operation.

    Which hotkey backend `self._shortcuts`/`self._launcher`/`self._session_launcher`
    resolve to is a desktop question, not something decided at construction:
    a caller that pins any of the three (every existing test does, to run
    against a fake) gets exactly what it pinned, unchanged from before this
    class supported more than one desktop. A caller that pins none -- the real
    `Installer()` `cli.py` constructs -- gets the backend `_backend_for`
    selects from the session resolved when it is first needed, cached for the
    rest of this instance's life so `install`, `uninstall` and `status` never
    disagree about which desktop they are talking to.
    """

    def __init__(
        self,
        service: UserService | None = None,
        launcher: ShortcutLauncher | None = None,
        shortcuts: PlasmaShortcuts | None = None,
        session=None,
        entrypoint_resolver: Callable[[], Path] = resolve_entrypoint,
        injection_selector: Callable[[], PasteInjection] = select_paste_injection,
        session_launcher: ShortcutLauncher | None = None,
        record_store: HotkeyRecordStore | None = None,
        run_command: RunCommand | None = None,
        env: dict[str, str] | None = None,
        profile: PlatformProfile | None = None,
        config: MurmlyConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        daemon_timeout: float = WINDOWS_DAEMON_START_TIMEOUT_SECONDS,
        poll_interval: float = WINDOWS_DAEMON_POLL_INTERVAL_SECONDS,
    ) -> None:
        from murmly.platform import resolve_platform

        self._pinned_shortcuts = shortcuts
        self._pinned_launcher = launcher
        self._pinned_session_launcher = session_launcher
        self._backend: tuple[object, object, object] | None = None
        # Resolved before `self._service` below, which a Windows profile
        # dispatches on -- task 8.1's own selection, matching how
        # `_backend_for` already dispatches the hotkey backend on the
        # resolved session.
        self._profile = profile if profile is not None else resolve_platform()
        self._service = service if service is not None else self._default_service()
        self._session = session
        self._resolve_entrypoint = entrypoint_resolver
        self._select_injection = injection_selector
        self._record_store = (
            record_store if record_store is not None else HotkeyRecordStore(default_hotkey_record_path())
        )
        # Reach the *auto-selected* backend's own command runner and
        # environment -- `_backend_for` -- without touching a single existing
        # caller: every one of them either pins `shortcuts`/`launcher`
        # directly (bypassing `_backend_for` entirely) or leaves both `None`
        # and gets today's exact `subprocess.run` and real filesystem paths,
        # since `_backend_for` only overrides its defaults when these are not
        # `None`. This is what lets a test drive an unpinned `Installer`
        # through the real desktop-selection branch against a fake command
        # runner and a scratch directory, instead of only the two backends in
        # isolation.
        self._run_command = run_command
        self._env = env
        # Only reached by `_install_in_process`/`_status_in_process`
        # (task 8.6/8.7): the config that names the command channel a running
        # daemon is reached through. Every real Linux caller never touches
        # this, so it stays unresolved -- `load_config` opens a file the
        # desktop-launcher flow has no reason to read.
        self._config = config
        self._sleep = sleep
        self._clock = clock
        self._daemon_timeout = daemon_timeout
        self._poll_interval = poll_interval

    def _default_service(self) -> object:
        """The service backend `SERVICE_MANAGEMENT` selects for `self._profile`.

        `UserService()` -- systemd -- is the fallback when no candidate
        matches at all: `cli.main` already refuses a platform with no backend
        here before `Installer` is ever constructed (task 1.7), so this branch
        exists only for a caller (a test, or a future platform) that reaches
        this class directly. It is not a claim that systemd works there.
        """
        from murmly.platform import SERVICE_MANAGEMENT

        choice = SERVICE_MANAGEMENT.select(self._profile)
        if choice.load is None:
            return UserService()
        # `load()` yields the backend *class* (`BackendCandidate.load`'s own
        # contract -- see `test_platform.py`'s `assertIs(UserService,
        # choice.load())`), so the class itself has to be instantiated here;
        # returning `choice.load()` directly would hand `install()` an
        # unbound class rather than a service to call methods on.
        return choice.load()()

    def _current_session(self):
        if self._session is not None:
            return self._session
        from murmly.desktop import detect_desktop_session

        return detect_desktop_session()

    def _resolve_backend(self, session=None) -> tuple[object, object, object]:
        if self._backend is None:
            if (
                self._pinned_shortcuts is not None
                or self._pinned_launcher is not None
                or self._pinned_session_launcher is not None
            ):
                # At least partially pinned: resolve exactly as this class did
                # before per-desktop selection existed, filling any unpinned
                # slot with Plasma's shapes built on whichever `shortcuts`
                # this run ended up with.
                shortcuts = self._pinned_shortcuts if self._pinned_shortcuts is not None else PlasmaShortcuts()
                launcher = (
                    self._pinned_launcher if self._pinned_launcher is not None else ShortcutLauncher(shortcuts)
                )
                session_launcher = (
                    self._pinned_session_launcher
                    if self._pinned_session_launcher is not None
                    else ShortcutLauncher(shortcuts, purpose=SESSION_HOTKEY)
                )
            else:
                resolved_session = session if session is not None else self._current_session()
                shortcuts, launcher, session_launcher = self._backend_for(resolved_session)
            self._backend = (shortcuts, launcher, session_launcher)
        return self._backend

    def _backend_for(self, session) -> tuple[object, object, object]:
        """The (shortcuts, window launcher, session launcher) triple for the
        desktop `session` names.

        Every desktop besides GNOME -- including one Murmly does not register
        hotkeys on at all -- gets Plasma's shapes, unchanged from what this
        class always built: a desktop with no hotkey backend never reaches
        this method, because `install()` checks `session.supported` before any
        of the three properties are touched, and `status()`/`uninstall()` on
        such a desktop only ever read a launcher file that cannot exist there.

        `self._run_command`/`self._env` are `None` for every real caller
        (`cli.py` constructs a plain `Installer()`), so the constructor call
        below passes nothing extra and each backend class's own default --
        `subprocess.run`, the real XDG directories -- applies exactly as
        before this injection point existed. A test naming both is the only
        way either branch is reached with anything else.
        """
        from murmly.platform import Desktop

        run_command_kwargs = {} if self._run_command is None else {"run_command": self._run_command}

        if getattr(session, "desktop", Desktop.OTHER) is Desktop.GNOME:
            from murmly.desktop import GnomeShortcuts, GnomeShortcutLauncher

            shortcuts = GnomeShortcuts(**run_command_kwargs)
            return (
                shortcuts,
                GnomeShortcutLauncher(shortcuts, purpose=WINDOW_HOTKEY),
                GnomeShortcutLauncher(shortcuts, purpose=SESSION_HOTKEY),
            )
        shortcuts = PlasmaShortcuts(**run_command_kwargs)
        launcher_kwargs = dict(run_command_kwargs)
        if self._env is not None:
            launcher_kwargs["env"] = self._env
        return (
            shortcuts,
            ShortcutLauncher(shortcuts, **launcher_kwargs),
            ShortcutLauncher(shortcuts, purpose=SESSION_HOTKEY, **launcher_kwargs),
        )

    @property
    def _shortcuts(self):
        return self._resolve_backend()[0]

    @property
    def _launcher(self):
        return self._resolve_backend()[1]

    @property
    def _session_launcher(self):
        return self._resolve_backend()[2]

    def install(self, hotkey: Hotkey, session_hotkey: Hotkey | None = None) -> InstallOutcome:
        """Install the service and bind one or both hotkeys.

        The second is optional. An installation performed without it binds the
        focused-window hotkey alone and leaves speech output reachable only by a
        sender that opens a session itself, which is what an existing
        installation upgrading in place gets.
        """
        from murmly.platform import OperatingSystem, hotkey_mechanism_is_in_process

        # Task 14.5: raise the Accessibility consent dialog exactly once, from
        # this explicit `murmly install` invocation, before either install
        # path below reports whether a paste can be injected -- never from
        # `status()`, `uninstall()`, or the daemon, none of which reach this
        # method at all. See `mac_clipboard.request_accessibility_permission`'s
        # own docstring for why this is the one call site in the whole
        # codebase allowed to prompt.
        if self._profile.operating_system is OperatingSystem.MACOS:
            self._request_macos_accessibility_permission()

        if hotkey_mechanism_is_in_process(self._profile):
            # No desktop session to resolve and no launcher file to write:
            # the binding lives inside the daemon's own process (design.md's
            # "Four hotkey backends"). `_current_session()`/`_resolve_backend()`
            # below are Linux/desktop concepts this platform has no analogue
            # of, so they are never reached for it.
            return self._install_in_process(hotkey, session_hotkey)

        entrypoint = self._resolve_entrypoint()
        session = self._current_session()
        # Seeded from the same session `install()` already resolved, so a run
        # that reaches the properties below never resolves the desktop a
        # second time and cannot disagree with the `session.supported` check
        # just below.
        self._resolve_backend(session)
        messages: list[str] = []

        # Before anything is written. Two Murmly bindings on one key cannot be
        # told apart by the desktop, so one of them would silently never receive
        # the keypress.
        if session_hotkey is not None and session_hotkey.keycode == hotkey.keycode:
            raise HotkeyConflictError(
                f"{hotkey.portable} was requested for both the focused window and the "
                "speech session. The desktop cannot tell two bindings on one key apart, "
                "so one of them would never receive the keypress. Choose a different key "
                "for one of them."
            )

        if not session.supported:
            self._service.install(entrypoint)
            messages.append(session.detail)
            messages.append(
                "Murmly did not register a hotkey. Bind this command to a shortcut "
                f"in your desktop settings:\n    {entrypoint} toggle"
            )
            if session_hotkey is not None:
                # Named too, or the second key the person asked for disappears
                # from the report entirely: the install reads as a success and
                # nothing ever says the session hotkey was not bound.
                messages.append(
                    "Murmly did not register a hotkey for the speech session either. "
                    "Bind this command to a second shortcut to "
                    f"{SESSION_HOTKEY.description}:\n    {entrypoint} toggle-session"
                )
            messages.extend(self._paste_injection_messages())
            return InstallOutcome(
                entrypoint=entrypoint,
                hotkey=None,
                session_hotkey=None,
                service_installed=True,
                hotkey_registered=False,
                session_hotkey_registered=False,
                already_bound=False,
                session_supported=False,
                session_verified=False,
                user_override=None,
                messages=tuple(messages),
            )

        if not session.verified:
            messages.append(session.detail)

        # Refuse a conflict before writing anything, so a refusal leaves no
        # partial state behind. Both requested keys are judged first: a
        # collision on the second must not leave the first bound.
        requested = [(self._launcher, hotkey)]
        if session_hotkey is not None:
            requested.append((self._session_launcher, session_hotkey))
        # Every component this run is about to rewrite. A key one of them holds
        # now is a key it is about to release, so it is not a foreign claim --
        # without this, swapping Murmly's own two hotkeys was refused with a
        # message naming Murmly as the other application.
        rewriting = {self._purpose_of(launcher).desktop_id for launcher, _key in requested}
        for launcher, requested_hotkey in requested:
            self._refuse_conflict(launcher, requested_hotkey, rewriting)

        already_bound = bool(self._shortcuts.owners_of(hotkey.keycode)) and (
            self._launcher.declared_hotkey() == hotkey.portable
        )

        service_existed = self._service.is_installed
        self._service.install(entrypoint)

        # A launcher this run did not request, still bound, whose command has
        # moved. Reinstalling one hotkey is how a moved checkout is repaired,
        # and repairing only the requested one leaves the other running a path
        # that is gone -- silently, since nothing reports a launcher's command.
        for launcher in self._stale_launchers(entrypoint, requested):
            requested.append((launcher, parse_hotkey(launcher.declared_hotkey())))

        # A key moving from one Murmly purpose to another has to be released
        # before it is claimed: the desktop delivers a key to whichever
        # component claimed it first, so writing the new claim while the old one
        # still holds it binds nothing.
        claimed = {key.keycode for _launcher, key in requested}
        for launcher, key in requested:
            declared = launcher.declared_hotkey()
            if declared is None or declared == key.portable:
                continue
            try:
                held = parse_hotkey(declared).keycode
            except HotkeyError:
                continue
            if held in claimed:
                launcher.unregister()

        # Only what this run writes, so a failure removes nothing that was
        # working before the command was typed.
        written: list[object] = []
        # A key the desktop did not confirm in time is still persisted, so the
        # remaining ones are still attempted and every unconfirmed key is
        # reported together. Raising on the first left the second never written
        # at all: not bound now, and not bound at the next login either.
        unconfirmed: list[Hotkey] = []
        for launcher, requested_hotkey in requested:
            purpose = self._purpose_of(launcher)
            bound = (
                bool(self._shortcuts.owners_of(requested_hotkey.keycode))
                and launcher.declared_hotkey() == requested_hotkey.portable
                # Rewritten when the command it runs has moved, even though the
                # key is unchanged: reinstalling after the checkout moved is how
                # a stale entrypoint gets repaired, and skipping the write here
                # would leave the launcher pointing at a path that is gone.
                and launcher.declared_entrypoint() == f"{entrypoint} {purpose.command}"
            )
            if bound:
                messages.append(
                    f"{requested_hotkey.portable} is already bound to Murmly "
                    f"({purpose.description})."
                )
                continue
            try:
                written.append(launcher)
                launcher.register(entrypoint, requested_hotkey)
                self._verify(requested_hotkey, purpose)
            except HotkeyNotConfirmedError:
                # The launcher stays: the binding is persisted for next login,
                # so a later failure's rollback must not take away the one thing
                # this key still has going for it.
                written.pop()
                unconfirmed.append(requested_hotkey)
            except Exception as error:
                self._rollback(service_existed, written)
                if unconfirmed:
                    keys = ", ".join(key.portable for key in unconfirmed)
                    raise InstallError(
                        f"{error} {keys} was written but not confirmed in this "
                        f"session, and has been left in place for the next login."
                    ) from error
                raise

        if unconfirmed:
            keys = ", ".join(key.portable for key in unconfirmed)
            raise HotkeyNotConfirmedError(
                f"The desktop did not register {keys} within "
                f"{REGISTRATION_TIMEOUT_SECONDS:g} seconds.",
                tuple(unconfirmed),
            )

        self._write_hotkey_record()

        override = self._launcher.user_override()
        if override is not None and override != hotkey.portable:
            messages.append(
                f"Your desktop settings override Murmly's hotkey with {override}, which takes "
                f"precedence over {hotkey.portable}. Murmly has left that override in place."
            )

        messages.append(
            f"Registered {hotkey.portable}. Press it once to confirm it reaches Murmly: "
            "registration is confirmed, but only a keypress proves the desktop delivers it."
        )
        if session_hotkey is not None:
            messages.append(
                f"Registered {session_hotkey.portable} to {SESSION_HOTKEY.description}."
            )
        messages.extend(self._paste_injection_messages())
        return InstallOutcome(
            entrypoint=entrypoint,
            hotkey=hotkey,
            session_hotkey=session_hotkey,
            service_installed=True,
            hotkey_registered=True,
            session_hotkey_registered=session_hotkey is not None,
            already_bound=already_bound,
            session_supported=True,
            session_verified=session.verified,
            user_override=override,
            messages=tuple(messages),
        )

    def _request_macos_accessibility_permission(self) -> None:
        """Task 14.5's one call site. Never raises: a failed *request* to
        raise a dialog must not fail an installation that would otherwise
        succeed and report the permission as ungranted on its own, through
        `select_paste_injection`'s `macos_accessibility_trusted` check
        (`integrations.py`) -- exactly the same "a report must never fail an
        install" rule `_paste_injection_messages` states for itself.
        """
        try:
            from murmly.mac_clipboard import request_accessibility_permission

            request_accessibility_permission()
        except Exception as error:  # noqa: BLE001 - a permission request must never fail an install
            logger.debug("Could not request the macOS Accessibility permission: %s", error)

    def _write_hotkey_record(self) -> None:
        """Persist which purposes are actually bound, for task 5.4's record.

        Built from `declared_hotkey()` on both launchers -- the bound state --
        rather than only the keys this run requested: an install naming one
        purpose alone must not drop the other purpose's still-bound entry from
        the record. Written unconditionally, on every platform: the record
        costs nothing to keep where nothing reads it yet, and is exactly what
        an in-process backend (Windows, section 8; macOS, section 13) needs
        the day one exists. See `hotkey_record.py`.
        """
        bindings: dict[str, str] = {}
        for launcher in (self._launcher, self._session_launcher):
            declared = launcher.declared_hotkey()
            if declared is not None:
                bindings[self._purpose_of(launcher).key] = declared
        try:
            self._record_store.write(bindings)
        except OSError as error:
            # A hotkey is already bound and verified by this point -- the
            # record is a convenience nothing on Linux reads yet, and must
            # never be the reason an otherwise successful install reports
            # failure. See `hotkey_record.py`'s own "must never raise" rule.
            logger.warning("Could not write the hotkey record: %s", error)

    def _paste_injection_messages(self) -> tuple[str, ...]:
        """Say whether a transcript will reach the focused window.

        Reported, never remedied: enabling an injector needs root, and Murmly
        confines its writes to the files it owns. An installation without one still
        works — every transcript is copied to the clipboard.
        """
        try:
            injection = self._select_injection()
        except Exception as error:  # noqa: BLE001 - a report must never fail an install
            logger.debug("Paste injector selection failed during install: %s", error)
            return ()
        if injection.available:
            return (f"Transcripts will be pasted into the focused window with {injection.method}.",)
        messages = [f"Transcripts will be copied to the clipboard but not pasted: {injection.reason}"]
        if injection.remedy:
            remedy = "\n".join(f"    {line}" for line in injection.remedy)
            messages.append(f"To paste in this session, run:\n{remedy}")
        return tuple(messages)

    def _refuse_conflict(self, launcher, hotkey: Hotkey, rewriting=frozenset()) -> None:
        purpose = self._purpose_of(launcher)
        owners = self._shortcuts.owners_of(hotkey.keycode)
        own = {purpose.desktop_id, *rewriting}
        foreign = [owner for owner in owners if owner.component_unique not in own]
        if not foreign:
            return
        if all(o.component_unique in {DESKTOP_ID, SESSION_DESKTOP_ID} for o in foreign):
            # Murmly's own other hotkey, which this run was not asked to touch.
            # The generic message sends the person to their desktop settings to
            # release a binding Murmly wrote and Murmly can move -- and naming
            # Murmly as the conflicting application reads as a bug report.
            other = self._purpose_of(
                self._session_launcher
                if purpose.desktop_id == DESKTOP_ID
                else self._launcher
            )
            raise HotkeyConflictError(
                f"{hotkey.portable} is currently Murmly's own hotkey to "
                f"{other.description}. Name both keys in one command to move it "
                f"-- `murmly install <window-key> <session-key>` -- or run "
                f"`murmly uninstall` first."
            )
        names = ", ".join(sorted({owner.label for owner in foreign}))
        raise HotkeyConflictError(
            f"{hotkey.portable} is already used by {names}. Choose a different hotkey, "
            "or release that one in your desktop shortcut settings first."
        )

    def _stale_launchers(self, entrypoint: Path, requested) -> list[object]:
        """Bound launchers this run did not ask about whose command has moved."""
        asked = {id(launcher) for launcher, _hotkey in requested}
        stale = []
        for launcher in (self._launcher, self._session_launcher):
            if id(launcher) in asked:
                continue
            declared = launcher.declared_hotkey()
            if declared is None:
                continue
            purpose = self._purpose_of(launcher)
            if launcher.declared_entrypoint() == f"{entrypoint} {purpose.command}":
                continue
            try:
                parse_hotkey(declared)
            except HotkeyError:
                # Its own file says something Murmly cannot parse, so there is
                # no key to rebind it to. Left exactly as found.
                continue
            stale.append(launcher)
        return stale

    @staticmethod
    def _purpose_of(launcher) -> HotkeyPurpose:
        return getattr(launcher, "purpose", WINDOW_HOTKEY)

    def _verify(self, hotkey: Hotkey, purpose: HotkeyPurpose = WINDOW_HOTKEY) -> None:
        """Confirm the desktop resolved the launcher to the intended key.

        The key-code comparison checks Murmly's own key table against the
        desktop's parser, so a wrong constant fails here rather than silently
        binding something else. Each purpose is verified against its own
        component, so a failure affecting one is never reported for the other.
        """
        registered = self._shortcuts.registered_keys(purpose.desktop_id)
        if registered != [hotkey.keycode]:
            raise InstallError(
                f"The desktop registered a different hotkey than requested. Asked for "
                f"{hotkey.portable} ({hotkey.keycode}), got {registered or 'nothing'}."
            )

        owners = self._shortcuts.owners_of(hotkey.keycode)
        others = [owner for owner in owners if owner.component_unique != purpose.desktop_id]
        if others:
            names = ", ".join(sorted({owner.label for owner in others}))
            raise InstallError(
                f"{hotkey.portable} is claimed by both Murmly and {names}. The desktop "
                "delivers such a key to whichever claimed it first, so Murmly would never "
                "receive it."
            )
        if not owners:
            raise InstallError(f"The desktop reports no owner for {hotkey.portable} after registering it.")

    def _rollback(self, service_existed: bool, written: "list[object]") -> None:
        """Undo what this run created, leaving anything pre-existing alone.

        Only the launchers this run wrote. Removing both would delete a hotkey
        that was bound and working before the command started and that the
        failure never touched. Catches `DesktopQueryError` alongside
        `InstallError`: GNOME's launcher raises the former for a gsettings
        failure, where Plasma's raises the latter -- rollback must not let
        either backend's own failure replace the error that triggered it.
        """
        for launcher in written:
            try:
                launcher.unregister()
            except (InstallError, DesktopQueryError):
                logger.warning("Could not remove a Murmly launcher during rollback.")
        if not service_existed:
            try:
                self._service.remove()
            except InstallError:
                logger.warning("Could not remove the Murmly service during rollback.")

    def uninstall(self) -> InstallOutcome:
        """Remove everything Murmly installed, tolerating anything already gone."""
        from murmly.platform import hotkey_mechanism_is_in_process

        if hotkey_mechanism_is_in_process(self._profile):
            return self._uninstall_in_process()

        messages: list[str] = []
        problems: list[str] = []

        # Each binding released on its own, so an installation carrying only
        # one of them succeeds and does not report the absent one as a failure.
        # Catches `DesktopQueryError` alongside `InstallError` for the same
        # reason `_rollback` does: GNOME's launcher can raise either, from the
        # same gsettings call KDE's launcher has no equivalent of.
        hotkey_removed = False
        for launcher in (self._launcher, self._session_launcher):
            try:
                hotkey_removed = launcher.unregister() or hotkey_removed
            except (InstallError, DesktopQueryError) as error:
                hotkey_removed = True
                problems.append(str(error))

        try:
            service_removed = self._service.remove()
        except InstallError as error:
            service_removed = False
            problems.append(str(error))

        try:
            self._record_store.remove()
        except OSError as error:
            # Same rule as `_write_hotkey_record`: the record is not something
            # any platform this change targets reads, so a failure to clear it
            # must not turn an otherwise-successful uninstall into a failure.
            problems.append(f"Could not clear the hotkey record: {error}")

        if service_removed:
            messages.append("Removed the Murmly service.")
        if hotkey_removed:
            messages.append("Released the Murmly hotkey.")
        if not service_removed and not hotkey_removed:
            messages.append("Murmly was not installed; there was nothing to remove.")
        messages.extend(problems)

        return InstallOutcome(
            entrypoint=None,
            hotkey=None,
            session_hotkey=None,
            service_installed=False,
            hotkey_registered=False,
            session_hotkey_registered=False,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=tuple(messages),
        )

    def status(self) -> dict[str, object]:
        """Installation state for diagnostics.

        The top-level `hotkey` keys keep describing the focused-window binding,
        unchanged, and `hotkeys` lists every binding with the purpose it serves.
        """
        from murmly.platform import hotkey_mechanism_is_in_process

        if hotkey_mechanism_is_in_process(self._profile):
            return self._status_in_process()

        service = self._service.status()
        declared = self._launcher.declared_hotkey()
        report: dict[str, object] = {
            "installed": service.installed,
            "service_active": service.active,
            "entrypoint": service.entrypoint,
            "hotkey": declared,
            "hotkey_held": False,
            "detail": service.detail,
        }

        override = self._launcher.user_override()
        if override is not None:
            report["hotkey_override"] = override

        report["hotkeys"] = [
            self._hotkey_status(launcher)
            for launcher in (self._launcher, self._session_launcher)
        ]

        if declared is None:
            if service.installed:
                report["detail"] = "The Murmly service is installed but no hotkey is registered."
            return report

        window = report["hotkeys"][0]
        report["hotkey_held"] = window["held"]
        if "holder" in window:
            report["hotkey_holder"] = window["holder"]
        if window.get("detail") is not None:
            report["detail"] = window["detail"]
        return report

    def _hotkey_status(self, launcher) -> dict[str, object]:
        """One binding, judged on its own against its own component."""
        purpose = self._purpose_of(launcher)
        declared = launcher.declared_hotkey()
        override = launcher.user_override()
        entry: dict[str, object] = {
            "purpose": purpose.key,
            "description": purpose.description,
            "command": purpose.command,
            "hotkey": declared,
            "held": False,
            "detail": None,
        }
        if override is not None:
            entry["override"] = override
        if declared is None:
            entry["detail"] = f"No hotkey is bound to {purpose.description}."
            return entry

        try:
            hotkey = parse_hotkey(override or declared)
            owners = self._shortcuts.owners_of(hotkey.keycode)
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            entry["detail"] = f"Unable to check {declared}: {error}"
            return entry

        ours = [owner for owner in owners if owner.component_unique == purpose.desktop_id]
        others = [owner for owner in owners if owner.component_unique != purpose.desktop_id]
        entry["held"] = bool(ours) and not others
        if others:
            names = ", ".join(sorted({owner.label for owner in others}))
            entry["holder"] = names
            entry["detail"] = f"{hotkey.portable} is held by {names}, not Murmly."
        elif not ours:
            entry["detail"] = (
                f"{hotkey.portable} is registered for Murmly but no owner is reported."
            )
        return entry

    # ----------------------------------------------------------------
    # In-process hotkey backends (task 8's Windows registrar and task 13's
    # macOS Carbon registrar). No desktop session, no launcher
    # file, no `_shortcuts.owners_of` query: the binding lives inside the
    # daemon's own process, so the only way to bind, verify or release it is
    # to reach that process over the command channel `self._socket_path()`
    # names. `HotkeyRecordStore` (task 5.4) is what a fresh daemon start reads
    # to re-create the binding, and what these methods themselves read and
    # write as the one record of "what is supposed to be bound" -- there is
    # no desktop-held copy to fall back on the way `declared_hotkey()` is for
    # Plasma and GNOME.
    # ----------------------------------------------------------------

    def _socket_path(self) -> str:
        if self._config is not None:
            return str(self._config.socket_path)
        from murmly.config import default_config_path, load_config

        return str(load_config(default_config_path()).socket_path)

    def _wait_for_daemon_command(self, socket_path: str, command: str) -> dict[str, object]:
        """Send `command`, retrying while the daemon this run just started is
        still coming up, bounded by `self._daemon_timeout`.

        Raises `InstallError` naming the last failure on timeout -- callers
        turn that into `HotkeyNotConfirmedError` rather than a hard failure,
        the same distinction `ShortcutLauncher.register`'s own bounded wait
        draws between "not yet confirmed" and "refused".
        """
        from murmly.daemon import DaemonNotRespondingError, send_command

        deadline = self._clock() + self._daemon_timeout
        last_error: Exception | None = None
        while True:
            try:
                return send_command(socket_path, command)
            except (OSError, DaemonNotRespondingError) as error:
                last_error = error
                if self._clock() >= deadline:
                    raise InstallError(
                        f"The Murmly daemon did not answer {command!r} within "
                        f"{self._daemon_timeout:g} seconds: {last_error}."
                    ) from last_error
                self._sleep(self._poll_interval)

    def _best_effort_rebind(self, socket_path: str) -> None:
        """Ask a running daemon to re-read the record, without raising.

        Used after a conflict is rolled back, to put the daemon's live
        registration back in step with the record this run just restored --
        a failure here is reported, never raised: the record itself is
        already correct, and the daemon's own next start reads it regardless.
        """
        from murmly.daemon import DaemonNotRespondingError, send_command
        from murmly.daemon import COMMAND_REBIND_HOTKEYS

        try:
            send_command(socket_path, COMMAND_REBIND_HOTKEYS)
        except (OSError, DaemonNotRespondingError) as error:
            logger.debug("Could not reach the running daemon to restore its previous hotkeys: %s", error)

    def _restore_record(self, previous_record: dict[str, str]) -> None:
        try:
            if previous_record:
                self._record_store.write(previous_record)
            else:
                self._record_store.remove()
        except OSError as error:
            logger.warning("Could not restore the previous hotkey record: %s", error)

    def _rollback_service(self) -> None:
        try:
            self._service.remove()
        except InstallError:
            logger.warning("Could not remove the Murmly service during rollback.")

    def _in_process_hotkey_encoder(self) -> Callable[[str], object]:
        """The per-portable-text encoder that validates a hotkey for
        whichever in-process platform `self._profile` names.

        Only ever reached from `_install_in_process`, which is itself only
        ever reached when `hotkey_mechanism_is_in_process(self._profile)` is
        true -- today, Windows and macOS -- so macOS is the only case besides
        the Windows default that needs naming here.
        """
        from murmly.platform import OperatingSystem

        if self._profile.operating_system is OperatingSystem.MACOS:
            return macos_hotkey_for_portable
        return windows_hotkey_for_portable

    def _install_in_process(
        self, hotkey: Hotkey, session_hotkey: Hotkey | None
    ) -> InstallOutcome:
        """`install()`'s whole body for a platform that registers the hotkey
        inside the daemon's own process (design.md's "Windows and macOS
        register in Murmly's own process").

        Installation therefore starts the daemon *before* reporting a hotkey
        bound (the `desktop-integration` spec's "Hotkey takes effect in the
        running session"): the record is written, the service is installed
        and started, and only a daemon that reports the requested purposes
        actually held (`COMMAND_STATUS`'s `hotkeys_held`, task 8.6) is
        reported as a successful bind. A refusal the platform itself raises
        (task 8.4 -- `WindowsHotkeyRegistrar.rebind` surfacing
        `RegisterHotKey`'s own collision) leaves no registration behind: the
        record reverts and the daemon is told to pick the previous bindings
        back up, mirroring the desktop-launcher flow's own rollback.
        """
        from murmly.daemon import COMMAND_REBIND_HOTKEYS, COMMAND_STATUS

        entrypoint = self._resolve_entrypoint()

        requested: dict[str, Hotkey] = {WINDOW_HOTKEY.key: hotkey}
        if session_hotkey is not None:
            if session_hotkey.keycode == hotkey.keycode:
                raise HotkeyConflictError(
                    f"{hotkey.portable} was requested for both the focused window and the "
                    "speech session. Only one binding can ever receive the keypress, so "
                    "choose a different key for one of them."
                )
            requested[SESSION_HOTKEY.key] = session_hotkey

        # Refuse anything this platform cannot register at all, before
        # anything is written -- the same "before anything is written" rule
        # the desktop-launcher flow applies to its own two-hotkeys-one-key
        # check just above. Dispatched on the resolved operating system
        # rather than always validating against Windows' own encoder: the two
        # in-process platforms genuinely disagree (macOS's function-key
        # ceiling is F20, Windows' is F24), so a macOS key past F20 would
        # otherwise pass this check and only fail later, inside the daemon,
        # with a less useful error.
        encode_for_platform = self._in_process_hotkey_encoder()
        for purpose_key, requested_hotkey in requested.items():
            try:
                encode_for_platform(requested_hotkey.portable)
            except HotkeyError as error:
                raise InstallError(str(error)) from error

        previous_record = self._record_store.read()
        already_bound = previous_record.get(WINDOW_HOTKEY.key) == hotkey.portable
        new_record = dict(previous_record)
        new_record.update({key: value.portable for key, value in requested.items()})

        service_existed = self._service.is_installed
        self._service.install(entrypoint)

        try:
            self._record_store.write(new_record)
        except OSError as error:
            if not service_existed:
                self._rollback_service()
            raise InstallError(f"Could not persist the hotkey binding: {error}") from error

        socket_path = self._socket_path()
        portables = ", ".join(value.portable for value in requested.values())

        try:
            rebind_response = self._wait_for_daemon_command(socket_path, COMMAND_REBIND_HOTKEYS)
            status_response = self._wait_for_daemon_command(socket_path, COMMAND_STATUS)
        except InstallError as error:
            # The record is left exactly as written: this is "not confirmed
            # in this session", not "refused" -- the same distinction
            # `HotkeyNotConfirmedError`'s own docstring draws for the
            # desktop-launcher flow, whose launcher also stays on this path.
            raise HotkeyNotConfirmedError(
                f"{portables} could not be confirmed in this session: {error} The binding "
                "is saved and will take effect the next time the Murmly service starts.",
                tuple(requested.values()),
            ) from error

        held = set(status_response.get("hotkeys_held") or ())
        missing = requested.keys() - held
        if missing:
            # The platform's own refusal is the collision (task 8.4): nothing
            # was queried first. No registration is left behind (the
            # `desktop-integration` spec's "no service, launcher, or hotkey
            # registration is left behind") -- the record reverts and the
            # still-running daemon is told to pick the previous bindings back
            # up immediately, not only at its next start.
            self._restore_record(previous_record)
            self._best_effort_rebind(socket_path)
            if not service_existed:
                self._rollback_service()
            names = ", ".join(requested[key].portable for key in sorted(missing))
            detail = str(rebind_response.get("detail") or "").strip()
            raise HotkeyConflictError(
                f"{names} could not be bound: {detail or 'the platform refused the registration.'}"
            )

        messages: list[str] = []
        if already_bound:
            messages.append(
                f"{hotkey.portable} is already bound to Murmly ({WINDOW_HOTKEY.description})."
            )
        else:
            messages.append(
                f"Registered {hotkey.portable}. It is held by the running Murmly daemon: the "
                "binding exists only while the daemon runs, and is released if it stops."
            )
        if session_hotkey is not None:
            messages.append(
                f"Registered {session_hotkey.portable} to {SESSION_HOTKEY.description}. It is "
                "held by the running Murmly daemon."
            )
        messages.extend(self._paste_injection_messages())

        return InstallOutcome(
            entrypoint=entrypoint,
            hotkey=hotkey,
            session_hotkey=session_hotkey,
            service_installed=True,
            hotkey_registered=True,
            session_hotkey_registered=session_hotkey is not None,
            already_bound=already_bound,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=tuple(messages),
        )

    def _uninstall_in_process(self) -> InstallOutcome:
        messages: list[str] = []
        problems: list[str] = []

        had_record = bool(self._record_store.read())
        try:
            self._record_store.remove()
        except OSError as error:
            problems.append(f"Could not clear the hotkey record: {error}")

        # Best effort: drop what a still-running daemon holds right now,
        # rather than only removing what a future start would have read.
        # `remove()` below is about to stop that daemon anyway, so an
        # unreachable one here is not a problem to report.
        self._best_effort_rebind(self._socket_path())

        try:
            service_removed = self._service.remove()
        except InstallError as error:
            service_removed = False
            problems.append(str(error))

        if service_removed:
            messages.append("Removed the Murmly service.")
        if had_record:
            messages.append("Released the Murmly hotkey.")
        if not service_removed and not had_record:
            messages.append("Murmly was not installed; there was nothing to remove.")
        messages.extend(problems)

        return InstallOutcome(
            entrypoint=None,
            hotkey=None,
            session_hotkey=None,
            service_installed=False,
            hotkey_registered=False,
            session_hotkey_registered=False,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=tuple(messages),
        )

    def _held_purposes_from_daemon(self) -> frozenset[str] | None:
        """`None` when this cannot be determined -- distinct from an empty
        set, which means the daemon answered and holds nothing (18.13's
        "undetermined rather than granted", applied to a live query instead
        of a permission check)."""
        from murmly.daemon import COMMAND_STATUS, DaemonNotRespondingError, send_command

        try:
            response = send_command(
                self._socket_path(), COMMAND_STATUS, connect_timeout=1.0, response_timeout=3.0
            )
        except (OSError, DaemonNotRespondingError):
            return None
        held = response.get("hotkeys_held")
        return None if held is None else frozenset(held)

    def _status_in_process(self) -> dict[str, object]:
        """`status()`'s whole body for an in-process hotkey backend.

        Task 8.6/8.7 and the `desktop-integration` spec's own scenarios: held
        by the running daemon when it is running and reports the purpose held
        (`COMMAND_STATUS`'s `hotkeys_held`); not held, naming the daemon and
        the command that starts it, whenever the daemon is not running or
        cannot be reached -- there is no keypress to recover it the way a
        stopped Linux daemon has, so the report has to say what will.
        """
        from murmly.platform import OperatingSystem

        service = self._service.status()
        record = self._record_store.read()
        held = self._held_purposes_from_daemon() if service.active else frozenset()

        starter = getattr(self._service, "start_command_text", None)
        start_hint = starter() if starter is not None else "the command that starts the Murmly service"

        entries: list[dict[str, object]] = []
        for purpose in (WINDOW_HOTKEY, SESSION_HOTKEY):
            declared = record.get(purpose.key)
            entry: dict[str, object] = {
                "purpose": purpose.key,
                "description": purpose.description,
                "command": purpose.command,
                "hotkey": declared,
                "held": False,
                "detail": None,
            }
            if declared is None:
                entry["detail"] = f"No hotkey is bound to {purpose.description}."
            elif not service.active:
                entry["detail"] = (
                    f"{declared} is not currently held: it is registered inside the Murmly "
                    f"daemon's own process, and the daemon is not running. Start it with "
                    f"'{start_hint}'."
                )
            elif held is None:
                entry["detail"] = f"Unable to confirm whether {declared} is currently held."
            elif purpose.key in held:
                entry["held"] = True
                entry["detail"] = f"{declared} is held by the running Murmly daemon."
            else:
                entry["detail"] = f"{declared} is not currently held by the running Murmly daemon."
            entries.append(entry)

        report: dict[str, object] = {
            "installed": service.installed,
            "service_active": service.active,
            "entrypoint": service.entrypoint,
            "hotkey": record.get(WINDOW_HOTKEY.key),
            "hotkey_held": entries[0]["held"],
            "detail": service.detail,
            "hotkeys": entries,
        }
        if entries[0].get("detail") is not None:
            report["detail"] = entries[0]["detail"]
        if not service.installed:
            report["detail"] = "Murmly is not installed. Run 'murmly install <hotkey>' to install it."
        if self._profile.operating_system is OperatingSystem.MACOS:
            report["hotkey_mechanism_limitation"] = MACOS_HOTKEY_MECHANISM_LIMITATION
        return report
