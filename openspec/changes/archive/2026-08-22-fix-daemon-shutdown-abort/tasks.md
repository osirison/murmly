---
title: Fix Daemon Shutdown Abort Tasks
description: Track the reproduction, the removal of PortAudio's exit-time teardown from the daemon path, the release of capture during shutdown, and the unit ordering after the audio server
---

## 1. Confirm the mechanism before changing anything

- [x] 1.1 Reproduce the abort against a private PipeWire instance: start `pipewire` under `PIPEWIRE_CORE`, run a process with `PIPEWIRE_REMOTE` pointed at it that only imports `sounddevice`, kill the instance, then let the process return from `main`; confirm SIGABRT at `pa_jack.c:867` without a stream ever being opened
- [x] 1.2 Confirm the same harness exits 0 once `sounddevice._exit_handler` is unregistered, so the fix is verified against the real failure and not only against fakes
- [x] 1.3 Write the reproduction technique up as a field note in `docs/agent-notes/`; `PIPEWIRE_CORE` / `PIPEWIRE_REMOTE` isolation is the precondition that makes this testable without touching the session's own audio server

## 2. Neutralize PortAudio's exit-time teardown on the daemon path

- [x] 2.1 Add a function to `audio.py` that drops `sounddevice`'s `atexit` hook, reading the module out of `sys.modules` rather than importing it, so a daemon that never opened a device does not pay for the import
- [x] 2.2 Guard it on `_exit_handler` being present and log a warning rather than raising when it is not; `_exit_handler` is a private symbol and losing this protection must not stop a shutdown
- [x] 2.3 Call it from `_run_daemon`'s `finally` in `cli.py`, so a clean stop, a `DaemonStartupError`, and an unhandled exception are all covered, and no short-lived command is affected
- [x] 2.4 Add tests: a no-op when `sounddevice` was never imported, the hook unregistered when it was, a warning rather than a raise when the symbol is absent, and the hook left in place for a command other than `daemon`
- [x] 2.5 Add a test against the real `sounddevice` module that fails if `_exit_handler` is renamed, skipping when the module cannot be imported, following the suite's convention for tests that need something the environment may not have

## 3. Release the microphone during shutdown

- [x] 3.1 Stop capture in `MurmlyDaemon.shutdown()` through the session, beside the speech session close and before the connection drain, discarding the audio rather than transcribing it
- [x] 3.2 Log a failure to stop and continue: the socket, the overlay, and the remaining streams still have to be released
- [x] 3.3 Add tests: shutdown stops capture while a recording is in progress, shutdown while idle raises nothing, and a stop that fails still leaves the socket removed and the overlay closed
- [x] 3.4 Confirm the existing shutdown ordering tests still hold, including the one asserting the overlay closes after the socket is cleaned up

## 4. Order the installed unit after the audio server

- [x] 4.1 Add `After=pipewire.service wireplumber.service` to `SERVICE_UNIT_TEMPLATE` in `installer.py`, keeping the existing `graphical-session.target` ordering and `PartOf=`
- [x] 4.2 Extend `UnitTextTests` to assert the new ordering alongside the existing assertions
- [x] 4.3 Confirm install, reinstall, and uninstall tests still pass against the changed template

## 5. Verify

- [x] 5.1 Run the full suite: `uv run --extra cuda python -m unittest discover -s tests`
- [x] 5.2 Re-run the reproduction harness against a daemon built from this branch and confirm a stop after the audio server is gone exits 0 with no core dump
- [x] 5.3 Run `openspec validate --strict` on the change
- [x] 5.4 Update `README.md` only if it states anything this change contradicts
- [x] 5.5 Ask before restarting the user's installed service, and note that the unit ordering needs `murmly install` run again and that a unit already in `failed` needs one `systemctl --user reset-failed`
