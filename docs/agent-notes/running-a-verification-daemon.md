---
title: Running a second murmly daemon by hand, to verify something against a live one
description: Its socket must go under /run/user/<uid>, because a session scratchpad path exceeds the 108-byte AF_UNIX limit and the failure is reported as an unrelated-looking probe error; and starting it with an empty PATH is what keeps a verification capture off the clipboard and out of the focused window
trigger: murmly daemon, murmly --config, murmly doctor, murmly toggle, verify against a real daemon, setsid murmly daemon

depends_on: src/murmly/daemon.py, src/murmly/cli.py, src/murmly/integrations.py
recorded: 2026-08-28
verified_on: Fedora 44, Python 3.14, RTX 3080 Laptop, Plasma 6.7.4 Wayland
---

# Running a second murmly daemon by hand

Verifying anything about what the daemon holds -- model residency, idle release,
GPU memory -- needs a daemon running the branch under test. Start a second one
on its own socket rather than restarting the user's installed service. Two
preconditions decide whether that works, and neither announces itself.

## The socket must go under `/run/user/<uid>`

**Symptom:** a daemon started with `--config` pointing at a config whose
`socket_path` is inside an agent session directory never appears, and
`murmly doctor` reports

```
Unable to ask the Murmly daemon what it holds: AF_UNIX path too long
```

**Fix:** put the socket where the real one goes.

```toml
[daemon]
socket_path = "/run/user/1000/murmly-live-test.sock"
```

`AF_UNIX` paths are capped at 108 bytes by the kernel, and a session scratchpad
path is already over 120 before a filename is added. Nothing in murmly checks
the length, so the limit surfaces as an `OSError` from `connect` and `bind`
wherever those happen to be called.

**Why it was not obvious:** the standing instruction is to keep temporary files
in the session scratchpad, which is correct for every other file and wrong for
this one. The message names the cause but reads like a probe failure rather than
a configuration mistake, because that is the section of the report it lands in.

## Start it with an empty PATH so a capture cannot deliver

**Symptom:** verifying residency needs one real transcription -- the idle
countdown is armed by a recording ending, and nothing else arms it -- and
stopping a capture copies the transcript and presses Ctrl+V into whatever window
has focus.

**Fix:** give the daemon a PATH with none of the delivery tools on it.

```bash
setsid env PATH=/var/empty PYTHONPATH="$PWD/src" \
  /path/to/.venv/bin/python3 -m murmly --config test.toml daemon \
  > daemon.log 2>&1 < /dev/null &
```

Delivery shells out to `wl-copy`/`xclip` and to a paste injector, so every one
of them fails to be found and the toggle answers `"delivered": false` with the
transcript discarded. The daemon starts and transcribes normally -- there is a
test for starting without clipboard tools -- so this costs nothing but the
delivery being verified, which is not what a residency check is looking at.

Set `overlay.enabled = false` in the same config, or the recording indicator
appears on the user's desktop for the length of the capture.

**Clean up by pid, not by socket.** `pkill -TERM -f "murmly --config <path>
daemon"` then remove the socket file. Check `nvidia-smi
--query-compute-apps` after: the process is gone from `ps` a moment before its
GPU memory is returned, so a reading taken immediately still lists it.
