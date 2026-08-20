---
title: Add Speech Output
description: Give Murmly a speech output path and a bidirectional agent session that speaks text, reports where playback reached, and returns the user's words when the user interrupts
---

## Why

Murmly transcribes speech and has no way to produce it. `audio.py` opens exactly
one kind of stream, `sd.RawInputStream` (`audio.py:119`), and nothing in the
repository constructs an output stream, a playback buffer, or a way to stop audio
that is already playing. Every path is one-directional: audio in, text out.

The next thing Murmly is asked to do runs the other way. An agent produces text as
it thinks, and the person wants to hear it as it arrives rather than after it is
finished. Waiting for a complete answer before speaking wastes the time the agent
spent generating it.

That inverts the transport. Today a caller opens a connection, sends one request,
reads one response, and closes (`daemon.py:901-918`, `daemon.py:783-798`). An agent
streaming a reply needs the opposite shape: it sends text repeatedly over time and
must be told things it did not ask about — that playback reached a given message,
that everything queued has been heard, and that the person interrupted.

The interruption is the part that cannot be retrofitted onto the current transport.
**The person who interrupts is not the agent.** They reach for the dictation hotkey,
which is a different process on a different connection. The reply to that keypress
goes to the process that pressed it. The agent asked no question, so no response
frame can reach it, and it keeps generating text for a person who has stopped
listening. Nothing short of the daemon writing to the agent unprompted solves this.

There is a second gap behind the first. When the person speaks after interrupting,
their words have to reach the agent. Murmly delivers transcripts by pasting into the
focused window (`transcript-delivery`). An agent is not a focused window, so under
the current capability set there is no path from the person's voice to the agent at
all — and pasting those words into whatever window happens to hold focus during a
voice conversation is worse than useless.

## What Changes

- **Murmly speaks text.** A new speech output capability synthesizes text locally
  and plays it on an output device, with the voice, speed, and output device
  configurable and every value bounded with a stated default.
- **A speech session is a connection that stays open.** An agent connects, sends
  text as it produces it, and reads events on the same connection: which message
  started, that everything queued was heard, and that the person interrupted. Text
  sent earlier is spoken earlier, because one connection preserves order.
- **Interrupting returns the remainder.** When the person interrupts, the session is
  told which message was playing and which messages never started, so the agent
  stops generating and knows what was not heard.
- **A second hotkey dictates to the agent.** The existing hotkey continues to
  transcribe into the focused window. A second hotkey transcribes into the open
  speech session instead. Both stop speech first.
- **Speech never overlaps capture.** A hotkey press stops playback before the
  microphone opens, so Murmly cannot transcribe its own voice. This is what makes
  echo cancellation unnecessary.
- **A transcript that belongs to a session is delivered to that session and
  nowhere else.** It is not pasted, and the clipboard is not touched, because the
  window holding focus during a voice conversation is not the intended recipient.
- **BREAKING**: `command-interface`'s one-response-per-connection rule no longer
  describes every connection. One-shot commands are unchanged in every respect;
  speech sessions are a second, explicitly declared connection type that exchanges
  many frames. A caller that does not open a session sees no difference.

Speech output defaults to disabled. `status` and `toggle` are unchanged in request,
response, and meaning. Reading a highlighted selection aloud is deliberately not in
this change; the engine supports it and it needs a hotkey and a subcommand, not new
machinery.

## Capabilities

### New Capabilities

- `speech-output`: how Murmly turns text into audible speech — opting in, the
  session that carries text in and events out, the order and units speech is
  produced in, what happens when the person interrupts, where an interrupted
  session's transcript goes, what speech signals may not contain, and what
  diagnostics report about it.

### Modified Capabilities

- `command-interface`: the rule that an accepted connection receives exactly one
  response gains an explicit exception for connections that declare themselves
  speech sessions. Every existing scenario is unchanged and continues to describe
  one-shot commands.
- `transcript-delivery`: recording a delivery target before transcription gains an
  exception for a transcript produced inside a speech session, whose recipient is
  the session rather than a window.
- `desktop-integration`: the capability gains a requirement that Murmly binds more
  than one hotkey and that every existing binding rule applies to each
  independently. Three requirements that name the hotkey in the singular — taking
  effect in the running session, uninstall, and diagnostics — are restated in the
  plural.

## Impact

- `src/murmly/audio.py` — an output stream alongside the existing input stream,
  with the same preflight-and-negotiate device selection, the same lock-free
  callback discipline, and an abort that stops audio already handed to the device.
- `src/murmly/daemon.py` — a session connection type that reads many frames and
  writes many; a speech queue with per-message identity; a stop path reachable
  while speech is playing; a state meaning output is active; session lifetime tied
  to the connection; sessions must not consume the eight command worker slots.
- `src/murmly/tts.py` — new. Synthesis, voice selection, and its own runtime
  resolution. It cannot reuse `resolve_runtime` (`stt.py:169`), which validates
  compute types against a vocabulary the synthesis runtime does not share
  (`config.py:58`).
- `src/murmly/config.py` — a `[tts]` table. `load_config` reads exactly five named
  tables (`config.py:137-141`) and returns `{}` for any other (`config.py:225`), so
  a `[tts]` table added today is silently ignored.
- `src/murmly/integrations.py` — a transcript destination that is a session rather
  than a window. The existing clipboard and paste paths are untouched.
- `src/murmly/installer.py` — a second hotkey binding. The installer currently
  writes one fixed `murmly toggle` launcher with one shortcut (`installer.py:176`).
- `src/murmly/cli.py` — a second hotkey argument on `install`, and a `doctor`
  section for speech output. The report dict is flat (`cli.py:351-370`) and needs a
  nested section.
- `tests/` — the fake sounddevice module defines only `PortAudioError`,
  `query_devices`, `check_input_settings` and `RawInputStream`
  (`tests/test_audio.py:69-78`), and `FakeStream` has no `write`. Playback tests
  need output fakes before anything else can be tested.
- New dependencies: a local synthesis runtime and its model files. Speech output
  stays disabled and diagnosable when they are absent.
- No change to transcription, the overlay, the clipboard paste path, or the
  existing hotkey's behaviour.
