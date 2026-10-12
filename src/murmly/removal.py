"""Taking Murmly off a computer: what `murmly uninstall` and the app both run.

`remove_murmly` is the one function. It runs `Installer.uninstall`, which removes
the service, the keys, the launchers and the app's menu entry, and then, only when
asked, deletes Murmly's own settings and downloaded files. `murmly uninstall` and
the app's "Remove murmly" call this and nothing else, so the two cannot drift.

Deleting settings and downloaded files is never a default. The caller passes
`purge=True` after a person has chosen it by name, and the paths it deletes are
the ones `purge_targets` returns, so a confirmation can show them first.

No Qt here, so every rule is tested on every platform.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from murmly.config import default_config_path, default_data_dir
from murmly.installer import InstalledItem, InstallOutcome, Installer

#: Folders inside the data directory that a purge leaves alone. The announcement
#: hook's script lives in `hooks`, and the coding assistants' own settings still
#: point at it until `setup.sh uninstall` unregisters it; deleting the script
#: first would make every assistant turn report a failed hook.
KEEP_IN_DATA_DIR = ("hooks",)

#: Every directory Murmly creates is named this, and a purge refuses any other
#: name, so a mistaken setting can never aim it at a person's home folder.
OWN_DIRECTORY_NAME = "murmly"


PURGE_SKIPPED = (
    "Settings and downloaded files were kept: the Murmly service could not be removed "
    "and may still be using them."
)


@dataclass(frozen=True, slots=True)
class PurgeTarget:
    """A folder a purge empties: what is in it, in words, and where it is."""

    label: str
    path: Path
    #: Names directly inside the folder that are kept.
    keep: tuple[str, ...] = ()

    @property
    def exists(self) -> bool:
        return self.path.is_dir()


def purge_targets(env: dict[str, str] | None = None) -> tuple[PurgeTarget, ...]:
    """The folders a purge deletes: the settings and the downloaded models.

    The settings folder holds the configuration file and the record of the keys.
    The data folder holds the downloaded synthesis models, the speaker model and
    enrolled voices. On macOS both are one folder, which is then listed once.
    """
    config_dir = default_config_path(env).parent
    data_dir = default_data_dir(env)
    if config_dir == data_dir:
        return (
            PurgeTarget(
                "Settings, downloaded models and enrolled voices", data_dir, KEEP_IN_DATA_DIR
            ),
        )
    return (
        PurgeTarget("Settings", config_dir),
        PurgeTarget("Downloaded models and enrolled voices", data_dir, KEEP_IN_DATA_DIR),
    )


@dataclass(frozen=True, slots=True)
class PurgeResult:
    removed: tuple[Path, ...] = ()
    kept: tuple[Path, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def messages(self) -> tuple[str, ...]:
        lines = [f"Deleted {path}." for path in self.removed]
        lines += [f"Left {path} in place." for path in self.kept]
        return tuple(lines) + self.problems


def refusal(path: Path) -> str | None:
    """Why a purge must not delete `path`, or None when every rule allows it.

    Checked by the purge itself, on top of where the folders were resolved from, so a
    wrong setting upstream still cannot aim a deletion at a person's own folders.
    """
    if not path.is_absolute():
        return "it is not an absolute path."
    if path == Path(path.anchor) or path.parent == path:
        return "it is a root folder."
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    if path == home:
        return "it is the home folder."
    if path.name != OWN_DIRECTORY_NAME:
        return "it is not a folder Murmly made."
    # A junction is Windows' link between folders and is not a symbolic link.
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return "it is a link, not a folder."
    return None


def purge(
    targets: tuple[PurgeTarget, ...],
    rmtree: Callable[[Path], None] = shutil.rmtree,
) -> PurgeResult:
    """Delete what `targets` name. Never raises: a file that cannot go is reported.

    A folder that is already gone is not a problem. A kept name is skipped, and the
    folder is then not removed itself, only emptied.
    """
    removed: list[Path] = []
    kept: list[Path] = []
    problems: list[str] = []
    for target in targets:
        path = target.path
        reason = refusal(path)
        if reason is not None:
            problems.append(f"Did not delete {path}: {reason}")
            continue
        if not path.is_dir():
            continue
        try:
            children = sorted(path.iterdir())
        except OSError as error:
            problems.append(f"Could not read {path}: {error}")
            continue
        protected = [child for child in children if child.name in target.keep]
        for child in children:
            if child in protected:
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    rmtree(child)
                else:
                    child.unlink(missing_ok=True)
            except OSError as error:
                problems.append(f"Could not delete {child}: {error}")
        if protected:
            kept.extend(protected)
            continue
        try:
            path.rmdir()
        except OSError as error:
            problems.append(f"Could not delete {path}: {error}")
            continue
        removed.append(path)
    return PurgeResult(tuple(removed), tuple(kept), tuple(problems))


@dataclass(frozen=True, slots=True)
class RemovalPlan:
    """What a removal would take away, read before anything is removed."""

    items: tuple[InstalledItem, ...]
    purge_targets: tuple[PurgeTarget, ...]


def plan_removal(installer: Installer, env: dict[str, str] | None = None) -> RemovalPlan:
    """What `remove_murmly` would remove: the installer's own list, and the purge folders."""
    return RemovalPlan(tuple(installer.installed_items()), purge_targets(env))


@dataclass(frozen=True, slots=True)
class RemovalResult:
    """What a removal did: the installer's report, then the purge's when one ran."""

    outcome: InstallOutcome
    purged: PurgeResult | None = None
    #: True when a purge was asked for and not run, because the service was not removed.
    purge_skipped: bool = False

    @property
    def failed(self) -> bool:
        """Whether any part of the removal, or of the purge, did not happen."""
        purge_problems = self.purged.problems if self.purged is not None else ()
        return bool(self.outcome.problems or purge_problems or self.purge_skipped)

    @property
    def messages(self) -> tuple[str, ...]:
        lines = tuple(self.outcome.messages)
        if self.purged is not None:
            lines += self.purged.messages
        if self.purge_skipped:
            lines += (PURGE_SKIPPED,)
        return lines


def remove_murmly(
    installer: Installer,
    purge_user_files: bool = False,
    env: dict[str, str] | None = None,
) -> RemovalResult:
    """Run `murmly uninstall`, and delete settings and models only when asked.

    `InstallError` from the uninstall propagates and nothing is purged: files a
    person asked to be deleted along with a removal that did not happen stay. So
    they do when the service could not be removed, since it may still be running
    with those files open.
    """
    outcome = installer.uninstall()
    if purge_user_files and outcome.service_removal_failed:
        return RemovalResult(outcome, None, purge_skipped=True)
    purged = purge(purge_targets(env)) if purge_user_files else None
    return RemovalResult(outcome, purged)
