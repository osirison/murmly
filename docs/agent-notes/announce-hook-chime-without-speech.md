---
title: Notes but no speech means synthesis failed after the session was accepted
description: The announcement chime plays only once the speech session is open, so hearing it and then silence narrows the fault to synthesis; the packaging and startup-probe causes are both closed now, which makes the symptom evidence of something else
trigger: hooks/murmly-announce.py, murmly-announce.py, MURMLY_ANNOUNCE_LOG, MURMLY_ANNOUNCE_FOREGROUND, announcement not spoken, chime but no speech, uv sync --no-group tts

depends_on: hooks/murmly-announce.py, src/murmly/tts.py, src/murmly/daemon.py, pyproject.toml
recorded: 2026-08-29
updated: 2026-08-30
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
  -> failed: kokoro-onnx is required for speech output. Run `uv sync` before
     enabling it; speech output is installed by default and only
     `--no-group tts` leaves it out.
```

## Why a running daemon does not notice

`SpeechEngine.__init__` probes once with `importlib.util.find_spec("kokoro_onnx")`
and stores the result in `_unavailable_reason` for the life of the process. The
model itself is built lazily — `_load_model()` is reached only from
`synthesize()`.

**The packaging hole that made this common is closed.** `tts` moved from an
extra to a `[dependency-groups]` group listed in `[tool.uv] default-groups`, so
`uv sync --extra cuda` no longer touches it — see the "`uv sync --extra cuda`
used to remove speech output" section of
`docs/agent-notes/onnxruntime-gpu-cuda-version.md`. `kokoro-onnx` now survives
every sync that does not name `--no-group tts`.

**The daemon also re-probes.** Declaring a `speech_session` calls
`unavailable_reason_now()`, which asks the synthesizer again instead of trusting
the startup probe — unless it is already resident, in which case holding its
weights is stronger evidence than a probe and none is paid. A runtime lost since
startup is therefore caught here, before the session is accepted and before the
chime plays: the hook gets a refusal and makes no sound at all, rather than the
notes-then-silence this note is named for. On a current build the specific
pattern this note describes — a daemon advertising a capability a background
sync quietly removed, while never having built the model even once — is closed.

A synthesizer that is already resident still skips this check, because
residency is the proof. Something that breaks the runtime underneath an
already-loaded model — deleted model files, an undone `onnxruntime` swap, a
manual uninstall — still reaches `synthesize()` unchecked and still produces
chime-then-silence. Hearing that symptom on a current build is evidence of one
of those, not of the packaging hole; the table under "Read the chime as a
probe" is still the right first move.

**Fix:**

```bash
cd /path/to/murmly          # the repository root: the daemon's venv lives there
uv sync                     # or: uv sync --extra cuda, on a CUDA install
```

**No restart is needed.** `_load_model` leaves `self._model` as `None` when
construction raises and hands the error to the caller, keeping a failed rebuild
retryable on purpose, and that failure is deliberately never written into
`_unavailable_reason`. A daemon that has been failing for a day picks the package
up on the next request. The same statelessness is why the re-probe above
recovers immediately too: `unavailable_reason_now()` calls `_probe()` fresh every
time the synthesizer is not resident, so the very first `speech_session` request
after the fix lands succeeds.

**Why it was not obvious:** the daemon's own error text is exact and names the
command that fixes it, but the hook writes it nowhere unless `MURMLY_ANNOUNCE_LOG`
is set, and the sync that caused it succeeded quietly hours earlier.

**Dating it is still the fastest move**, whatever removed the package. Compare
the package metadata against when the daemon started, rather than guessing:

```bash
find .venv/lib/python3.*/site-packages -maxdepth 1 -name '*.dist-info' \
  -printf '%T+ %f\n' | sort | tail
systemctl --user show -p ActiveEnterTimestamp murmly
```

Anything written after that timestamp changed the environment under the running
process. On 2026-08-28 it was seven `nvidia_*_cu12` directories three minutes
after the start -- a `uv sync --extra cuda` from the days when that also removed
`kokoro-onnx`. That particular cause is closed; the comparison is not specific to
it.
