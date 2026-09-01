#!/usr/bin/env python3
"""Task 12.1-12.3: does a launchd-started process get real microphone audio?

design.md's largest risk. macOS gates the microphone through TCC, which
attributes a request to a signed bundle carrying `NSMicrophoneUsageDescription`.
A bare Python process started by a launchd agent has neither, and the reported
failure is silent: no dialog, no exception, a stream delivering zeroes. A
process run from a terminal usually works only because it inherits the
terminal application's own grant -- which is exactly what would hide this in
development and only surface it once the daemon runs unattended.

This cannot be answered from Linux, and the project's CI Mac is headless with
no microphone at all -- it can prove the diagnostics distinguish "denied" from
"absent" (see `MacosMicrophonePermissionRuntimeIntegrationTests` in
`tests/test_platform.py`), but not whether launchd audio arrives on a machine
that has a microphone to arrive from. Only a person with a real Mac can run
this.

WHAT TO RUN, IN ORDER
======================

1. Baseline, from a Terminal you are typing into (task 12.2):

       uv run --no-sync python3 scripts/macos_microphone_spike.py record --seconds 3

   Speak into the microphone for the 3 seconds. Look at the printed peak and
   RMS. Nonzero (peak well above 0.0, RMS not `-inf`-equivalent) is what a
   terminal-inherited grant looks like -- the control this compares against.

2. The same recording, started by launchd instead (task 12.1):

       uv run --no-sync python3 scripts/macos_microphone_spike.py install-agent --seconds 3
       sleep 5
       uv run --no-sync python3 scripts/macos_microphone_spike.py status

   `install-agent` writes `~/Library/LaunchAgents/com.murmly.spike.microphone.plist`
   and runs it once at load (`RunAtLoad`, not `KeepAlive`) with `sys.executable`
   as an absolute path, because launchd's own `PATH` is minimal and will not
   find a bare `python3`. `status` prints `launchctl print` for the agent and
   the tail of its log file -- the log is where to look, since a launchd agent
   has no attached terminal for `print()` to reach; `install-agent` sets
   `StandardOutPath`/`StandardErrorPath` to route it there instead.

   Compare the peak/RMS in the log against step 1's. If step 1 was nonzero and
   this is all-zero (or the log shows a `PortAudioError` opening the stream),
   that is design.md's failure reproduced: TCC did not attribute this process's
   microphone request to any grant, or refused before PortAudio ever obtained a
   frame that wasn't silence.

3. If step 2 came back all-zero, try task 12.3's remedy -- attribute the
   agent to a bundle that already holds the microphone grant:

       uv run --no-sync python3 scripts/macos_microphone_spike.py uninstall-agent
       uv run --no-sync python3 scripts/macos_microphone_spike.py install-agent --seconds 3 \\
           --associated-bundle-id com.apple.Terminal
       sleep 5
       uv run --no-sync python3 scripts/macos_microphone_spike.py status

   `com.apple.Terminal` is a plausible first bundle to try only if Terminal
   itself already holds the microphone grant (macOS will have prompted for it
   the first time something run from Terminal used the microphone) -- this
   script does not grant anything itself, per the `platform-support` spec's
   "MUST NOT attempt to grant a permission on the person's behalf" rule.
   `AssociatedBundleIdentifiers` requires the bundle identifier to already be
   installed and already hold the grant Murmly wants; Murmly ships no bundle
   of its own to name here (that is task 12.4's `.app` wrapper, not built by
   this script). If this also comes back all-zero, 12.3 has failed and 12.4 is
   the remaining route -- do not conclude that from this script alone; it
   proves 12.3, nothing past it.

4. Clean up either way:

       uv run --no-sync python3 scripts/macos_microphone_spike.py uninstall-agent

   To rerun step 2 or 3 without a fresh `install-agent` (e.g. after changing
   nothing but wanting a second sample), `kickstart -k` re-triggers it without
   reinstalling the plist:

       launchctl kickstart -k gui/$(id -u)/com.murmly.spike.microphone

WHAT THIS DOES NOT ANSWER
==========================

This is a spike, not a fix and not a service: it does not use `launchctl
bootstrap`'s exit-0-on-a-malformed-plist failure mode as a pass signal (this
script's own `install-agent` checks `launchctl`'s return code, and
`launchctl print` in `status` is the actual proof the agent loaded), and it is
not `LaunchdUserService` (task 13.3/13.4) -- it only exists to answer 12.1
through 12.3 in one place. Do not point Murmly's real launchd service backend
at this file.

Until one of 12.1-12.4 is proven working end to end by someone running the
steps above on a real Mac, macOS microphone capture is not claimed anywhere
in this codebase (task 12.6) -- see `microphone_diagnostics` in `murmly/cli.py`
for the diagnostic surface that stays honest about this in the meantime.
"""

from __future__ import annotations

import argparse
import math
import plistlib
import subprocess
import sys
from pathlib import Path

from murmly.installer import macos_launchd_agent_plist

LABEL = "com.murmly.spike.microphone"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _log_path(label: str) -> Path:
    return Path.home() / "Library" / "Logs" / f"{label}.log"


