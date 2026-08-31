"""The two things `setup.sh` exists to get right, moved where they are testable.

`setup.sh`'s own header names them: carrying the extras already installed
across every `uv sync` so a plain sync never silently drops the CUDA wheels or
speech output, and reapplying the GPU build of ONNX Runtime that every sync
puts back to the CPU one. Both are `uv sync`'s own behaviour -- it matches the
environment exactly to the extras and groups it is given -- and neither varies
by platform, which is why `design.md`'s "Installation: `murmly install` grows,
`setup.sh` shrinks to a bootstrap" moves them into `murmly` rather than into a
fourth shell script.

This module is what `cli.py`'s `sync` subcommand calls (task 16.1). It also
gains the pre-sync gates task 16.3 and 11.5/11.6 ask for -- a refusal or a
warning read before `uv sync` runs, where it can still change anything, rather
than after -- and 3.4's caller: `packages.system_packages` detects the
package manager and names its packages, and `install_system_packages` below
is what actually offers to run its command, the same offer `setup.sh`'s own
`install_system_packages` (`setup.sh:181-206`) made for `dnf` alone.

Every `uv`/package-manager invocation here takes `run_command` as a parameter,
the same seam `packages.py`'s `which` parameter and every backend registry's
`run_command` already use: a test drives the extras-resolution and the GPU
swap against a fake recording what it was asked to run, never against a real
`uv` or a real package manager. `setup.sh` keeps the responsibilities this
module does not take on: pulling the source, the synthesis model download,
the announcement hooks, restarting the service, and every subcommand's own
argument parsing (`command_install`, `command_upgrade`, `command_hooks`,
`command_uninstall`) -- none of that is `uv sync`'s behaviour, and none of it
varies by platform in the way the two problems above do.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from murmly.packages import (
    WhichCommand,
    has_nvidia_gpu,
    install_command,
    missing_packages,
    system_packages,
)

if TYPE_CHECKING:
    from murmly.platform import EnvironmentPrecondition, PlatformProfile

RunCommand = Callable[..., "subprocess.CompletedProcess[str]"]
#: A yes/no question, answered `True` or `False`. Never raises: the three
#: outcomes `setup.sh`'s own `confirm()` distinguishes (assumed yes, declined
#: for want of a terminal, and an actual answer) are folded into this one
#: boolean here, same as they are there -- the caller only ever needs to know
#: whether to proceed.
Confirm = Callable[[str], bool]
#: A line of progress or explanation. `print` in production; a list's
#: `.append` in a test, so what was said is exactly what got asserted.
Announce = Callable[[str], None]

#: Pinned by `docs/agent-notes/onnxruntime-gpu-cuda-version.md`: 1.25+ moved to
#: CUDA 13, whose wheels are not usable from PyPI yet, while the `cuda` extra
#: pins CUDA 12. The one place this version is named now -- `setup.sh`'s own
#: constant is gone; see the module docstring above.
ONNXRUNTIME_GPU_VERSION = "1.24.4"


class EnvironmentSyncError(RuntimeError):
    """A step this module ran (`uv sync`, an uninstall, an install) failed.

    Raised rather than left to a caller inspecting a return code, because
    every caller of `sync_environment` -- `cli._run_sync` today -- needs to
    stop at exactly the point `setup.sh`'s `set -euo pipefail` already stops
    the whole script: a sync that fails must not be followed by a GPU swap
    against packages that were never installed.
    """


# --------------------------------------------------------------------------
# 16.3 / 11.5 / 11.6 -- refuse or warn before anything is synced
# --------------------------------------------------------------------------


def refuse_before_sync(profile: "PlatformProfile") -> str | None:
    """None where it is fine to sync; otherwise the refusal, unraised.

    Two situations name themselves here rather than being left to `uv sync`'s
    own resolver, per task 16.3: an operating system Murmly does not support
    at all (macOS, per this change's binding scope decision -- Murmly does
    not claim it, and `_dispatch`'s own `_unsupported_platform_message` would
    refuse every other command on it the same way, but the very first
    `uv sync` a fresh checkout runs happens from a bootstrap shell, before any
    `murmly` command can run to say so -- this is the check that runs once
    `murmly sync` itself is reachable, and `setup.sh`'s own guard is what
    covers the gap before that), and a machine with no build of the runtime
    transcription needs (`design.md`'s "CUDA loading, and what has no build
    where" table: musl Linux, Windows on ARM64, Intel macOS). Both fail at
    `uv sync` today with a resolver error naming a package instead of the
    machine characteristic that has no build of it -- exactly what the
    `platform-support` spec forbids surfacing in the runtime's own words.
    """
    from murmly.platform import SUPPORTED_OPERATING_SYSTEMS, transcription_runtime_gap

    if not profile.supported:
        supported = ", ".join(operating_system.value for operating_system in SUPPORTED_OPERATING_SYSTEMS)
        return (
            f"murmly: unsupported platform: {profile.operating_system.value}. "
            f"Murmly supports: {supported}."
        )

    gap = transcription_runtime_gap(profile)
    if gap is not None:
        return (
            f"murmly: refusing to sync. No {gap.runtime} build exists for "
            f"{gap.characteristic}, and transcription is what Murmly is."
        )
    return None


def refuse_or_warn_environment_preconditions(
    profile: "PlatformProfile",
    announce: Announce,
    preconditions: "Mapping[str, EnvironmentPrecondition] | None" = None,
) -> str | None:
    """Task 11.5/11.6, read before `uv sync` runs rather than only afterward.

    `platform_diagnostics` already renders `environment_preconditions_for` as
    `murmly doctor`'s ongoing report for a machine already running Murmly --
    that call site stays exactly as it is. This is the second, install-time
    caller `EnvironmentPrecondition`'s own docstring names: the one place
    long-path support can still be corrected *before* it costs anything,
    because `uv sync` otherwise fails with "the system cannot find the path
    specified" and never mentions length.

    Long paths refuse, because a sync that is going to fail this way gains
    nothing by being attempted -- the failure it would produce names nothing
    a person can act on. Developer Mode only warns: a doubled model cache is
    real disk cost, never a hard failure, so `EnvironmentPrecondition`'s own
    "worth naming, never worth refusing anything over" distinction from
    `Permission` applies here without exception.

    `satisfied is None` -- the platform offered no way to read it, or the
    read raised -- refuses nothing on either precondition: the
    `platform-support` spec's "silence is never claimed as a grant" rule
    extends the same way in the other direction here -- silence is not
    claimed as a *denial* either, so a machine this cannot be read on is
    warned about and let through rather than blocked on a guess.

    `preconditions` defaults to the real table and takes a parameter for the
    same reason `runtime_gaps_for`/`resolve_extras` do: on this suite's own
    Linux runner, `_windows_long_paths_enabled`'s default `read_registry_value`
    is bound at *definition* time, so nothing patchable at the platform
    module's top level can make a real Windows registry read answer `False`
    here -- a constructed `EnvironmentPrecondition(check=lambda profile:
    False)` is what lets a test exercise the refusal branch at all.
    """
    from murmly.platform import (
        ENVIRONMENT_PRECONDITIONS,
        WINDOWS_DEVELOPER_MODE,
        WINDOWS_LONG_PATHS,
        environment_preconditions_for,
    )

    active_preconditions = preconditions if preconditions is not None else ENVIRONMENT_PRECONDITIONS
    report = environment_preconditions_for(profile, active_preconditions)

    long_paths = report.get(WINDOWS_LONG_PATHS)
    if long_paths is not None:
        if long_paths["satisfied"] is False:
            return f"murmly: refusing to sync. {long_paths['description']}. {long_paths['remedy']}"
        if long_paths["satisfied"] is None:
            announce(f"Warning: could not determine whether {long_paths['description']}.")

    developer_mode = report.get(WINDOWS_DEVELOPER_MODE)
    if developer_mode is not None and developer_mode["satisfied"] is not True:
        announce(f"Warning: {developer_mode['description']}. {developer_mode['remedy']}")

    return None


# --------------------------------------------------------------------------
# 3.4's caller -- Linux system packages, for whichever manager is here
# --------------------------------------------------------------------------


def install_system_packages(
    profile: "PlatformProfile",
    *,
    speech_output: bool,
    confirm: Confirm,
    announce: Announce,
    which: WhichCommand = shutil.which,
    run_command: RunCommand = subprocess.run,
) -> None:
    """Offer this machine's package manager the packages Murmly's optional
    features need, generalised past `setup.sh`'s `dnf`-only step (task 3.4).

    Wayland and Plasma are read off `profile` the same way `is_wayland_session`
    and `desktop.py`'s own detection already do -- `wayland_display` or
    `session_type == "wayland"`, `desktop is Desktop.PLASMA` -- rather than
    imported from either module, because both take an environment mapping or
    a live session, and this only ever has the platform reading `cli._dispatch`
    already resolved.

    Unlike `setup.sh:181-206`'s `dnf`-specific step (now removed, replaced by
    this call from `cli._run_sync`), this does not diff against what is
    already installed -- `rpm -q` has no equivalent this table could name once
    per manager without a fifth divergent command, and 3.4 only asks to
    detect the manager and name its packages, not to detect what a machine
    already has. A person is asked once, for the full list, every time this
    runs.

    This is a real behaviour change from `setup.sh`'s old dnf-only step, not
    only a generalisation of it: a Fedora machine with everything already
    installed used to print "Everything Murmly uses is already installed"
    and offer nothing, and now offers the full `sudo dnf install -y ...`
    list on every `install`/`upgrade` -- which `--yes` then runs, a `sudo`
    prompt on an upgrade that used to need none. Restoring a per-manager
    missing-package diff is future work, not something 3.4 asked for; this
    drift is deliberate but should be visible, not silently inherited.
    """
    from murmly.platform import Desktop

    wayland = profile.wayland_display or profile.session_type == "wayland"
    plasma = profile.desktop is Desktop.PLASMA
    packages = system_packages(wayland=wayland, plasma=plasma, speech_output=speech_output, which=which)

    if packages.manager is None:
        announce("No recognised package manager here, so this step is skipped. The packages Murmly would use are:")
        announce(f"  {' '.join(packages.names)}")
        return

    # Only what is actually absent, which is what `setup.sh` did against
    # `rpm -q` before this step was generalised past `dnf`. Without it every
    # `install` and `upgrade` offers the whole list, and `--yes` runs it: a
    # `sudo` prompt and a package manager invocation on an upgrade that needs
    # neither.
    missing = missing_packages(packages.manager, packages.names, run_command)
    if not missing:
        announce("Everything Murmly uses is already installed.")
        return

    command = install_command(packages.manager, missing)
    announce(f"Command: {' '.join(command)}")
    if not confirm("Install these now?"):
        announce("Skipped. Murmly runs without them, with the features they serve disabled.")
        return
    run_command(list(command))


# --------------------------------------------------------------------------
# 16.1 -- extras carried across a sync, and the ONNX Runtime GPU swap
# --------------------------------------------------------------------------


def installed_package(project_dir: Path, name: str, run_command: RunCommand) -> bool:
    """Whether `name` is installed in `project_dir`'s environment.

    `uv pip show` reads the project environment without changing it -- the
    same call `setup.sh`'s own `installed_package()` makes (`setup.sh:110`).
    Any nonzero exit (not installed, no environment yet at all) answers
    `False`; nothing here distinguishes the two, because both mean the same
    thing to every caller below: do not assume this package is present.
    """
    result = run_command(
        ["uv", "pip", "show", "--project", str(project_dir), name],
        capture_output=True,
        text=True,
    )
    return getattr(result, "returncode", 1) == 0


def current_extras(project_dir: Path, run_command: RunCommand) -> tuple[str, ...]:
    """Extras already present, so a sync is never given a smaller set than it found.

    `setup.sh:220-228`'s own docstring explains why speech output is not among
    these: it is a default dependency group now, kept by every sync without
    being named, so the only reading it still needs is `wants_speech_output`'s
    own -- whether a person has deliberately turned it off.
    """
    if installed_package(project_dir, "nvidia-cublas-cu12", run_command):
        return ("cuda",)
    return ()


def wants_speech_output(want_tts: str, *, venv_exists: bool, kokoro_installed: bool) -> bool:
    """Whether the next sync should carry speech output (`setup.sh:230-243`).

    `want_tts` is `"yes"`, `"no"`, or `"auto"` -- `--tts`/`--no-tts`/neither.
    Under `"auto"`, an environment that already exists and has no synthesizer
    in it was opted out of one deliberately, so an upgrade leaves it that way
    rather than silently reinstalling speech output the same fault runs the
    other direction from the incident `docs/agent-notes/onnxruntime-gpu-cuda-version.md`
    records. A machine with no environment yet, or one that already carries
    the synthesizer, gets it: the synthesizer is a default dependency group,
    and arrives without being asked for.
    """
    if want_tts == "yes":
        return True
    if want_tts == "no":
        return False
    if venv_exists and not kokoro_installed:
        return False
    return True


def resolve_extras(
    *,
    current: Sequence[str],
    want_cuda: str,
    has_nvidia_driver: bool,
    confirm: Confirm,
    announce: Announce,
) -> tuple[str, ...]:
    """The extras the next sync should be given (`setup.sh:245-269`).

    `want_cuda` is `"yes"`, `"no"`, or `"auto"` -- `--cuda`/`--no-cuda`/neither.
    Under `"auto"`, an environment that does not already carry the extra is
    offered it once an NVIDIA driver is detected, exactly as `setup.sh`'s own
    resolution does; one already carrying it is left alone regardless of
    whether a driver can still be found, because removing a working GPU
    install because a driver check answered differently this run is not what
    "auto" means for either flag in this module.
    """
    wants_cuda = "cuda" in current

    if want_cuda == "yes":
        wants_cuda = True
    elif want_cuda == "no":
        wants_cuda = False
    elif not wants_cuda and has_nvidia_driver:
        announce("An NVIDIA driver is present. The cuda extra runs transcription on the GPU.")
        if confirm("Install the GPU runtime?"):
            wants_cuda = True

    return ("cuda",) if wants_cuda else ()


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What the next `uv sync` should be given, decided before it runs."""

    extras: tuple[str, ...]
    include_tts: bool

    @property
    def has_cuda(self) -> bool:
        return "cuda" in self.extras


