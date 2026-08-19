---
title: Harden Command Transport Design
description: Technical approach for answered connections, coded failures, shape-tolerant request parsing, and owner-only access on the command socket
---

## Context

See proposal.md — Why. The constraints that shape the approach, each verified
against the current tree:

- **Five paths close an accepted connection without writing.** No worker slot
  (`daemon.py:375-377`); a caught-and-passed parse or timeout failure
  (`daemon.py:364-365`); an `AttributeError` from a non-object payload escaping
  both handlers (`daemon.py:415`); shutdown observed after slot acquisition
  (`daemon.py:380-383`); a worker thread that fails to start
  (`daemon.py:392-403`). Only the first three were in the original scope.
- **The except clauses are `(json.JSONDecodeError, socket.timeout,
  UnicodeDecodeError, ValueError)` plus `OSError`.** `AttributeError` is in
  neither. Measured: `[1,2]`, `"hi"`, and `5` each kill the command thread and
  produce a zero-byte read at the client.
- **`shutdown()` destroys the very write the requirement demands.** It calls
  `SHUT_RDWR` then `close()` on every tracked connection (`daemon.py:339-348`), so
  an in-flight worker's `sendall` cannot succeed.
- **A shipped test asserts the behavior being removed.**
  `tests/test_daemon.py:437` asserts `assertEqual(b"", received)` on the capacity
  path. It must be rewritten, not worked around.
- **A shipped test tolerates the shutdown behavior either way.**
  `tests/test_daemon.py:308-312` wraps `send_command` in `except RuntimeError:
  pass`, so it passes whether or not shutdown answers.
- **The client raises `RuntimeError` on a zero-byte read** (`daemon.py:701-702`).
  Neither CLI catch tuple includes it (`cli.py:131`, `cli.py:163`) and
  `_run_client_command` catches only `DaemonUnavailableError` (`cli.py:107`).
- **`main` has no top-level guard** (`cli.py:82-99`), and `_run_daemon`
  (`cli.py:222-237`) lets `serve_forever` propagate.
- **Connecting to an AF_UNIX socket requires write permission on the node.**
  Measured: mode `0555` gives `PermissionError` on `connect`; `0755` and `0600`
  connect for the owner. A `0755` socket is therefore *not* connectable by another
  account. The real exposure is a directory another account can write, where the
  node can be pre-created or replaced.
- **`Path.mkdir(parents=True, mode=0o700)` applies the mode to the final
  directory only** — measured: intermediates land at `0755` — and with
  `exist_ok=True` an existing directory is not re-moded.
- **`socket.SO_PEERCRED` is `17` here** and returns the peer's pid, uid, and gid
  on an accepted connection.
- **Response shapes pinned by exact-dict assertions** are all successful:
  `tests/test_daemon.py:154`, `:854`, `:864`, `:1245` (four sites, three shapes).
  Failure responses are asserted field-by-field, so adding `code` breaks nothing.
  `tests/test_cli.py:88` pins `runtime_device == "cuda"` on the success path.

## Goals / Non-Goals

**Goals:**

- An accepted connection is answered, with the two cases that cannot be answered
  stated as exceptions rather than left as undocumented behavior.
- No Murmly command that can terminate with an unhandled error.
- A failure vocabulary a program can branch on without parsing prose.
- Owner-only reachability that does not depend on the ambient permissions of a
  directory Murmly did not create.

**Non-Goals:**

- Protocol or capability advertisement. Deferred until a client needs it.
- Any new command, parameter, or delivery mode — including plumbing a parsed
  request object through to command dispatch. The daemon reads the command name
  from an object and ignores other fields; nothing carries a parameter yet, and
  the plumbing's shape should be driven by a real one.
- Partitioning connection capacity between callers, which requires caller
  identity and belongs to the next change. The one exception is that a refused
  peer must not consume capacity, which needs no identity model.
- Any change to transcription, delivery, the overlay, the installed service, or
  the hotkey binding.

## Decisions

### The over-capacity refusal is written inline on the accept loop

When no worker slot is free, the accept loop writes the refusal itself.

*Alternatives considered.* **Reserving one permit for refusals** makes the pool
size a lie and still needs an inline write when the reserve is taken. **Raising
the bound** moves the path rather than removing it. **Spawning an unbounded
refusal thread** reintroduces the exhaustion the bound exists to prevent.

The connection gets a send timeout before the write and a failed write is
discarded rather than retried, so a peer that connects and never reads cannot
block the accept loop.

The same treatment covers the two mute paths in the same function that were not
in the original scope: shutdown observed after slot acquisition, and a worker
thread that fails to start. Both already sit inside `_dispatch_connection`'s
`finally`. Care is needed with the semaphore: it is a `BoundedSemaphore` with a
release in that `finally` and another in the worker, and a double release raises
`ValueError`. The refusal write must not move either release.

