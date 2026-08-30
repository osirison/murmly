## Why

A daemon can lose speech output without noticing. On 2026-08-28 the service started
at 23:15:18 with `kokoro-onnx` present, so the one-shot `find_spec` probe in
`SpeechEngine.__init__` passed and `_unavailable_reason` stayed `None`. At 23:18:34
a `uv sync --extra cuda` — correct-looking, and missing only `--extra tts` — removed
the package. `uv sync` matches the extras it is given exactly, so omitting one
uninstalls it.

The daemon then spent 24 hours accepting speech sessions on a probe that had stopped
being true after three minutes. The synthesizer is built lazily, so nothing tried to
import the missing package until an announcement asked for speech, by which point the
session was already open. The announcement hook opens the session before it plays its
attention notes — deliberately, because notes with no announcement behind them are
worse than silence — so every turn played three rising notes and then said nothing.
The person heard a promise the daemon could not keep, once per turn, for a day.

Two independent gaps produced that. The environment could lose the synthesizer by
omission, and the daemon could not tell that it had. Either one closed alone would
have prevented what happened, and they are worth closing separately because they fail
in different directions: packaging cannot catch a manual uninstall, and a runtime
check cannot stop the uninstall.

## What Changes

**The packaging cannot drop speech output by omission.** `tts` moves from
`[project.optional-dependencies]` to `[dependency-groups]` and is named in
`[tool.uv] default-groups`. uv includes default groups unless they are excluded
explicitly, so no sync drops the synthesizer by not mentioning it. Verified against
uv 0.12.3: with `tts` as a default group, `uv sync --extra cuda` — the exact command
that caused the incident — keeps it, and `uv sync --extra cuda --no-group tts` still
removes it deliberately. `[tool.uv] default-extras` does not exist in uv 0.12.3; it is
rejected as an unknown field, so a dependency group is the only mechanism that does
this.

`cuda` stays an extra. It is 1.8 GB and hardware-gated, and its absence falls back to
the CPU visibly and by design. Speech output is a few megabytes and its absence is
silent. The asymmetry matches the consequence.

**The daemon stops advertising a capability it has lost.** When a speech session is
declared and the synthesizer is not resident, Murmly re-probes for the synthesis
runtime before accepting. A runtime that has gone away since startup is refused with
the same code and reason as one that was never there. The announcement hook already
returns before playing its notes when a session is refused, so the person hears
nothing at all rather than notes followed by silence.

The re-probe runs only on the non-resident path, so a daemon that has spoken recently
pays nothing for it. A failed probe refuses that one declaration and MUST NOT be
recorded as the permanent unavailability reason — the existing comment on
`_load_model` is explicit that one transient failure written there would silence the
daemon for the rest of its life, and that constraint holds here for the same reason.

**BREAKING** for anyone driving `uv` directly: `--extra tts` stops being a valid way
to ask for speech output, and a plain `uv sync` now installs the synthesis packages
where before it removed them. Removing them becomes an explicit `--no-group tts`.
`./setup.sh --no-tts` keeps its name and maps onto the new flag underneath, so a
scripted install does not break. No published interface breaks either — Murmly is not
on PyPI, so there is no `murmly[tts]` consumer to strand.

Documentation shrinks rather than grows. The README repeats the "name every extra
every time" warning in three places and `setup.sh` carries a `current_extras` /
`resolve_extras` pair whose only job is never to make this mistake. Those exist to
compensate for the footgun this change removes.

Left out deliberately: no desktop notification is added. The daemon log and
`murmly doctor` already name the cause exactly, and the project has no notification
machinery to extend.

## Capabilities

### New Capabilities

None. Both layers change how existing capabilities behave rather than adding one.

### Modified Capabilities

- `speech-output`: the requirement that unavailable speech output is reported rather
  than fatal currently reasons only about startup. It gains the case where the runtime
  disappears while the daemon is running: availability is determined when a session is
  declared, not once at start, and a runtime lost since startup is refused rather than
  accepted and failed afterwards. Includes the constraint that a probe failure must not
  become the permanent reason.
- `agent-announcements`: an announcement that cannot be spoken must make no sound at
  all, attention notes included. This is the user-visible half of the incident and the
  claim the README already makes, and it is not currently stated anywhere as a
  requirement.

The packaging half carries no spec delta. Which table a dependency is declared in is
not observable behaviour, and specs describe behaviour. It is covered in `design.md`
and `tasks.md` instead rather than restated as an invented requirement.

## Impact

| Area | Change |
| --- | --- |
| `pyproject.toml` | `tts` moves to `[dependency-groups]`; `[tool.uv] default-groups` added; `cuda` unchanged |
| `uv.lock` | regenerated |
| `setup.sh` | `current_extras`, `resolve_extras`, `sync_environment`, and the `--no-tts` flag rework onto groups |
| `.github/workflows/tests.yml` | `uv sync --locked` starts installing `kokoro-onnx`, `phonemizer`, `espeakng-loader`, `joblib`, `attrs`, `dlinfo` — all small; the 340 MB of model files are separate and stay opt-in |
| `src/murmly/tts.py` | a re-probe entry point beside `available` / `unavailable_reason`, not writing `_unavailable_reason` |
| `src/murmly/daemon.py` | the `speech_session` declaration path consults it when the synthesizer is not resident |
| `README.md` | three "name every extra" passages become deletable; the install and speech-output sections change |
| `docs/agent-notes/` | `onnxruntime-gpu-cuda-version.md` and `uv-sync-cuda-runtime.md` need their sync recipes updated; `announce-hook-chime-without-speech.md` needs its cause section revised |

`hooks/murmly-announce.py` needs no change. It already returns before `play_chime()`
when a session is refused; this change is what makes the refusal happen.