def sync_arguments(plan: SyncPlan) -> tuple[str, ...]:
    """The `uv sync` arguments `plan` implies, after `sync`/`--locked` (`setup.sh:283-297`)."""
    arguments: list[str] = []
    for extra in plan.extras:
        arguments += ["--extra", extra]
    if not plan.include_tts:
        # Named only to leave it out -- the group is a default, so the sync
        # carries the synthesizer unless this says otherwise.
        arguments += ["--no-group", "tts"]
    return tuple(arguments)


def swap_in_gpu_onnxruntime(project_dir: Path, run_command: RunCommand, announce: Announce, reason: str) -> None:
    """Replace the CPU `onnxruntime` with the GPU build (`setup.sh:338-345`).

    Uninstalled first, deliberately, and best-effort: both distributions
    install into the same `onnxruntime` package namespace, and an environment
    holding both leaves whichever survives a later uninstall broken (recorded
    in `docs/agent-notes/onnxruntime-gpu-cuda-version.md`'s "Do not install
    both distributions"). The uninstall's own exit code is not checked -- an
    environment with no CPU build to remove is exactly what a successful
    previous swap already left behind, and `setup.sh`'s own `|| true` treats
    that the same way.

    The install runs `--reinstall`. A plain install no-ops when `uv` still
    has `onnxruntime-gpu` recorded as present from before the uninstall above
    ran -- the exact "repairing it needs `--reinstall`" trap that agent note
    records -- so the reinstall is unconditional here rather than only on a
    detected failure.
    """
    announce(f"{reason}: onnxruntime-gpu=={ONNXRUNTIME_GPU_VERSION}")
    run_command(["uv", "pip", "uninstall", "--project", str(project_dir), "onnxruntime"])
    result = run_command(
        [
            "uv",
            "pip",
            "install",
            "--project",
            str(project_dir),
            "--reinstall",
            f"onnxruntime-gpu=={ONNXRUNTIME_GPU_VERSION}",
        ]
    )
    if getattr(result, "returncode", 0) != 0:
        raise EnvironmentSyncError(f"Could not install onnxruntime-gpu=={ONNXRUNTIME_GPU_VERSION}.")


