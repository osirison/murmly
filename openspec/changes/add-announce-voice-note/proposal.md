## Why

The announcement hook speaks whatever the agent happened to write for the screen.
It strips the markup, takes the opening sentences, and stops at roughly twenty
seconds. That text was composed for someone looking at a terminal: it names files
and symbols, leans on the code block underneath it, and carries the qualifiers a
reader can skim past. Spoken, it is jargon read at dictation speed, and the cap
routinely cuts it before the outcome arrives. There is nothing in the message that
was written for the ear, so the hook is guessing which part of a text was.

The agent is the only party that knows what the turn amounted to. Asking it to say
so, in one short passage written to be heard, replaces the guess with an authored
voice note.

## What Changes

- A coding agent marks the passage it wants spoken with a `<voice-note>` element in
  its final message. The hook speaks that passage and nothing else from the message.
- Content inside the element is spoken as written, rather than reduced to its
  opening sentences. Sentence and character caps that exist to bound an extract do
  not apply to a passage that was authored to be heard.
- A message with no `<voice-note>` is announced exactly as it is today: the same
  extractive summary of the last agent message. An installation that upgrades
  without instructing its agent hears no change.
- `./setup.sh hooks` registers a second hook alongside the existing `Stop` hook: a
  Claude Code `SessionStart` hook that writes the convention to stdout, which Claude
  Code adds to the session's context. Nothing is written into the user's own
  instruction files.
- GitHub Copilot CLI has no hook that injects context, so it is instructed by a
  documented snippet the user pastes into `AGENTS.md`. Its `Stop` announcement reads
  `<voice-note>` the same way once it is written.
- `./setup.sh hooks off` and `./setup.sh uninstall` remove both registrations, and
  registering twice still leaves one of each.
- `MURMLY_ANNOUNCE_LOG` records which of the two paths a turn took.

## Capabilities

### New Capabilities

- `agent-announcements`: what a coding agent's finished turn is announced as — how
  an agent marks the passage written to be heard, what the hook speaks when one is
  present and when none is, how the convention reaches the agent, and the rule that
  none of it may fail or delay the agent's turn.

### Modified Capabilities

None. The daemon is unchanged: the hook remains an ordinary speech-session client,
and every requirement in `speech-output` applies to it exactly as it does today.

## Impact

- `hooks/murmly-announce.py` — extract `<voice-note>` from the last agent message,
  prefer it over the extractive summary, keep the summary as the fallback.
- `hooks/install_hooks.py` — register and remove a Claude Code `SessionStart` hook
  in addition to `Stop`, through the same merge, backup, and strip machinery.
- A new hook script that emits the convention, installed beside `murmly-announce.py`.
- `setup.sh` — install and remove the additional script and registration.
- `tests/test_announce_hook.py` — extraction, precedence, fallback, and the
  install/remove behaviour of the second registration.
- `README.md` — the announcement section currently describes the summary as
  extractive and states that no tokens are spent producing it. Both stop being true
  when a voice note is present.

No change to `src/murmly`, the command socket protocol, the configuration file, or
`murmly doctor`. No new dependencies.