### Shutdown drains, then answers whatever the drain did not

`shutdown()` sets its event, then waits a bounded interval for tracked connections
to be released by their workers, and only then force-closes whatever remains. A
worker that has read a request and observes the shutdown answers with the
shutting-down code before releasing.

**A drain alone is not enough, and measuring it is what showed why.** A
transcription runs for seconds and the drain waits half of one, so on the single
case this exists for -- the service restarting mid-transcription -- the drain
expires under the command and the caller still gets an empty read. Measured
end to end: an eight-second transcription plus a SIGTERM produced zero bytes at
the client and `Murmly daemon closed the connection before responding.` on its
stderr. That satisfies the desktop-integration rule, which asks only for a message
rather than a traceback, and violates *An accepted connection is answered*, which
asks for a response rather than an empty read.

So after the drain expires, `shutdown()` writes the shutting-down response itself
to every connection still owed one.

*Alternative considered, and why it was reconsidered.* **Having `shutdown()`
write the response itself** was rejected first because it races the worker for the
same socket. It only races while both are free to write. A single-writer claim
removes the race: whoever takes the claim writes, and the other finds the
connection already answered and writes nothing, so the connection still carries
exactly one response. **Widening the drain to cover a transcription** would make
shutdown latency depend on decode time -- up to twelve seconds on CPU -- and
systemd would kill the process before the response was written anyway.

A connection is registered as owed an answer the moment its first request byte
arrives, before the request is decoded. Registering after the decode leaves a
window in which the request has arrived, shutdown does not yet know the connection
is owed anything, and the force-close lands in the gap. A request that turns out
to be unreadable is owed an answer too, so the earlier point is also the correct
one.

A connection on which no request had been read is still closed unanswered. There
is nothing to answer, and holding shutdown open for a peer that has not spoken
would make shutdown latency depend on an idle client.

### Shape is checked, not caught

The fix is an explicit check that the decoded payload is a mapping and that the
command name is a string, not a wider except clause. A caught `AttributeError`
would report "could not read the request" for a class of bugs that are not
request problems.

The except clauses are still widened, as a backstop only: a command thread that
dies takes its response with it, so the handler converts any unexpected exception
into a response and logs it at warning. The two mechanisms answer different
questions — the check produces the correct message, the backstop guarantees some
message.

The malformed-JSON and invalid-text paths are already caught but currently
`pass`; they must be converted to responses explicitly. They are the change's
headline defect and are easy to leave behind, because they look handled.

### Codes are a small closed set, and messages do not change

Seven categories: busy, unsupported command, malformed request, over capacity,
not permitted, shutting down, and command failure. Shutting down is its own
category rather than being folded into command failure, because a caller should
retry it and must not retry the other.

Every existing `error` string keeps its exact wording, and every unsuccessful
response keeps every other field it carries today — several carry `state`
(`daemon.py:431`, `:437`, `:448`, `:466`), so the response builder takes the code,
the message, and any additional fields rather than the first two alone.

`code` becomes visible in `murmly toggle` and `murmly status` output on failure,
because `_run_client_command` prints the response verbatim (`cli.py:110`). That is
intended: the CLI does not act on the code, it shows it.

### Peer identity is checked before a worker slot is taken

The peer's identity is read on the accepted connection and compared to the
daemon's own, in the accept loop, before `_worker_slots.acquire`. Checking inside
the worker would let a foreign account occupy every slot and deny service to the
owner — the pool this change is otherwise hardening. The refusal is written with
the same send-timeout treatment as the capacity refusal.

The comparison is injectable, because exercising a genuine cross-account refusal
requires a second account and root, which the suite does not have and must not
need. The real read is exercised only for its same-account success.

Where the platform cannot report a peer's identity, the daemon logs once at
startup, reports it in diagnostics, and continues serving on file permissions
alone. Refusing to serve would make the daemon unusable on a platform whose only
fault is not offering the check; treating the unknown as permitted silently would
be the failure this decision exists to avoid.

### Permissions are set explicitly, and the configured path is validated narrowly

`Path.mkdir(mode=...)` cannot be relied on: it applies the mode to the final
directory only and skips an existing one. So Murmly creates the directory without
`exist_ok`, and `chmod`s it to `0700` only when it created it — an existing
`XDG_RUNTIME_DIR` is the session's, not Murmly's, and is already `0700`. The
socket node is `chmod`ed to `0600` after bind. The window between `bind` and
`chmod` is accepted: the containing directory is the barrier and is restricted
first.

Validation of a configured `socket_path` runs at daemon startup, before the
existing `mkdir`/`unlink` — which currently deletes whatever is at the configured
path, so it must not run against a path Murmly is about to refuse.