def sync_environment(
    project_dir: Path,
    *,
    want_cuda: str = "auto",
    want_tts: str = "auto",
    confirm: Confirm,
    announce: Announce = print,
    run_command: RunCommand = subprocess.run,
    which: WhichCommand = shutil.which,
    venv_exists: Callable[[Path], bool] = lambda project_dir: (project_dir / ".venv").is_dir(),
) -> SyncPlan:
    """`setup.sh:275-336`'s `sync_environment`, moved here per task 16.1.

    Reads the environment's current extras and whether the GPU build of ONNX
    Runtime was installed *before* syncing -- syncing is what puts the CPU
    build back, so this is the last point either fact can still be read from
    what is actually there rather than from what this run is about to make
    true. `sync_arguments` never sees `restore_gpu`/the GPU swap decision:
    the extras carried across and the ONNX Runtime swap are related but
    separate corrections, exactly as `setup.sh`'s own two functions keep them.

    Does not install the synthesis models -- that responsibility stays in
    `setup.sh`'s own `install_models`, which downloads a file rather than
    calling `uv`, so nothing about it is `uv sync`'s own behaviour for this
    module to own.

    `venv_exists` takes a parameter for the same reason every command runner
    in this module does: a test drives "an environment already exists and
    opted out of speech output" against a directory that was never created,
    rather than needing a real `.venv` on disk to exercise it.
    """
    venv_present = venv_exists(project_dir)
    current = current_extras(project_dir, run_command)
    kokoro_installed = installed_package(project_dir, "kokoro-onnx", run_command)
    restore_gpu = installed_package(project_dir, "onnxruntime-gpu", run_command)

    plan = SyncPlan(
        extras=resolve_extras(
            current=current,
            want_cuda=want_cuda,
            has_nvidia_driver=has_nvidia_gpu(which),
            confirm=confirm,
            announce=announce,
        ),
        include_tts=wants_speech_output(want_tts, venv_exists=venv_present, kokoro_installed=kokoro_installed),
    )

    if plan.extras:
        announce(f"Syncing with: {' '.join(plan.extras)}")
    else:
        announce("Syncing with no extras.")
    if not plan.include_tts:
        announce("Leaving speech output out.")

    result = run_command(
        ["uv", "sync", "--project", str(project_dir), "--locked", *sync_arguments(plan)]
    )
    if getattr(result, "returncode", 0) != 0:
        raise EnvironmentSyncError("uv sync failed; the environment was not changed further.")

    if plan.has_cuda and plan.include_tts:
        if restore_gpu:
            swap_in_gpu_onnxruntime(
                project_dir, run_command, announce, "restoring it, since the sync put the CPU build back"
            )
        else:
            announce(
                "Speech output can also run on the GPU. That build of ONNX Runtime replaces "
                "the CPU one rather than joining it, and every sync puts the CPU one back."
            )
            if confirm("Run synthesis on the GPU?"):
                swap_in_gpu_onnxruntime(project_dir, run_command, announce, "installing it")
    elif restore_gpu:
        announce("Warning: the GPU build of ONNX Runtime was installed but speech output is not, so it was not restored.")

    return plan


