---
title: Notes but no speech means the daemon lost kokoro-onnx after it started
description: The announcement chime plays only after the speech session is open, so hearing it narrows the fault to synthesis; a running daemon keeps advertising speech output that a later sync removed
trigger: hooks/murmly-announce.py, murmly-announce.py, MURMLY_ANNOUNCE_LOG, MURMLY_ANNOUNCE_FOREGROUND, announcement not spoken, chime but no speech, uv sync --extra cuda

depends_on: hooks/murmly-announce.py, src/murmly/tts.py, src/murmly/daemon.py, pyproject.toml
recorded: 2026-08-29
---

# Notes but no speech means the daemon lost `kokoro-onnx` after it started

**Symptom:** every turn ends with the three rising notes and then nothing is
spoken. `murmly doctor` run afterwards reports speech output as unavailable, but
the daemon has been running for hours and never logged a change.

## Read the chime as a probe

`announce()` in `hooks/murmly-announce.py` opens the speech session *before* it
plays the notes, deliberately — a chime with no announcement behind it is worse
than silence. So what you hear localises the fault without any instrumentation:

| What you hear | Where the fault is |
| --- | --- |
| nothing at all | the daemon refused the session, or is not running: speech disabled, another client holding it, a capture in progress, an empty `<voice-note>` |
| notes, then silence | the session was accepted and synthesis failed afterwards |

Only the second row needs investigating. The first is every quiet path the hook
documents, and all of them are intended.

## Run the hook by hand

The hook forks and closes its descriptors, so nothing it does is observable by
default. Two variables make it a foreground command that explains itself:

```bash
python3 - <<'PY' > /tmp/payload.json
import json
print(json.dumps({
    "last_assistant_message": "text <voice-note>spoken part</voice-note>",
    "cwd": "/home/qp/Cloud/Projects/murmly",
    "transcript_path": "",
}))
PY

MURMLY_ANNOUNCE_FOREGROUND=1 MURMLY_ANNOUNCE_CHIME=0 \
MURMLY_ANNOUNCE_LOG=/tmp/ann.log \
  python3 ~/.local/share/murmly/hooks/murmly-announce.py < /tmp/payload.json
cat /tmp/ann.log
```

`last_assistant_message` is enough on its own: the announcement is made from the
payload, and `transcript_path` is now only read to name the agent. That is what
makes a hand-built payload a complete harness here rather than an approximation.
Run the copy under `~/.local/share/murmly/hooks/`, not the one in the repository
— that is what Claude Code actually executes, and it can be older than the tree.

For testing the hook's *reading* of a real session rather than its speaking, see
`docs/agent-notes/testing-a-stop-hook-needs-two-turns.md`; a single `claude -p`
run hides the faults that need a previous turn to exist.

The log line for this failure names its own remedy:

```
  -> failed: kokoro-onnx is required for speech output. Run `uv sync --extra tts` ...
```

## Why a running daemon does not notice

`SpeechEngine.__init__` probes once with `importlib.util.find_spec("kokoro_onnx")`
and stores the result in `_unavailable_reason` for the life of the process. The
model itself is built lazily — `_load_model()` is reached only from
`synthesize()`. So a `uv sync` that drops the `tts` extra after the daemon started
leaves it advertising a capability it can no longer deliver: `speech_session` is
accepted on the startup probe, the chime plays, and the first import happens far
too late to refuse.

`uv sync --extra cuda` on its own is the usual cause. It is exact, so it
uninstalls `kokoro-onnx`; see the "name every extra every time" section of
`docs/agent-notes/onnxruntime-gpu-cuda-version.md`, which describes the same
hazard reached through the `onnxruntime` swap.

**Fix:**

```bash
cd /path/to/murmly          # the repository root: the daemon's venv lives there
uv sync --extra cuda --extra tts
```

**No restart is needed.** `_load_model` leaves `self._model` as `None` when
construction raises and hands the error to the caller, keeping a failed rebuild
retryable on purpose, and that failure is deliberately never written into
`_unavailable_reason`. A daemon that has been failing for a day picks the package
up on the next request.

**Why it was not obvious:** the daemon's own error text is exact and names the
command that fixes it, but the hook writes it nowhere unless `MURMLY_ANNOUNCE_LOG`
is set, and the sync that caused it succeeded quietly hours earlier. Date it from
the package metadata rather than guessing:

```bash
find .venv/lib/python3.*/site-packages -maxdepth 1 -name '*.dist-info' \
  -printf '%T+ %f\n' | sort | tail
```

`nvidia_*_cu12` directories written after the daemon's `systemctl --user show -p
ActiveEnterTimestamp murmly` are a `--extra cuda` sync that ran underneath it.
