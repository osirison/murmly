# Hearing when your coding assistant finishes

Here is what it sounds like. You look away from the terminal. When the agent
finishes its turn, you hear three rising notes, then one short sentence
naming the agent, the project, and the branch — "Claude Code in murmly, on
branch main." — and then the agent's own summary of what it did.

| | |
| --- | --- |
| three rising notes | so you know a message is arriving before any words start |
| one short sentence | the agent, the project, and the branch: "Claude Code in murmly, on branch main." |
| the agent's `<voice-note>` | as it wrote it, up to about a minute |
| — or a summary of its message | when it wrote no voice note: whole sentences from the opening, capped at about twenty seconds |

Code fences, tables, links, and headings are stripped before anything is
spoken; they read as noise out loud. A voice note is spoken as written
otherwise — the agent wrote it to be heard, so there is nothing to extract
from it. A summary is extractive: the message's own opening sentences,
rather than a second model's paraphrase, so nothing is invented and no
tokens are spent producing it.

An agent can drive murmly's speech output directly, but murmly ships only
one thing that does this for you automatically: a hook that fires when a
coding agent finishes a turn, so you can look away from the terminal and
still know when it is done and what happened. What it speaks is written by
the agent — murmly asks it to end each turn with a `<voice-note>` element
holding a few sentences of plain spoken English, and speaks that. An agent
that writes nothing of the sort is summarised from its message instead,
which is what this hook did before voice notes existed, and what it still
falls back to.

## Before you start

This needs speech output turned on first. See
[Making murmly speak](making-murmly-speak.md).

## Switching it on

```bash
./setup.sh hooks            # offers whichever agents are installed
./setup.sh hooks claude     # or name one
./setup.sh hooks copilot
./setup.sh hooks off        # unregister
```

This registration step is `setup.sh` only, on Linux — `bootstrap.ps1` has no
equivalent yet. On Windows, register the hook directly with the coding
assistant's own mechanism, pointing it at `hooks/murmly-announce.py`; the
hook itself runs the same way, and exits cleanly, on every platform.

Claude Code and GitHub Copilot CLI both fire a `Stop` event at the end of a
turn, and both hand it the same thing — a JSON payload naming a JSONL
transcript — so one script serves both. What differs between them is the
row shape inside the transcript, and the hook reads either.

## When it stays quiet

An empty `<voice-note>` element announces nothing at all. That is how an
agent says a turn was not worth interrupting you for, and it is the one
case that does not fall back to a summary.

The hook stays silent, and exits 0, for every ordinary reason it cannot
speak:

- speech output is disabled or unavailable
- the clock is inside your [quiet
  window](settings.md#tts-quiet-hours)
- another client is already holding the session
- a capture is running
- the voice note is empty
- there was nothing worth saying in the last message

Silent means silent in all of these. The chime plays only once the
speech session has been accepted, so a turn that will not be announced
makes no sound at all — a signal in front of an announcement that never
arrives is worse than nothing, and at one per turn it teaches you to
ignore the signal.

It never fails a turn. It also detaches into its own process before
speaking, so an announcement never holds the agent up.

## Asking the agent for a voice note

Claude Code is told automatically. `./setup.sh hooks claude` registers a
second hook, on `SessionStart`, that prints the convention into the
session's context. It runs for every source — startup, resume, clear,
compact, fork — because a session that lost the convention at a compaction
would go back to summaries halfway through the day without saying so. It is
registered synchronously, and that is deliberate: an async hook runs in the
background, where its output never reaches the context, so it would
instruct nobody while appearing to work.

Copilot CLI documents no hook whose output reaches the model, so it cannot
be told this way. `./setup.sh hooks copilot` prints the text below; put it
in your `AGENTS.md`:

```
Murmly speaks the end of your turn aloud, for a person who is not looking at the
terminal.

End each turn with one <voice-note> element holding what that person needs to
hear: two or three sentences of plain spoken English saying what you did,
whether it worked, and anything they have to decide. Lead with the outcome. No
file paths, no identifiers, no code, no markdown -- it is read out, not
displayed.

Everything outside the element is for the screen and is not spoken. Leave the
element empty to say nothing aloud for a turn that does not need announcing.
```

The `<voice-note>` element stays visible in your terminal. The hook reads
the turn's transcript after the agent has already displayed it, so there is
nothing it could strip; what it can do is ask for the note at the end of the
message, where it is easiest to ignore.

`MURMLY_ANNOUNCE_INSTRUCT=0` turns the whole convention off and leaves you
with the summary.

An element inside a code fence — such as the instruction block above — is
an example rather than a note, and is not spoken.

## Changing what you hear

| Variable | Effect |
| --- | --- |
| `MURMLY_ANNOUNCE_CHIME=0` | speak without the notes |
| `MURMLY_ANNOUNCE_LOG=<path>` | append a line per turn naming what it said — the agent's own voice note, an extract, or suppressed — or why it stayed quiet |
| `MURMLY_ANNOUNCE_AGENT=<name>` | name the agent yourself rather than inferring it from the transcript |
| `MURMLY_ANNOUNCE_INSTRUCT=0` | stop asking the agent for a voice note, leaving every announcement a summary |

The notes are generated rather than shipped. To use your own, put a WAV at
`~/.local/share/murmly/announce-chime.wav` and it is played instead. They go
to the default output device — on Linux through `pw-play`, `paplay`, or
`aplay`, whichever is present, and on Windows through the standard playback
API — rather than through `[tts] output_device`, which is
the speech path.

## What it writes, and how to undo it

| Path | What it is |
| --- | --- |
| `~/.claude/settings.json` | Claude Code registration, under both `Stop` and `SessionStart`, merged into the file |
| `~/.claude/settings.json.murmly-backup` | the previous `settings.json`, kept beside it |
| `~/.copilot/hooks/murmly-announce.json` | Copilot CLI registration, `Stop` only, a file of its own |

Nothing is written into your `CLAUDE.md` or any other file holding your own
instructions. Running the setup twice leaves one registration of each, not
two, and `./setup.sh hooks off` or `./setup.sh uninstall` takes every
registration out.

## Where to next

If an announcement does not arrive, see
[When something goes wrong](troubleshooting.md). To turn speech output on in
the first place, see [Making murmly speak](making-murmly-speak.md).
