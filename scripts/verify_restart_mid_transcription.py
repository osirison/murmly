#!/usr/bin/env python3
"""Restart the service while a transcription is running and check what the caller gets.

This is task 8.3 of `openspec/changes/harden-command-transport`, which cannot be
done by the test suite: the suite calls `shutdown()` in process, and what is
unverified is whether a `systemctl --user restart` produces the same interleaving.
Doing it by hand means restarting during the few seconds a transcription is
decoding, and a miss looks exactly like a pass -- the toggle simply succeeds -- so
this script waits for the daemon to report THINKING and restarts inside that
window instead of guessing.

The command it runs is the one the hotkey runs: the `ExecStart` entrypoint
recorded in the installed unit, followed by `toggle`. Only the key event itself is
not exercised, which is what task 8.2 covers.

Outcomes, one per attempt:

  answered        the caller received the shutting-down response. This is the
                  pass: the connection was owed an answer and got one.
  no response     the connection closed without a response, reported as a
                  message. No traceback, so the desktop-integration rule holds,
                  but "An accepted connection is answered" does not.
  traceback       the failure this whole change exists to remove.
  raced           the transcription finished before the restart landed, so the
                  scenario never happened. Retried, not counted.

Usage:

    uv run --no-sync python scripts/verify_restart_mid_transcription.py
    uv run --no-sync python scripts/verify_restart_mid_transcription.py --attempts 3 --speak-seconds 6

Speak for the whole recording window. Silence can be filtered out before the
decoder runs, which finishes the transcription too quickly to restart into.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass

from murmly.config import load_config
from murmly.installer import SERVICE_NAME, UserService

# Imported late enough to fail with the repo's own message if the package is not
# importable, rather than a bare ImportError from the shebang.
from murmly.daemon import send_command


POLL_SECONDS = 0.05
STATE_TIMEOUT_SECONDS = 20.0
TOGGLE_TIMEOUT_SECONDS = 120.0
SERVICE_READY_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Attempt:
    outcome: str
    detail: str
    returncode: int | None
    stdout: str
    stderr: str


def daemon_state(socket_path: str) -> str | None:
    """The daemon's state, or None while it is not answering."""
    try:
        response = send_command(socket_path, "status", connect_timeout=1.0, response_timeout=5.0)
    except Exception:
        return None
    state = response.get("state")
    return state if isinstance(state, str) else None


