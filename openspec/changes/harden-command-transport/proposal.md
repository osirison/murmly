---
title: Harden Command Transport
description: Make every Murmly command answer its caller, identify failures by code, and restrict the command socket to the account that owns it
---

## Why

The daemon closes connections without sending a byte on five paths: no worker slot
free, a request that cannot be parsed, a request that is valid JSON but not an
object, a worker thread that fails to start, and shutdown while a command is in
flight. The client turns a byte-less read into a bare `RuntimeError` that no
caller catches, so the failure surfaces as an unhandled traceback from a keypress
that has no output channel — a violation of the shipped `desktop-integration`
rule that a hotkey press must never do that. It is reachable today by restarting
the service while a transcription is running.

The non-object case is worse than silent. A payload that is valid JSON but not an
object (`[1,2]`, `"hi"`, `5`) reaches `command.get(...)` and raises
`AttributeError`, which is in none of the connection handler's except clauses. The
command thread dies with a traceback in the daemon log and the caller gets
nothing. Measured against a live daemon on all three payloads.

Failures are reported only as prose. `"Daemon is busy."` and
`"Unsupported command: X"` are written for a person to read; a program that has to
decide what to do next has nothing to branch on but the wording.

The socket is unprotected against a caller from another account. Its parent
directory is created with no mode and the node is never `chmod`ed, so both land at
`0755` under the usual umask, and `daemon.socket_path` is user-configurable with
no validation at all. Connecting to an AF_UNIX socket requires *write* permission,
so `0755` does not by itself let another account connect — but a directory that
other accounts can write to lets one pre-create or replace the node, so the
owner's own `murmly toggle` connects to a socket someone else is serving.

Separately, `murmly doctor` exits with a traceback and prints nothing when
`stt.device = "cuda"` and the CUDA extra is absent, because the runtime probe runs
outside the guards that protect the rest of the report. That is the exact
misconfiguration the command exists to explain.

All of these are defects today with a single caller. A second consumer of the
socket makes each of them routine rather than rare.

## What Changes

- **A connection Murmly accepts is answered.** Connection capacity exhaustion, a
  request that cannot be parsed, a request that is not an object, and a worker
  that fails to start each produce a response frame instead of a silent close.
  Two cases remain unanswerable and are stated as exceptions rather than left
  implicit: a peer that closed the connection first, and a connection on which no
  request had been read when shutdown began.
- **No command terminates with an unhandled error.** The client reports a
  connection closed without a response and exits non-zero. Every subcommand runs
  under a top-level guard, so a malformed `config.toml` or a refused daemon
  startup is reported rather than raised.
- **Unsuccessful responses carry a stable machine-readable `code`** alongside the
  existing `error` string, and keep every other field they carry today. Existing
  `error` wording is unchanged.
- **A request of an unexpected shape is answered, not fatal.** A payload that is
  not an object, and a command name that is not a string, each produce a response,
  and the daemon keeps serving.
- **`murmly doctor` reports every section it can and explains the ones it
  cannot**, rather than abandoning the whole report because one probe failed.
- **The command socket is restricted to the account that owns it.** Directories
  Murmly creates for it and the socket node are owner-only, and a peer whose
  reported identity differs from the daemon's is refused.
- **BREAKING**: a configured `daemon.socket_path` whose containing directory is
  writable by group or other causes the daemon to refuse to start, reporting the
  path and both remedies. Directories that are merely readable or traversable by
  others are not refused, because the socket node itself is owner-only and
  connecting requires write permission on it. The default path is unaffected, and
  `murmly doctor` reports the condition without refusing to run.

Successful responses are unchanged. `status` still returns exactly
`{"ok": true, "state": ...}` and the `toggle` response keeps every field and
meaning it has today. Protocol and capability advertisement is deliberately not
part of this change; it is deferred until a client exists that needs it.

## Capabilities

### New Capabilities

- `command-interface`: how a caller reaches Murmly and is answered — the command
  socket's access control, which accepted connections must receive a response and
  which may not, machine-readable failure codes, the rule that no Murmly command
  terminates without telling its caller why, and the completeness of the
  diagnostics report.

### Modified Capabilities

- `desktop-integration`: the requirement that a hotkey press recovers when the
  daemon is not listening gains a scenario for a daemon that accepts the
  connection and then closes it without responding. The rule is unchanged; its
  scenarios cover only the cases where connecting itself fails.

## Impact

- `src/murmly/daemon.py` — response frames on the capacity, malformed-request,
  unexpected-shape, and thread-start paths; a bounded drain before shutdown
  force-closes; object-shaped request parsing; `code` on every unsuccessful
  response; socket and directory permissions; peer identity check on accept.
- `src/murmly/cli.py` — a named exception handled on the client path; a top-level
  guard in `main`; the daemon startup refusal reported rather than raised; the
  transcription runtime probe moved inside a guard.
- `src/murmly/config.py` — no change. Socket path validation runs at daemon
  startup, so every other command continues to load a configuration the daemon
  would refuse.
- `tests/test_daemon.py` — `test_daemon.py:437` currently asserts the zero-byte
  close on the capacity path and must be rewritten to assert the response.
- `tests/test_cli.py` — the diagnostics success shape is pinned at
  `test_cli.py:88` and must not change.
- `README.md` and `config.example.toml` — document the socket path privacy
  requirement.
- No new dependencies. Peer credentials are read through the standard library.
- No change to the installed service, the hotkey binding, transcription, delivery,
  or the overlay.