# --------------------------------------------------------------------------
# 16.6 -- decline every prompt, rather than assume, with nothing attached
# --------------------------------------------------------------------------


def make_confirm(
    assume_yes: bool,
    *,
    announce: Announce = print,
    isatty: Callable[[], bool] = sys.stdin.isatty,
    read_line: Callable[[str], str] = input,
) -> Confirm:
    """Build a `Confirm`, matching `setup.sh`'s own three-way `confirm()` exactly.

    `assume_yes` answers every question `True` without reading anything,
    which is `--yes`'s whole contract. Failing that, a caller with nothing
    attached to standard input -- no controlling terminal, as every unattended
    invocation of `murmly sync` has -- gets `False` for every question rather
    than a prompt that would block forever or, worse, a default this module
    quietly picked on the caller's behalf: task 16.6 asks for a decline here,
    never an assumption, and this is the one place that rule is enforced for
    every question this module ever asks. Only with both a terminal and no
    `--yes` is a person actually asked, and an end-of-input there (a redirected
    but empty stdin, a closed pipe) declines the same way a "no" typed by hand
    would.
    """

    def confirm(prompt: str) -> bool:
        if assume_yes:
            announce(f"{prompt} -- assuming yes (--yes)")
            return True
        if not isatty():
            announce(f"Warning: {prompt} -- skipped, nothing is attached to answer. Pass --yes to accept.")
            return False
        try:
            reply = read_line(f"{prompt} [y/N] ")
        except EOFError:
            return False
        return reply.strip().casefold() == "y"

    return confirm


__all__ = [
    "Announce",
    "Confirm",
    "EnvironmentSyncError",
    "ONNXRUNTIME_GPU_VERSION",
    "RunCommand",
    "SyncPlan",
    "current_extras",
    "install_system_packages",
    "installed_package",
    "make_confirm",
    "refuse_before_sync",
    "refuse_or_warn_environment_preconditions",
    "resolve_extras",
    "swap_in_gpu_onnxruntime",
    "sync_arguments",
    "sync_environment",
    "wants_speech_output",
]
