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
from murmly.hotkey import Hotkey, parse_hotkey
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
    """


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


def launcher_text(entrypoint: Path, hotkey: Hotkey, name: str = APPLICATION_NAME) -> str:
    """The launcher body.

    A literal ``%`` is doubled, matching how the desktop's own shortcut editor
    escapes an Exec value.
    """
    exec_line = f"{entrypoint} toggle".replace("%", "%%")
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
    ) -> None:
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
    def launcher_path(self) -> Path:
        return self._applications_dir / DESKTOP_ID

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
        header = f"[services][{DESKTOP_ID}]"
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
        write_atomically(self.launcher_path, launcher_text(entrypoint, hotkey))
        self._rebuild_cache()
        if not self._wait_until(lambda: self._shortcuts.component_exists(DESKTOP_ID)):
            raise HotkeyNotConfirmedError(
                f"The desktop did not register {hotkey.portable} within "
                f"{self._timeout:g} seconds."
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
        if not self._wait_until(lambda: not self._shortcuts.component_exists(DESKTOP_ID)):
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
    ) -> None:
        self._shortcuts = shortcuts if shortcuts is not None else PlasmaShortcuts()
        self._service = service if service is not None else UserService()
        self._launcher = launcher if launcher is not None else ShortcutLauncher(self._shortcuts)
        self._session = session
        self._resolve_entrypoint = entrypoint_resolver
        self._select_injection = injection_selector

    def _current_session(self):
        if self._session is not None:
            return self._session
        from murmly.desktop import detect_desktop_session

        return detect_desktop_session()

    def install(self, hotkey: Hotkey) -> InstallOutcome:
        entrypoint = self._resolve_entrypoint()
        session = self._current_session()
        messages: list[str] = []

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
                service_installed=True,
                hotkey_registered=False,
                already_bound=False,
                session_supported=False,
                session_verified=False,
                user_override=None,
                messages=tuple(messages),
            )

        if not session.verified:
            messages.append(session.detail)

        # Refuse a conflict before writing anything, so a refusal leaves no
        # partial state behind.
        owners = self._shortcuts.owners_of(hotkey.keycode)
        foreign = [owner for owner in owners if owner.component_unique != DESKTOP_ID]
        if foreign:
            names = ", ".join(sorted({owner.label for owner in foreign}))
            raise HotkeyConflictError(
                f"{hotkey.portable} is already used by {names}. Choose a different hotkey, "
                "or release that one in your desktop shortcut settings first."
            )

        already_bound = bool(owners) and self._launcher.declared_hotkey() == hotkey.portable

        service_existed = self._service.is_installed
        self._service.install(entrypoint)

        if already_bound:
            messages.append(f"{hotkey.portable} is already bound to Murmly.")
        else:
            try:
                self._launcher.register(entrypoint, hotkey)
                self._verify(hotkey)
            except HotkeyNotConfirmedError:
                # The launcher stays: the binding is persisted for next login.
                raise
            except Exception:
                self._rollback(service_existed)
                raise

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
        messages.extend(self._paste_injection_messages())
        return InstallOutcome(
            entrypoint=entrypoint,
            hotkey=hotkey,
            service_installed=True,
            hotkey_registered=True,
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

    def _verify(self, hotkey: Hotkey) -> None:
        """Confirm the desktop resolved the launcher to the intended key.

        The key-code comparison checks Murmly's own key table against the
        desktop's parser, so a wrong constant fails here rather than silently
        binding something else.
        """
        registered = self._shortcuts.registered_keys(DESKTOP_ID)
        if registered != [hotkey.keycode]:
            raise InstallError(
                f"The desktop registered a different hotkey than requested. Asked for "
                f"{hotkey.portable} ({hotkey.keycode}), got {registered or 'nothing'}."
            )

        owners = self._shortcuts.owners_of(hotkey.keycode)
        others = [owner for owner in owners if owner.component_unique != DESKTOP_ID]
        if others:
            names = ", ".join(sorted({owner.label for owner in others}))
            raise InstallError(
                f"{hotkey.portable} is claimed by both Murmly and {names}. The desktop "
                "delivers such a key to whichever claimed it first, so Murmly would never "
                "receive it."
            )
        if not owners:
            raise InstallError(f"The desktop reports no owner for {hotkey.portable} after registering it.")

    def _rollback(self, service_existed: bool) -> None:
        """Undo what this run created, leaving anything pre-existing alone."""
        try:
            self._launcher.unregister()
        except InstallError:
            logger.warning("Could not remove the Murmly launcher during rollback.")
        if not service_existed:
            try:
                self._service.remove()
            except InstallError:
                logger.warning("Could not remove the Murmly service during rollback.")

    def uninstall(self) -> InstallOutcome:
        """Remove everything Murmly installed, tolerating anything already gone."""
        messages: list[str] = []
        problems: list[str] = []

        try:
            hotkey_removed = self._launcher.unregister()
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
            service_installed=False,
            hotkey_registered=False,
            already_bound=False,
            session_supported=True,
            session_verified=True,
            user_override=None,
            messages=tuple(messages),
        )

    def status(self) -> dict[str, object]:
        """Installation state for diagnostics."""
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

        if declared is None:
            if service.installed:
                report["detail"] = "The Murmly service is installed but no hotkey is registered."
            return report

        try:
            hotkey = parse_hotkey(override or declared)
            owners = self._shortcuts.owners_of(hotkey.keycode)
        except Exception as error:  # noqa: BLE001 - diagnostics must not raise
            report["detail"] = f"Unable to check the Murmly hotkey: {error}"
            return report

        ours = [owner for owner in owners if owner.component_unique == DESKTOP_ID]
        others = [owner for owner in owners if owner.component_unique != DESKTOP_ID]
        report["hotkey_held"] = bool(ours) and not others
        if others:
            names = ", ".join(sorted({owner.label for owner in others}))
            report["hotkey_holder"] = names
            report["detail"] = f"{hotkey.portable} is held by {names}, not Murmly."
        elif not ours:
            report["detail"] = f"{hotkey.portable} is registered for Murmly but no owner is reported."
        return report
