"""Linux system packages Murmly's optional features need, and how to name them.

`setup.sh` names these packages by their `dnf` spelling and installs them only
when `dnf` is on the machine (`setup.sh:143-200`); everywhere else it prints
the list and leaves installation to the person reading it. That is correct as
far as it goes -- an unrecognised package manager should not guess at a
command -- but it goes no further than Fedora, and section 16's installer
needs to go further without setup.sh's bash growing four more copies of the
same `if`.

This module is what section 16 calls. It is deliberately not wired into
`setup.sh` itself: rewriting the installer in bash to call out to a Python
module it does not otherwise depend on would be a stranger migration than
leaving `setup.sh` exactly as it is until section 16 replaces it outright, per
`design.md`'s "Installation: `murmly install` grows, `setup.sh` shrinks to a
bootstrap".

Package manager detection and the per-manager package names below are the two
pieces `wanted_system_packages()` (`setup.sh:148-169`) already computes for
`dnf`; nothing here changes what that function decides for a Fedora machine.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import shutil
import subprocess


WhichCommand = Callable[[str], str | None]

#: Spelled the same way `environment.py` spells it, and for the same reason:
#: every probe in this codebase takes its command runner as a parameter so a
#: test can name a machine's state without the machine being in it.
RunCommand = Callable[..., "subprocess.CompletedProcess[str]"]

#: Checked in this order and stopped at the first match. A machine normally
#: has exactly one of these on its PATH; where more than one happens to be
#: present -- a compatibility shim, a container image layering two package
#: managers -- this makes the answer the same one every time rather than
#: whatever order `PATH` or dict iteration happens to produce.
PACKAGE_MANAGERS: tuple[str, ...] = ("dnf", "apt", "pacman", "zypper", "apk")

#: The command each manager installs packages with, before the package names
#: are appended. `apt` and `zypper` both want `install` verbatim like `dnf`;
#: `pacman` and `apk` spell it differently. `-y` (`dnf`, `apt`) and
#: `--noconfirm` (`pacman`) are included because a command a person is asked to
#: run once, by hand, still should not stop mid-way through five packages to
#: ask about the sixth -- the same reasoning `setup.sh:194-199` already applies
#: to its own `dnf` command, just once per manager instead of once.
_INSTALL_COMMAND_PREFIX: dict[str, tuple[str, ...]] = {
    "dnf": ("sudo", "dnf", "install", "-y"),
    "apt": ("sudo", "apt", "install", "-y"),
    "pacman": ("sudo", "pacman", "-S", "--noconfirm"),
    "zypper": ("sudo", "zypper", "install", "-y"),
    "apk": ("sudo", "apk", "add"),
}

#: How each manager is asked whether one package is already installed, before
#: the package name is appended. Exit status alone is the answer -- every one
#: of these exits non-zero for a package that is not installed -- so nothing
#: here parses output, which is the part that would differ per manager in ways
#: this table could not hold.
#:
#: This exists because dropping it was a regression rather than a
#: simplification. `setup.sh` diffed against `rpm -q` and printed "Everything
#: Murmly uses is already installed" when nothing was missing; generalising
#: that step past `dnf` briefly lost the diff, which meant every `install` and
#: `upgrade` offered the whole list, and `--yes` then *ran* it -- a `sudo`
#: prompt, and a package manager invocation, on an upgrade that used to need
#: neither.
#:
#: `zypper` is queried through `rpm` because that is the database it manages,
#: exactly as `dnf` is. `apt` is queried through `dpkg-query` for the same
#: reason. Neither is a fifth divergent mechanism; each is one row, the same
#: shape as its install command above.
_QUERY_COMMAND_PREFIX: dict[str, tuple[str, ...]] = {
    "dnf": ("rpm", "-q"),
    "apt": ("dpkg-query", "-W"),
    "pacman": ("pacman", "-Q"),
    "zypper": ("rpm", "-q"),
    "apk": ("apk", "info", "-e"),
}

#: One row per package Murmly's optional Linux features use, spelled per
#: package manager. The `dnf` column is `setup.sh`'s own
#: `wanted_system_packages()` list (`setup.sh:148-169`) and is confirmed
#: correct by that script having installed from it; `portaudio` there is
#: confirmed separately by `docs/agent-notes/portaudio-jack-exit-abort.md`,
#: which records `portaudio-19.7.0-3.fc44` on the machine this was written on.
#:
#: The `apt` column's `libportaudio2` is what task 3.5 names by hand: PortAudio
#: itself, needed because `sounddevice` bundles it into its own wheel on
#: Windows and macOS but not on Linux (`design.md`'s dependency table).
#: `gir1.2-gtk-4.0` (the GTK4 typelib) and `python3-gi` (PyGObject) are named
#: separately because Debian and Ubuntu split what Fedora's single `gtk4` and
#: `python3-gobject` packages each provide.
#:
#: The `pacman`, `zypper` and `apk` columns are inferred from each
#: distribution's own naming convention, not independently verified against a
#: running machine the way the `dnf` column is -- Arch, openSUSE and Alpine
#: package names occasionally drift from the upstream project name in ways a
#: table like this cannot catch without installing on one. A name that turns
#: out to be wrong there is a `doctor`/install-time report to correct, not a
#: silent failure: nothing here claims a package exists on the machine, only
#: what to ask the package manager for.
_PACKAGE_NAMES: dict[str, dict[str, str]] = {
    "gtk4": {
        "dnf": "gtk4",
        "apt": "gir1.2-gtk-4.0",
        "pacman": "gtk4",
        "zypper": "gtk4",
        "apk": "gtk4.0",
    },
    "python3-gobject": {
        "dnf": "python3-gobject",
        "apt": "python3-gi",
        "pacman": "python-gobject",
        "zypper": "python3-gobject",
        "apk": "py3-gobject3",
    },
    "libX11": {
        "dnf": "libX11",
        "apt": "libx11-6",
        "pacman": "libx11",
        "zypper": "libX11-6",
        "apk": "libx11",
    },
    "libXext": {
        "dnf": "libXext",
        "apt": "libxext6",
        "pacman": "libxext",
        "zypper": "libXext6",
        "apk": "libxext",
    },
    "wl-clipboard": {
        "dnf": "wl-clipboard",
        "apt": "wl-clipboard",
        "pacman": "wl-clipboard",
        "zypper": "wl-clipboard",
        "apk": "wl-clipboard",
    },
    "gtk4-layer-shell": {
        "dnf": "gtk4-layer-shell",
        "apt": "gir1.2-gtk4layershell-1.0",
        "pacman": "gtk4-layer-shell",
        "zypper": "gtk4-layer-shell",
        "apk": "gtk4-layer-shell",
    },
    "xdotool": {
        "dnf": "xdotool",
        "apt": "xdotool",
        "pacman": "xdotool",
        "zypper": "xdotool",
        "apk": "xdotool",
    },
    "wtype": {
        "dnf": "wtype",
        "apt": "wtype",
        "pacman": "wtype",
        "zypper": "wtype",
        "apk": "wtype",
    },
    "xclip": {
        "dnf": "xclip",
        "apt": "xclip",
        "pacman": "xclip",
        "zypper": "xclip",
        "apk": "xclip",
    },
    "espeak-ng": {
        "dnf": "espeak-ng",
        "apt": "espeak-ng",
        "pacman": "espeak-ng",
        "zypper": "espeak-ng",
        "apk": "espeak-ng",
    },
    # Task 3.5: `sounddevice` bundles PortAudio into its own wheel on Windows
    # and macOS but not on Linux, so this is the one package on this list that
    # nothing else in the dependency tree pulls in.
    "portaudio": {
        "dnf": "portaudio",
        "apt": "libportaudio2",
        "pacman": "portaudio",
        "zypper": "portaudio",
        "apk": "portaudio",
    },
}

#: Package roles every session needs, regardless of display protocol or
#: desktop -- the overlay's own toolkit and the X11 libraries the paste and
#: focus code link against on every session type, plus PortAudio for capture
#: and playback. Mirrors the base of `setup.sh`'s own
#: `wanted_system_packages()` (`setup.sh:149`), with `portaudio` added -- see
#: task 3.5 and the "Deviations" note in this change's return value: the shipped
#: script names no PortAudio package at all today.
_BASE_ROLES: tuple[str, ...] = ("gtk4", "python3-gobject", "libX11", "libXext", "portaudio")


@dataclass(frozen=True, slots=True)
class SystemPackages:
    """What to install, and how to ask a recognised package manager to."""

    #: One of `PACKAGE_MANAGERS`, or None where none was found on `PATH`.
    manager: str | None
    #: Package names in this manager's own spelling, in a fixed, readable
    #: order -- never a `set`, which would print the same list differently on
    #: every run.
    names: tuple[str, ...]
    #: The full command that would install `names`, or None where `manager` is
    #: None: printing a command built from no manager at all would be a
    #: command for whatever package manager happens to run it, which is
    #: exactly the guess `setup.sh:174-178` already refuses to make.
    command: tuple[str, ...] | None


def detect_package_manager(which: WhichCommand = shutil.which) -> str | None:
    """Which of `PACKAGE_MANAGERS` is on this machine's `PATH`, or None.

    `which` is a parameter, not a call baked in, for the same reason every
    other platform probe in this codebase takes its command runner as one: a
    test names a machine's package manager without needing that manager
    installed on the machine running the test.
    """
    for manager in PACKAGE_MANAGERS:
        if which(manager) is not None:
            return manager
    return None


def wanted_packages(
    *,
    wayland: bool,
    plasma: bool,
    speech_output: bool,
) -> tuple[str, ...]:
    """Which package roles this session needs, in the naming table's own keys.

    The same decision `setup.sh`'s `wanted_system_packages()` makes
    (`setup.sh:148-169`), kept separate from the per-manager spelling below so
    a future session variant (a fourth display protocol, say) changes one
    function rather than five package-manager columns.
    """
    roles = list(_BASE_ROLES)
    if wayland:
        roles.append("wl-clipboard")
        if plasma:
            # KWin bridges XTEST through libei, so an X11 tool reaches
            # Wayland-native windows; layer shell is what places the overlay.
            roles += ["gtk4-layer-shell", "xdotool"]
        else:
            roles.append("wtype")
    else:
        roles += ["xclip", "xdotool"]
    if speech_output:
        roles.append("espeak-ng")
    return tuple(roles)


def install_command(manager: str, names: tuple[str, ...]) -> tuple[str, ...]:
    """This manager's install command for exactly `names`.

    Separate from `system_packages`, which builds the command for everything a
    session wants: what actually gets offered is the subset `missing_packages`
    says is absent, and offering a command for packages already installed is
    what this exists to stop.
    """
    return _INSTALL_COMMAND_PREFIX[manager] + names


def missing_packages(
    manager: str,
    names: tuple[str, ...],
    run_command: RunCommand,
) -> tuple[str, ...]:
    """Which of `names` this manager does not already have installed.

    Every package whose query cannot be run at all is treated as missing, not
    as present: offering to install something already there costs a person one
    declined prompt, while skipping something absent costs them a feature that
    silently does not work.

    Returns `names` unchanged for a manager with no query row, which is the
    honest answer -- "could not tell" -- rather than a claim either way.
    """
    query = _QUERY_COMMAND_PREFIX.get(manager)
    if query is None:
        return names
    missing = []
    for name in names:
        try:
            completed = run_command([*query, name], check=False, capture_output=True)
        except OSError:
            # The query tool itself is absent -- `dpkg-query` on a machine
            # with `apt` but a broken path, say. Cannot tell, so offer it.
            missing.append(name)
            continue
        if completed.returncode != 0:
            missing.append(name)
    return tuple(missing)


def system_packages(
    *,
    wayland: bool,
    plasma: bool,
    speech_output: bool,
    which: WhichCommand = shutil.which,
) -> SystemPackages:
    """What this session needs, and how to ask this machine's manager for it.

    Where no manager in `PACKAGE_MANAGERS` is found, `names` is still populated
    -- printed plainly, per task 3.4, which is what `setup.sh` already does
    when `dnf` is absent (`setup.sh:174-178`) -- and `command` is None rather
    than a guess at a command line for a manager that was never identified.
    """
    manager = detect_package_manager(which)
    roles = wanted_packages(wayland=wayland, plasma=plasma, speech_output=speech_output)
    if manager is None:
        # No manager's spelling to prefer, so the role names themselves are
        # what gets printed -- the same names `setup.sh`'s own dnf column
        # uses, since every role above is named for its Fedora package.
        return SystemPackages(manager=None, names=roles, command=None)
    names = tuple(_PACKAGE_NAMES[role][manager] for role in roles)
    command = install_command(manager, names)
    return SystemPackages(manager=manager, names=names, command=command)


def has_nvidia_gpu(which: WhichCommand = shutil.which) -> bool:
    """Whether an NVIDIA GPU is likely present, asked in a way every platform answers.

    `setup.sh`'s own check (`setup.sh:121-123`) is `/proc/driver/nvidia/version`
    exists, or `nvidia-smi` is on `PATH` -- and the first half is Linux-only by
    construction. `nvidia-smi` is not: the NVIDIA driver installer places it on
    `PATH` on Windows as well, and neither `torch` nor `pynvml` becomes a
    dependency to ask the same question through an import instead.

    Presence on `PATH` only, exactly as `setup.sh`'s `have nvidia-smi` already
    checks (`setup.sh:114,122`) -- this does not run it. A machine with the
    kernel driver but not the package that ships the tool (the base `nvidia`
    driver without `nvidia-utils` or the CUDA package, say) answers False here
    the same way `setup.sh`'s own `have nvidia-smi` half already would; the
    `/proc` check being removed only drops the other half, which no other
    platform can share.
    """
    return which("nvidia-smi") is not None


__all__ = [
    "PACKAGE_MANAGERS",
    "SystemPackages",
    "WhichCommand",
    "detect_package_manager",
    "has_nvidia_gpu",
    "system_packages",
    "wanted_packages",
]