def wait_for_state(socket_path: str, wanted: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_state(socket_path) == wanted:
            return True
        time.sleep(POLL_SECONDS)
    return False


def wait_for_any_state(socket_path: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = daemon_state(socket_path)
        if state is not None:
            return state
        time.sleep(POLL_SECONDS)
    return None


def restart_service() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", "restart", SERVICE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )


def classify(process: subprocess.CompletedProcess[str]) -> Attempt:
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if "Traceback (most recent call last)" in stderr:
        return Attempt("traceback", "the caller raised instead of reporting", process.returncode, stdout, stderr)
    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        response = None
    if isinstance(response, dict):
        if response.get("code") == "shutting_down":
            return Attempt("answered", str(response.get("error", "")), process.returncode, stdout, stderr)
        if response.get("ok") is True:
            return Attempt(
                "raced",
                "the transcription finished before the restart landed",
                process.returncode,
                stdout,
                stderr,
            )
        return Attempt(
            "other response",
            f"code {response.get('code')!r}: {response.get('error')!r}",
            process.returncode,
            stdout,
            stderr,
        )
    reported = stderr.strip().splitlines()
    if reported:
        return Attempt("no response", reported[0], process.returncode, stdout, stderr)
    return Attempt("nothing", "the caller printed nothing at all", process.returncode, stdout, stderr)


def collect(stopping: subprocess.Popen[str]) -> tuple[str, str] | None:
    """The toggle's output, or None if it never returned."""
    try:
        return stopping.communicate(timeout=TOGGLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        stopping.kill()
        stopping.communicate()
        return None


def run_attempt(entrypoint: list[str], socket_path: str, speak_seconds: float) -> Attempt:
    state = wait_for_any_state(socket_path, SERVICE_READY_TIMEOUT_SECONDS)
    if state is None:
        return Attempt("setup", "the daemon is not answering", None, "", "")
    if state != "IDLE":
        return Attempt("setup", f"the daemon is {state}, not IDLE", None, "", "")

    started = subprocess.run([*entrypoint, "toggle"], capture_output=True, text=True, check=False)
    if started.returncode != 0:
        return Attempt("setup", f"the toggle that starts capture failed: {started.stderr.strip()}", None, "", "")
    try:
        opening = json.loads(started.stdout or "{}")
    except json.JSONDecodeError:
        opening = {}
    if opening.get("state") != "LISTENING":
        return Attempt("setup", f"capture did not start: {started.stdout.strip()}", None, "", "")

    print(f"    recording for {speak_seconds:g}s -- speak now", flush=True)
    time.sleep(speak_seconds)

    # The stopping toggle blocks until the transcription is done, so it is the
    # command that has to be interrupted. Started in the background, its response
    # collected after the restart.
    stopping = subprocess.Popen(
        [*entrypoint, "toggle"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # THINKING is published at the start of the stop path, before capture is even
    # closed, so seeing it means the window is open rather than nearly over.
    if not wait_for_state(socket_path, "THINKING", STATE_TIMEOUT_SECONDS):
        collected = collect(stopping)
        if collected is None:
            return Attempt("hung", f"no response within {TOGGLE_TIMEOUT_SECONDS:g}s", None, "", "")
        stdout, stderr = collected
        return Attempt(
            "raced",
            "the transcription finished before THINKING could be observed",
            stopping.returncode,
            stdout,
            stderr,
        )

    print("    THINKING observed, restarting the service", flush=True)
    restart = restart_service()
    if restart.returncode != 0:
        stopping.kill()
        stopping.communicate()
        return Attempt("setup", f"the restart failed: {restart.stderr.strip()}", None, "", "")

    collected = collect(stopping)
    if collected is None:
        # The caller never returned, which is the same dead end for a hotkey press
        # as a traceback: nothing happens and nothing says why.
        return Attempt("hung", f"no response within {TOGGLE_TIMEOUT_SECONDS:g}s", None, "", "")
    stdout, stderr = collected
    return classify(
        subprocess.CompletedProcess(stopping.args, stopping.returncode, stdout, stderr)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attempts", type=int, default=3, help="How many times to land the restart.")
    parser.add_argument(
        "--speak-seconds",
        type=float,
        default=6.0,
        help="How long to record before stopping. Longer means a longer decode to restart into.",
    )
    parser.add_argument(
        "--entrypoint",
        default=None,
        help="Override the command the hotkey runs. Defaults to the installed unit's ExecStart.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args(argv)

    service = UserService()
    if args.entrypoint is not None:
        entrypoint = args.entrypoint.split()
    else:
        recorded = service.recorded_entrypoint()
        if recorded is None:
            print(
                "No installed service to restart. Run 'murmly install <hotkey>' first, or pass "
                "--entrypoint.",
                file=sys.stderr,
            )
            return 1
        entrypoint = recorded.split()

    config = load_config(None)
    socket_path = str(config.socket_path)

    print("Restarting the service mid-transcription, checking what the caller receives.")
    print(f"  command   {' '.join(entrypoint)} toggle")
    print(f"  socket    {socket_path}")
    print(f"  attempts  {args.attempts}")
    print()
    print("This records from the microphone and this configuration delivers transcripts by")
    print("pasting them into the focused window. Focus something harmless -- a scratch file,")
    print("not a shell prompt -- before starting.")
    if not args.yes:
        try:
            input("Press Enter when ready, or Ctrl-C to stop. ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 130

    landed = 0
    raced = 0
    attempt_number = 0
    while landed < args.attempts:
        attempt_number += 1
        if raced >= args.attempts * 3:
            print("Gave up: the restart never landed inside a transcription. Try --speak-seconds 10.")
            return 1
        print(f"[attempt {attempt_number}]")
        result = run_attempt(entrypoint, socket_path, args.speak_seconds)

        if result.outcome == "setup":
            print(f"    could not run the attempt: {result.detail}", file=sys.stderr)
            return 1
        if result.outcome == "raced":
            raced += 1
            print(f"    inconclusive: {result.detail}")
            continue

        landed += 1
        if result.outcome == "answered":
            print(f"    answered: {result.detail!r} (exit {result.returncode})")
            continue

        print(f"    FAILED [{result.outcome}]: {result.detail}", file=sys.stderr)
        if result.stdout.strip():
            print(f"    stdout: {result.stdout.strip()}", file=sys.stderr)
        if result.stderr.strip():
            print("    stderr:", file=sys.stderr)
            for line in result.stderr.strip().splitlines():
                print(f"      {line}", file=sys.stderr)
        return 1

    print()
    print(f"PASS: {landed} restart(s) landed inside a transcription, each answered with")
    print("      the shutting-down response rather than an empty read or a traceback.")
    if raced:
        print(f"      {raced} further attempt(s) finished before the restart and were not counted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
