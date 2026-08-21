"""Installing Murmly into a desktop session.

Two pieces of state, both owned by Murmly and both removable:

* a systemd user unit anchored on ``graphical-session.target``, so the daemon
  runs for the lifetime of the graphical session rather than from boot, and
* a launcher entry carrying ``X-KDE-Shortcuts``, which is how the hotkey is
  registered.

Murmly writes nothing else. In particular it never edits the user's global
shortcut configuration; see ``docs/agent-notes/plasma-global-shortcut-binding.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from murmly.desktop import PlasmaShortcuts
from murmly.hotkey import Hotkey, HotkeyError, parse_hotkey
from murmly.integrations import PasteInjection, select_paste_injection


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

    candidate = interpreter.with_name(ENTRYPOINT_NAME)
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
    """
    return SERVICE_UNIT_TEMPLATE.format(exec_start=entrypoint)


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


def launcher_text(
    entrypoint: Path,
    hotkey: Hotkey,
    name: str = APPLICATION_NAME,
    command: str = "toggle",
) -> str:
    """The launcher body.

    A literal ``%`` is doubled, matching how the desktop's own shortcut editor
    escapes an Exec value.
    """
    exec_line = f"{entrypoint} {command}".replace("%", "%%")
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
    """Orchestrates the service and the hotkey as one operation."""

    def __init__(
        self,
        service: UserService | None = None,
        launcher: ShortcutLauncher | None = None,
        shortcuts: PlasmaShortcuts | None = None,
        session=None,
        entrypoint_resolver: Callable[[], Path] = resolve_entrypoint,
        injection_selector: Callable[[], PasteInjection] = select_paste_injection,
        session_launcher: ShortcutLauncher | None = None,
    ) -> None:
        self._shortcuts = shortcuts if shortcuts is not None else PlasmaShortcuts()
        self._service = service if service is not None else UserService()
        self._launcher = launcher if launcher is not None else ShortcutLauncher(self._shortcuts)
        self._session_launcher = (
            session_launcher
            if session_launcher is not None
            else ShortcutLauncher(self._shortcuts, purpose=SESSION_HOTKEY)
        )
        self._session = session
        self._resolve_entrypoint = entrypoint_resolver
        self._select_injection = injection_selector

    def _current_session(self):
        if self._session is not None:
            return self._session
        from murmly.desktop import detect_desktop_session

        return detect_desktop_session()

    def install(self, hotkey: Hotkey, session_hotkey: Hotkey | None = None) -> InstallOutcome:
        """Install the service and bind one or both hotkeys.

        The second is optional. An installation performed without it binds the
        focused-window hotkey alone and leaves speech output reachable only by a
        sender that opens a session itself, which is what an existing
        installation upgrading in place gets.
        """
        entrypoint = self._resolve_entrypoint()
        session = self._current_session()
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
        for launcher, requested_hotkey in requested:
            self._refuse_conflict(launcher, requested_hotkey)

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
                # The launcher stays: the binding is persisted for next login.
                unconfirmed.append(requested_hotkey)
            except Exception:
                self._rollback(service_existed, written)
                raise

        if unconfirmed:
            keys = ", ".join(key.portable for key in unconfirmed)
            raise HotkeyNotConfirmedError(
                f"The desktop did not register {keys} within "
                f"{REGISTRATION_TIMEOUT_SECONDS:g} seconds.",
                tuple(unconfirmed),
            )

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

    def _refuse_conflict(self, launcher, hotkey: Hotkey) -> None:
        purpose = self._purpose_of(launcher)
        owners = self._shortcuts.owners_of(hotkey.keycode)
        foreign = [owner for owner in owners if owner.component_unique != purpose.desktop_id]
        if not foreign:
            return
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
        failure never touched.
        """
        for launcher in written:
            try:
                launcher.unregister()
            except InstallError:
                logger.warning("Could not remove a Murmly launcher during rollback.")
        if not service_existed:
            try:
                self._service.remove()
            except InstallError:
                logger.warning("Could not remove the Murmly service during rollback.")

    def uninstall(self) -> InstallOutcome:
        """Remove everything Murmly installed, tolerating anything already gone."""
        messages: list[str] = []
        problems: list[str] = []

        # Each binding released on its own, so an installation carrying only
        # one of them succeeds and does not report the absent one as a failure.
        hotkey_removed = False
        for launcher in (self._launcher, self._session_launcher):
            try:
                hotkey_removed = launcher.unregister() or hotkey_removed
            except InstallError as error:
                hotkey_removed = True
                problems.append(str(error))

        try:
            service_removed = self._service.remove()
        except InstallError as error:
            service_removed = False
            problems.append(str(error))

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
