---
title: Harden Command Transport Tasks
description: Track implementation and validation of answered connections, coded failures, shape-tolerant request parsing, and owner-only access on the command socket
---

## 1. Failure codes

- [ ] 1.1 Define the closed code vocabulary in `daemon.py` covering seven categories: busy, unsupported command, malformed request, over capacity, not permitted, shutting down, command failure
- [ ] 1.2 Add a response builder taking a code, a message, and any additional fields, so a response carrying `state` keeps it
- [ ] 1.3 Route every `{"ok": False, ...}` return through that builder, including the three in `handle_command`, the one in `_handle_connection`, and the one in `_finish_toggle`, keeping each `error` string byte-identical
- [ ] 1.4 Add tests asserting each existing failure keeps its current `error` text and every other field it carries today, and gains the expected `code`
- [ ] 1.5 Add a test asserting the seven categories map to seven distinct codes
- [ ] 1.6 Add a test asserting successful responses carry no `code`, and confirm the four exact-dict assertions at `tests/test_daemon.py:154`, `:854`, `:864`, `:1245` still pass unchanged

## 2. Shape-tolerant request parsing

- [ ] 2.1 Replace the bare-string extraction in `_handle_connection` with an explicit check that the decoded payload is a mapping, returning the malformed-request code when it is not
- [ ] 2.2 Check that the command name is a string before dispatch, returning the unsupported-command code without coercing it
- [ ] 2.3 Leave `handle_command`'s signature unchanged; no parameter plumbing in this change
- [ ] 2.4 Add tests over a real socket for `[1,2]`, `"hi"`, `5`, and `{"command": 5}`, asserting a response arrives and a following `status` still answers

## 3. Answered connections

- [ ] 3.1 Convert the caught-and-passed parse failures in `_serve_connection` into responses: invalid JSON, invalid UTF-8, and a request that does not arrive within the command timeout
- [ ] 3.2 Widen `_serve_connection`'s except clauses so any unexpected exception becomes a response rather than a dead thread, logging the exception at warning
- [ ] 3.3 Write an over-capacity response inline in `_dispatch_connection` before closing, with a send timeout set first and a failed write discarded, without moving either existing semaphore release
- [ ] 3.4 Write a response on the two remaining mute paths in `_dispatch_connection`: shutdown observed after slot acquisition, and a worker thread that fails to start
- [ ] 3.5 Add a bounded drain to `shutdown()` so a worker whose request was already read can write a shutting-down response before its connection is force-closed
- [ ] 3.6 Rewrite `tests/test_daemon.py:437`, which currently asserts `assertEqual(b"", received)` on the capacity path, to assert the over-capacity response
- [ ] 3.7 Add tests for: invalid JSON, invalid UTF-8, a connect-then-idle request timeout, and an unexpected exception raised inside command handling — each asserting a response arrives and the daemon keeps serving
- [ ] 3.8 Add a shutdown test that asserts a response with the shutting-down code, replacing reliance on `tests/test_daemon.py:308-312` which swallows the failure with `except RuntimeError: pass`
- [ ] 3.9 Add a test that a peer which never reads its response does not stop the daemon accepting the next command

## 4. Commands that never raise

- [ ] 4.1 Replace the bare `RuntimeError` in `send_command` with a dedicated exception type for "connected but received no response", subclassing `RuntimeError` so `tests/test_daemon.py:311` stays honest
- [ ] 4.2 Handle that type by name in both `send_command_with_recovery` paths and in `_run_client_command`, reporting a single message and exiting non-zero
- [ ] 4.3 Add a top-level guard in `main` that reports an unexpected failure and returns non-zero, covering `load_config` on a malformed `config.toml` before any subcommand dispatches
- [ ] 4.4 Report the daemon startup refusal from `_run_daemon` as a message and a non-zero exit rather than letting it propagate
- [ ] 4.5 Add tests: a stub server that accepts and closes produces a reported message and non-zero exit with no exception escaping `main`; a malformed `config.toml` does the same for a non-daemon subcommand

## 5. Socket access control

- [ ] 5.1 Validate the configured `socket_path` at daemon startup, before the existing `mkdir` and `unlink`, refusing to start when its containing directory is writable by group or other, and reporting the path plus both remedies
- [ ] 5.2 Create the socket's parent directory without `exist_ok` and `chmod` it to `0700` only when Murmly created it, since `mkdir(mode=...)` skips existing directories and intermediates
- [ ] 5.3 `chmod` the socket node to `0600` immediately after bind
- [ ] 5.4 Read the peer's identity on each accepted connection and refuse a mismatch with the not-permitted code, checking before the worker slot is acquired so a refused peer consumes no capacity, with the comparison injectable
- [ ] 5.5 Log once at startup and report in diagnostics when the platform cannot report peer identity, and continue serving
- [ ] 5.6 Report the configured socket path's privacy in `murmly doctor` without refusing to run
- [ ] 5.7 Add tests for created socket and directory modes, and for a refused substituted peer identity alongside an accepted matching one
- [ ] 5.8 Add tests for startup refusal on a group- or other-writable configured path, acceptance of a readable-but-not-writable directory, and normal startup on the default path
- [ ] 5.9 Add a test that diagnostics still run and report the condition for a path the daemon would refuse

## 6. Diagnostics

- [ ] 6.1 Move the transcription runtime probe in `_run_doctor` inside a guard, reporting the failure in a separate detail field so `runtime_device` and `runtime_compute_type` keep the shape pinned at `tests/test_cli.py:88`
- [ ] 6.2 Bring the remaining unguarded probes in `_run_doctor` under the same rule: `is_wayland_session` and `create_focus_observer` as reached from `delivery_diagnostics`
- [ ] 6.3 Add a test asserting `murmly doctor` produces a complete report with `stt.device = "cuda"` and the runtime unavailable, naming the reason in the transcription section
- [ ] 6.4 Add a test asserting the success shape of every diagnostics section is unchanged

## 7. Documentation

- [ ] 7.1 Document the socket path privacy requirement in `README.md` where `daemon.socket_path` is described
- [ ] 7.2 Document it in `config.example.toml` beside the commented `socket_path` line

## 8. Verification

- [ ] 8.1 Run `uv run --extra cuda python -m unittest discover -s tests` and confirm the suite is green
- [ ] 8.2 Start the daemon, press the bound hotkey, and confirm capture still toggles normally
- [ ] 8.3 Restart the service while a transcription is in flight and confirm the hotkey path reports a message rather than a traceback
- [ ] 8.4 Send `[1,2]` and `not json` to the live socket and confirm both are answered and the daemon keeps serving
- [ ] 8.5 Run `murmly doctor` and confirm the report is complete and reports the socket path privacy
- [ ] 8.6 Run `openspec validate harden-command-transport --strict`