def _plist_path(label: str) -> Path:
    return _launch_agents_dir() / f"{label}.plist"


def _uid() -> int:
    import os

    return os.getuid()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(command)}", file=sys.stderr)
    return subprocess.run(command, capture_output=True, text=True)


def cmd_record(args: argparse.Namespace) -> int:
    """Record `args.seconds` of audio and print/append peak and RMS.

    Deliberately independent of `murmly.audio.SoundDeviceRecorder`: this
    needs to keep working even if that class's own device-selection logic is
    itself part of what a future investigation suspects, and it needs to be
    short enough to read in one sitting while debugging a TCC problem.
    """
    import sounddevice
    import numpy

    samplerate = args.samplerate
    frames = int(args.seconds * samplerate)
    lines: list[str] = []
    try:
        recording = sounddevice.rec(frames, samplerate=samplerate, channels=1, dtype="float32")
        sounddevice.wait()
    except Exception as error:  # noqa: BLE001 - this is a diagnostic script
        lines.append(f"RECORDING FAILED: {error!r}")
        _emit(lines, args.log)
        return 1

    samples = recording.reshape(-1)
    peak = float(numpy.max(numpy.abs(samples))) if len(samples) else 0.0
    rms = float(numpy.sqrt(numpy.mean(numpy.square(samples)))) if len(samples) else 0.0
    rms_dbfs = 20 * math.log10(rms) if rms > 0 else float("-inf")

    lines.append(f"frames={len(samples)} samplerate={samplerate}")
    lines.append(f"peak={peak:.6f} rms={rms:.6f} rms_dbfs={rms_dbfs:.1f}")
    if peak == 0.0:
        lines.append(
            "ALL ZERO -- either nothing was said, or (if you did speak) this is "
            "the TCC failure design.md describes: a stream delivering zeroes."
        )
    else:
        lines.append("Nonzero audio arrived.")
    _emit(lines, args.log)
    return 0


def _emit(lines: list[str], log: Path | None) -> None:
    text = "\n".join(lines)
    print(text)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def cmd_install_agent(args: argparse.Namespace) -> int:
    log_path = _log_path(args.label)
    plist_path = _plist_path(args.label)
    script_path = str(Path(__file__).resolve())

    program_arguments = [
        sys.executable,
        script_path,
        "record",
        "--seconds",
        str(args.seconds),
        "--samplerate",
        str(args.samplerate),
        "--log",
        str(log_path),
    ]
    plist = macos_launchd_agent_plist(
        args.label,
        program_arguments,
        associated_bundle_identifiers=[args.associated_bundle_id] if args.associated_bundle_id else None,
    )
    # RunAtLoad only for this spike -- KeepAlive would restart it in a loop
    # every time it exits, which is right for a service and wrong for a
    # one-shot measurement.
    plist["KeepAlive"] = False
    plist["StandardOutPath"] = str(log_path)
    plist["StandardErrorPath"] = str(log_path)

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)
    print(f"wrote {plist_path}")

    # bootout first so a reinstall is idempotent -- bootstrapping a label
    # that is already loaded fails instead of replacing it.
    _run(["launchctl", "bootout", f"gui/{_uid()}/{args.label}"])
    result = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path)])
    if result.returncode != 0:
        print(f"launchctl bootstrap failed (exit {result.returncode}): {result.stderr}", file=sys.stderr)
        return 1
    print(f"agent loaded. It runs once at load. Check with: {sys.argv[0]} status")
    return 0


def cmd_uninstall_agent(args: argparse.Namespace) -> int:
    _run(["launchctl", "bootout", f"gui/{_uid()}/{args.label}"])
    plist_path = _plist_path(args.label)
    if plist_path.exists():
        plist_path.unlink()
        print(f"removed {plist_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = _run(["launchctl", "print", f"gui/{_uid()}/{args.label}"])
    print(result.stdout or result.stderr)
    log_path = _log_path(args.label)
    if log_path.exists():
        print(f"--- {log_path} ---")
        print(log_path.read_text(encoding="utf-8"))
    else:
        print(f"no log yet at {log_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Record and print peak/RMS.")
    record.add_argument("--seconds", type=float, default=3.0)
    record.add_argument("--samplerate", type=int, default=16_000)
    record.add_argument("--log", type=Path, default=None)
    record.set_defaults(func=cmd_record)

    install_agent = subparsers.add_parser("install-agent", help="Install and load the launchd spike agent.")
    install_agent.add_argument("--seconds", type=float, default=3.0)
    install_agent.add_argument("--samplerate", type=int, default=16_000)
    install_agent.add_argument("--label", default=LABEL)
    install_agent.add_argument(
        "--associated-bundle-id",
        default=None,
        help="Task 12.3: a bundle identifier that already holds the microphone grant.",
    )
    install_agent.set_defaults(func=cmd_install_agent)

    uninstall_agent = subparsers.add_parser("uninstall-agent", help="Unload and remove the spike agent.")
    uninstall_agent.add_argument("--label", default=LABEL)
    uninstall_agent.set_defaults(func=cmd_uninstall_agent)

    status = subparsers.add_parser("status", help="Print launchctl's view of the agent and its log.")
    status.add_argument("--label", default=LABEL)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
