## Context

See `proposal.md` — Why for the incident and the two gaps it exposed.

Three pieces of the current implementation shape the approach:

`SpeechEngine._probe()` checks three things and constructs nothing: that
`kokoro_onnx` is importable, that both model files exist, and that espeak and the
ONNX providers resolve. It runs once, from `__init__`, and its result is stored in
`_unavailable_reason` for the life of the process. `available` and
`unavailable_reason` read that field.

`SpeechEngine._load_model()` builds the synthesizer lazily from `synthesize()`, and
its comment states the constraint this change has to respect: a construction that
raises leaves `_model` as `None` and hands the error to the caller, so the next
request tries again, and that failure must never reach `_unavailable_reason` because
"one transient failure recorded there would silence the daemon for the rest of its
life."

`Daemon._declare_session()` already applies the rule this change extends. It calls
`self._speech.begin(...)` inside the declaration and refuses on failure, with the
comment: "Opens the output device, so a session that cannot be given one is refused
now rather than accepted and failed once somebody is listening." The output device is
checked at declaration; the synthesis runtime is not. This change makes the two
consistent rather than introducing a new principle.

`uv` 0.12.3 is what the packaging half has to work within. It rejects
`[tool.uv] default-extras` as an unknown field, and honours `[tool.uv] default-groups`.

## Goals / Non-Goals

**Goals:**

- No `uv sync` invocation removes speech output unless it says so explicitly.
- A daemon refuses a session it cannot serve, so a caller can stay silent instead of
  committing to sound it cannot follow through on.
- A repaired environment recovers without a daemon restart, as it does today.

**Non-Goals:**

- Detecting the environment changing while a synthesizer is loaded. A loaded
  synthesizer holds its weights in memory and keeps working; there is nothing to
  detect and nothing that would fail.
- Watching the filesystem, polling, or any background reconciliation. The check
  happens at the one moment its answer is acted on.
- New reporting machinery. The daemon log and `murmly doctor` already name the cause.
- Changing `hooks/murmly-announce.py`. It already returns before playing its notes
  when a session is refused.
- Moving `cuda` to a group. See Decisions.

## Decisions

### A dependency group in `default-groups`, not an extra

`uv sync` matches the extras it is given exactly, so an extra can be dropped by
omission. A dependency group named in `[tool.uv] default-groups` is included unless
excluded explicitly, so it cannot.

Verified against uv 0.12.3 in a scratch project shaped exactly like the end state —
`cuda` as an extra, `tts` as a default group:

| Command | Result |
| --- | --- |
| `uv sync` | `tts` present |
| `uv sync --extra cuda` | `tts` present, `cuda` added — the incident command, made harmless |
| `uv sync --extra cuda --no-group tts` | `tts` removed, as asked |

*Alternative rejected — `[tool.uv] default-extras`.* Does not exist in uv 0.12.3;
the field is rejected by name. If a later uv adds it, moving back would be a smaller
change than this one, but nothing is gained by waiting.

*Alternative rejected — keep the extra and rely on `setup.sh`.* `setup.sh` already
does the right thing: `current_extras` reads what is installed and `resolve_extras`
keeps it. It was not the failure path. The failure path was `uv sync` typed by hand,
which is exactly what a default closes and what an installer cannot.

*Alternative rejected — move `cuda` to a default group too.* It is 1.8 GB and
hardware-gated, so a default that installs it is wrong for CPU-only machines. Its
absence also falls back to the CPU visibly and by documented design, where speech
output's absence is silent. The asymmetry is the point.

### Re-run the whole probe, not just `find_spec`

The incident was a missing package, but `_probe` covers two other ways speech output
becomes unavailable — deleted model files, unresolvable espeak — and both can happen
to a running daemon exactly as the package removal did. Calling `_probe()` again
covers all three through one code path rather than adding a second, narrower notion of
availability that would drift from the first.

Cost under the default configuration: one `find_spec`, two `Path.is_file()` calls, and
espeak plus provider resolution, which the existing comment records as returning above
the CUDA preload for `[tts] device = "cpu"` — distribution metadata and nothing else.
With `[tts] device = "cuda"` it repeats the preload, which is idempotent: the libraries
are already mapped by the startup probe, so the second call resolves against what is
loaded rather than loading again.

