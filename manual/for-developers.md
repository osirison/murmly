# For developers

This page is for a program driving murmly, not a person driving murmly. It
covers the two protocols a client speaks: the speech session, and the command
socket that starts and stops the microphone.

## Opening a speech session

Nothing in the CLI opens a speech session. `murmly toggle-session` routes a
transcript to a session that is already open; the session itself is opened by
a client connecting to the command socket, which is what an agent is. To try
speech output by hand, write that client — the protocol below is all of it.

One connection, newline-delimited JSON in both directions. Declare the session
and wait for the acknowledgement before sending anything else:

```json
{"command": "speech_session"}
```

Murmly answers `{"ok": true, "session": "speech"}`, or one refusal frame and a
closed connection. `speech_disabled`, `speech_unavailable`,
`speech_quiet_hours`, and `speech_session_in_use` are the reasons specific to
speech. A declaration can also be refused for the reasons any command can —
`command_failed`, `over_capacity`, `shutting_down`, `malformed_request`, or
`unsupported_command` — and with `busy` while a capture is running, because
accepting one then would reopen the loudspeaker into a live microphone. `busy`
is transient: retry once capture ends.

`speech_quiet_hours` is transient too, but on a longer scale: the clock is
inside the window set by [`tts.quiet_hours`](settings.md#tts-quiet-hours), and
the message names the time speech resumes. Retry after that, not immediately.
It is a separate code from `speech_disabled` and `speech_unavailable` precisely
so you can tell "come back later" from "this will not work until someone
changes something".

Treat any frame carrying `"ok": false` as a refusal and report its message,
rather than matching the speech reasons alone. One session is open at a time.

## Refusals inside an open session

Refusals also arrive **inside** an open session. A frame that is not JSON, is
not an object, or names a command Murmly does not know is answered with
`{"ok": false, "code": ..., "error": ...}`, and the session stays open — so a
sender that dispatches on `frame["event"]` alone will meet a frame that has no
`event` key. Branch on whichever of `event` and `ok` the frame carries.

## Frames a sender may send

| Frame | Meaning |
| --- | --- |
| `{"command": "speak", "name": "m1", "text": "..."}` | speak this, and call it `m1`. `name` must be a non-empty string, and one frame must stay under 65536 bytes — a larger one is refused with `malformed_request` and the session is closed, so split a long passage across several frames |
| `{"command": "end"}` | no more text is coming |
| `{"command": "cancel"}` | stop speaking and discard what is queued |

## Frames murmly sends

Murmly sends these without being asked:

| Frame | Meaning |
| --- | --- |
| `{"event": "started", "name": "m1"}` | `m1` has begun to be audible |
| `{"event": "heard_all"}` | everything queued was heard, and the sender had said it was finished |
| `{"event": "interrupted", "playing": "m2", "pending": ["m3"], "code": "speech_interrupted"}` | speech stopped: `m2` was cut off and `m3` never started. Sent when the person presses a capture hotkey **and** in answer to the sender's own `cancel`, so a sender that stops generating on this event must not mistake the echo of its own cancel for a barge-in. `playing` is null when nothing was audible |
| `{"event": "transcript", "text": "..."}` | what the person said, when the session hotkey started the capture |
| `{"event": "failed", "name": "m4", "error": "..."}` | `m4` could not be produced; the session continues. `name` is null when the failure is the output device itself rather than a named piece of text |
| `{"event": "shutting_down"}` | Murmly is stopping |

## Four things a sender should know

- **Wait for the acknowledgement.** The declaration is read by the same path
  that reads every other command, which reads one frame; text pipelined behind
  the declaration in a single write arrives as one unreadable request.
- **The position reported is what was heard, not what was produced.** Murmly
  produces sentence five while sentence four is audible, so a position taken
  from production would claim the person heard something they did not.
- **Events carry names, never text.** The one exception is the transcript,
  which is the whole point of delivering it.
- **Read continuously.** Events queue per session and never hold up playback,
  so a sender that stops reading is disconnected once 64 frames are
  outstanding — with no refusal frame, just a closed connection.

## The command socket

`murmly toggle`, `murmly toggle-session`, and `murmly status` reach the murmly
service over a UNIX socket at `daemon.socket_path`. That socket starts and
stops the microphone, so it is restricted to the account the service runs as:
the socket is created `0600`, any directory murmly creates for it is `0700`,
and a connection whose reported account differs from the service's is refused.

The default path is under `$XDG_RUNTIME_DIR`, which is already private and
needs no action. If you set `daemon.socket_path` yourself, no directory a
lookup of it passes through may be writable by group or other, and every one
of them must be owned by you or by root. That covers the whole path, not just
the directory holding the socket, and it follows any symbolic link on the way:
renaming a directory replaces everything under it, replacing a link redirects
everything reached through it, and an owner can grant itself write access at
any time.

The service refuses to start otherwise, because such a directory lets another
account create or replace the socket node, and your own `murmly toggle` would
then reach a socket murmly does not serve. The refusal names the directory at
fault. Either move the socket back under `$XDG_RUNTIME_DIR`, or correct that
directory — `chmod go-w` for a writable one, and for one another account owns
there is nothing to chmod, so move the socket instead:

```bash
chmod go-w /path/to/the/directory/it/named
```

A directory other accounts can only read or traverse is fine, because the
socket node itself is owner-only, and connecting to a UNIX socket requires
write permission on it. A shared directory with the sticky bit set — `/tmp`
and the like, where one account cannot remove another's entries — is fine
above the deepest directory on the path that already exists, though not as
that directory itself. So `/tmp/murmly-yours/murmly.sock` is served once you
have created `murmly-yours` as `0700`, and refused while it is still missing:
until it exists, anyone can create it first.

`murmly doctor` reports the condition under `command_socket` without refusing
to run, so you can check the state before restarting the service.

---

To turn speech output on in the first place, see [making murmly
speak](making-murmly-speak.md). For the ready-made hook that drives this
protocol for a coding assistant, see [hearing when your coding assistant
finishes](announcements.md). For the socket path setting itself, see [the
setting](settings.md#daemon-socket-path).