The predicate is **writable by group or other**, not "reachable". Because
connecting requires write permission on the node and the node is `0600`, a
directory others can merely read or traverse is not an exposure; refusing on it
would reject a `0755` `$HOME`, which is common outside Fedora's defaults. What a
writable directory permits is replacing the node, so that the owner's own commands
reach a socket Murmly does not serve.

Validation lives in the daemon, not in `load_config`. Every command loads
configuration, including `murmly doctor`, and a configuration that refuses to load
would break the diagnostics that exist to explain it. `doctor` reports the
condition; only the daemon refuses to run, and it reports the path and both
remedies rather than only the path.

### The client catches a named exception that still subclasses `RuntimeError`

`send_command` raises a dedicated exception type for "connected but received no
response", and the CLI catches that type by name rather than catching bare
`RuntimeError`, which would also swallow genuine programming errors.

It subclasses `RuntimeError` deliberately: `tests/test_daemon.py:311` catches
`RuntimeError` around `send_command` today, and subclassing keeps that test honest
without weakening the CLI, which names the precise type.

### `main` gains a top-level guard

The requirement that no command terminates with an unhandled error cannot be met
by fixing individual call sites: `load_config` runs for every subcommand before
dispatch (`cli.py:84`) and raises on a malformed `config.toml`, and this change
introduces a new refusal inside `murmly daemon`. A guard in `main` that reports
the failure and returns non-zero covers both, and covers `_run_spike`'s unguarded
recorder and transcriber as a side effect.

The guard is a backstop. Paths with a specific message — the daemon startup
refusal, the no-response failure — still report their own, because a generic
message names the wrong thing.

### Diagnostics report a failure as a section value, without changing the success shape

The transcription runtime probe moves inside a guard. The failure is reported in a
separate detail field rather than by substituting a sentinel string into
`runtime_device` and `runtime_compute_type`, because `tests/test_cli.py:88` pins
those two values on the success path and a program reading them should not have to
distinguish a device name from an error message.

## Risks / Trade-offs

- **A shipped test asserts the behavior being removed** (`tests/test_daemon.py:437`,
  `assertEqual(b"", received)`) → it is rewritten by this change, called out as a
  task rather than discovered during implementation.
- **The shutdown test tolerates both outcomes** (`tests/test_daemon.py:308-312`,
  `except RuntimeError: pass`) → a new assertion is required; the existing test
  cannot verify the fix.
- **The refusal write can delay the accept loop** → send timeout before the write,
  failed writes discarded, never retried.
- **Semaphore accounting in `_dispatch_connection`** → a refusal write placed on
  the wrong side of the acquire either leaks a permit or double-releases a
  `BoundedSemaphore`, which raises. The two existing release sites are not moved.
- **Bounded shutdown drain adds shutdown latency** → the bound is short and
  applies only to connections whose request was read; idle connections close
  immediately.
- **`shutdown()` and a worker could both write to one connection** → a
  single-writer claim, taken under the connections lock, decides which of them
  writes; the other returns without writing. Asserted by reading a connection to
  close rather than to its first newline, so a second frame would be observed.
- **`shutdown()` runs in signal-handler context on the accept-loop thread**, so
  the refusal writes it now performs run there too. Each has a send timeout and a
  discarded failure, bounding it at the worker count times that timeout. Stated
  rather than solved: the pre-existing hazard underneath it is that `shutdown()`
  can deadlock against `_connections_lock` held across `thread.start()` on the
  same thread, which predates this change and is unchanged by it.
- **Refusing to start on a group- or other-writable configured `socket_path` is a
  behavior change** → the default path is unaffected; the refusal names the path
  and both remedies; `doctor` reports it without refusing; `README.md` and
  `config.example.toml` document it.
- **The refusal message reaches the journal, not the user** → the hotkey path
  reports only that the daemon did not start. Nothing in this change fixes that,
  and the migration relies on `doctor`. Stated rather than solved.
- **Peer identity cannot be exercised cross-account in the suite** → the
  comparison is injected and the refusal path tested with a substituted identity.
- **Widening the except clauses could mask a defect** → the backstop logs at
  warning with the exception, and the shape check means correct requests never
  reach it.

## Migration Plan

No migration for the wire format. Every change is additive to unsuccessful
responses or internal; successful responses are identical.

One configuration requires action: a `daemon.socket_path` in a directory writable
by group or other. The daemon refuses to start and reports the path with both
remedies — move it under the per-user runtime directory, or remove write access
for other accounts from its directory. `murmly doctor` reports the condition
without refusing to run, so the state is discoverable before and after the
upgrade. `README.md` and `config.example.toml` state the requirement where the
option is documented.