### Gate the re-probe on the synthesizer not being resident

`SpeechEngine.resident` is a plain field read. When it is true the synthesizer is
constructed and holding its weights, which is stronger evidence than any probe could
produce, and re-probing would charge every session for a condition that cannot hold.
When it is false the daemon knows nothing about the current state of the environment,
which is precisely the incident.

This also keeps the common case free. A daemon announcing turns keeps its synthesizer
resident between them under the default `unload_after_idle_s`, so the probe runs at the
first session after a start or an idle release, not at every turn.

### The re-probe's result refuses one declaration and is not stored

The result is returned to the declaration that asked for it and discarded. It is not
written to `_unavailable_reason`.

This is the constraint `_load_model` already states, applied one level up. The startup
probe's result means "speech output could not run when this daemon started" and is
allowed to be permanent because it describes a fixed moment. A re-probe's result
describes now, and now changes — this session's own repair proved it, with a day-old
daemon speaking again the moment the package returned. A stored negative would have
turned a repairable condition into one needing a restart, which is a worse failure than
the one being fixed.

A new method on `SpeechEngine` carries this: it returns a reason or `None` and touches
no state. `available` and `unavailable_reason` keep their current meaning, so
`murmly doctor`, which builds its own engine in its own process, is unaffected.

### The check goes where the `available` check already is

`_declare_session` consults it in the same place it consults `available`, before
taking `self._lock`. The lock ordering documented there — `self._lock` then
`_speech_session_lock`, because a toggle takes them that way through `_barge_in` — is
untouched, and no probe runs under either lock.

## Risks / Trade-offs

**CI's `uv sync --locked` starts installing `kokoro-onnx` and four small
dependencies** → The suite injects module absence with `injected_module` rather than
relying on the environment not having the package, so the availability tests should be
unaffected. Verify by running the suite against an environment synced the new way
before trusting that; if any test does depend on the package being absent, it is
testing its environment rather than the code and should be rewritten to inject.

**A slow probe delays session declaration** → The announce hook allows two seconds to
connect and declare. The probe stays off the resident path, and on the non-resident
path it does metadata work only under the default configuration. If `[tts] device =
"cuda"` makes it slow enough to matter, the fallback is to narrow the re-probe to
`find_spec` alone and accept that deleted model files stay a late failure.

**Speech output becomes present on machines that declined it** → The packages arrive
for everyone; the feature does not. `[tts] enabled` still gates every sound, and the
340 MB of model files are downloaded separately and stay opt-in. The installer's
question changes from "install these packages?" to "download these model files?".

**A person who wants the packages gone has a longer command** → `uv sync --no-group
tts` rather than omitting a flag. That is the trade being made deliberately: the
destructive operation becomes the one you have to ask for.

**Two agent notes and three README passages go stale on the same commit** → They are
the compensating documentation this change removes the need for. Updating them is part
of the change rather than a follow-up, because a note that still says
`uv sync --extra tts` sends a future reader to a command that no longer does anything.

## Migration Plan

1. `pyproject.toml`, then `uv lock`, then `uv sync --extra cuda --group tts` on any
   machine with a live install. The lock regeneration is the only step that has to
   happen before the others.
2. `setup.sh` reworks `current_extras` / `resolve_extras` / `sync_environment` onto
   groups. The user-facing `--no-tts` flag keeps its name and maps to `--no-group tts`
   underneath, so nobody's scripted install breaks.
3. Daemon change, which is independent of the packaging change and can land in either
   order.
4. Documentation last, once the commands it quotes are the real ones.

Rollback is reverting `pyproject.toml` and `uv.lock` and re-running `uv sync --extra
cuda --extra tts`. The daemon change rolls back on its own and has no persistent state.

## Open Questions

None. The one question worth asking — whether `setup.sh` should keep the `--no-tts`
flag name — is decided in the Migration Plan rather than deferred, because it would
change the task breakdown.
