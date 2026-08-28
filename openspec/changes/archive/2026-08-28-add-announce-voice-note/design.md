## Context

See `proposal.md` — Why. What shapes the approach is the four constraints already
in place around the hook:

- The hook is an ordinary client of the speech session. It gets one session at a
  time, it must exit 0 whatever happens, and it detaches before speaking so the turn
  is never held up. None of that changes.
- `last_agent_message()` selects the message an announcement is made from, reading
  both Claude Code's and Copilot CLI's transcript row shapes. It was to have been left
  alone. Verifying the change on a live session showed it selects the wrong turn, and
  the decision below replaces where the message comes from while leaving how it is
  read as a fallback.
- `plain_text()` strips markdown but not HTML elements, so a `<voice-note>` element
  reaching the extract path would be spoken with its brackets. That has to be handled
  explicitly rather than assumed away.
- `install_hooks.py` merges into `~/.claude/settings.json`, a file of the person's
  own settings, and backs it up before every write. A second registration goes
  through that same machinery rather than beside it.

Two facts were checked against the agents' documentation rather than recalled:

- Claude Code adds a `SessionStart` hook's plain-text stdout to the session context.
  Its documentation states the exceptions to stdout being invisible are
  `UserPromptSubmit`, `UserPromptExpansion`, and `SessionStart`, "where Claude Code
  adds plain-text stdout as context that Claude can see and act on." `SessionStart`
  matchers are `startup`, `resume`, `clear`, `compact`, and `fork`.
- GitHub Copilot CLI's hook events are `sessionStart`, `sessionEnd`,
  `userPromptSubmitted`, `preToolUse`, `postToolUse`, `errorOccurred`, and
  `agentStop`. Its documentation describes hooks as running shell commands and does
  not document any of them injecting context into the model. This design therefore
  assumes Copilot cannot be instructed automatically. If that turns out to be wrong,
  the instruction script already exists and only needs a second registration.

## Goals / Non-Goals

**Goals:**

- One marked passage in, one announcement out, with the extract path intact
  underneath it and reached by exactly the messages that reached it before.
- The convention arrives without Murmly writing into a file the person maintains.
- Both registrations install, reinstall, and remove as one unit.

**Non-Goals:**

- No second model, no paraphrasing, no summarisation of the marked passage. The
  agent wrote it; it is spoken.
- No change to the daemon, the socket protocol, the configuration file, or
  `murmly doctor`. The hook learns nothing new about Murmly's state.
- No attempt to hide the element from the terminal. The hook reads the transcript
  after the fact and cannot alter what was displayed.
- No paragraph-level prosody. The speech-output spec guarantees sentence pauses;
  a blank line inside a marked passage buys nothing beyond that.

## Decisions

### The element is `<voice-note>`

Chosen over `<tts>`, `<audio>`, and a ```` ```voice-note ```` fence.

`<audio>` is an HTML element, which invites a renderer or the agent itself to treat
it as one. `<tts>` names the machinery rather than the purpose, and the instruction
reads better when the name says what the agent is being asked to write.

The fence was the real alternative, and it loses on one point: it renders as a code
block, and the whole premise is that this passage is prose rather than code. It wins
on one point, which is worth stating because it is the cost of the choice — the
existing `plain_text()` strips fences, so an un-upgraded hook meeting an instructed
agent would silently drop a fenced note, whereas it speaks an XML element's brackets
aloud. That mismatch needs one hook installed by hand at a path the installer does
not manage, since `./setup.sh hooks` replaces the script and registers the
instruction in the same run. It is accepted rather than designed around.

### Extraction, and what counts as a passage

Fenced code blocks are removed from the message before anything is searched for.
Without that, every turn spent discussing this convention announces the example
rather than the note, which is not a corner case — it is the whole of the work that
builds it. The cost is that a marked passage cannot contain a fence, which is
already true in spirit: the passage is prose, and `plain_text()` would drop the
fence a sentence later anyway.

What is then matched, case-insensitively, non-greedy, across lines: an opening
`<voice-note>` with no attributes, its content, and a closing `</voice-note>`. Every
match is taken, in document order, and joined into one passage — an agent that wrote
two of them meant both, and announcing only one silently discards what it said.

The passage then goes through `plain_text()` unchanged. An agent asked for prose
that supplies a path in backticks gets the path spoken rather than the backticks;
one that supplies a table gets it dropped. Both are the right outcome and neither
needs new code.

Three results are distinguished, not two, and the announcement's log line names
which occurred:

| Message | Announced | Log |
| --- | --- | --- |
| a passage with text in it | that passage | the agent's own |
| a passage with nothing in it | nothing | suppressed by the agent |
| no passage, or an unclosed opener | the extract, as today | an extract |

Suppression is why the empty case is not folded into the fallback. An agent that
wrote the element knows the convention, so an empty one is a decision that the turn
is not worth interrupting for. Falling back to an extract there would override the
only instruction the agent gave.

An unclosed opener is the reverse: it is a truncated or malformed message, not a
decision, so it takes the fallback. Before the fallback extracts anything, stray
`<voice-note>` and `</voice-note>` markers are removed from the text, so the failure
mode is a summary of the message rather than the word "voice note" read aloud.

### The marked passage keeps a bound of its own

`MAX_SUMMARY_CHARACTERS = 400` and `MAX_SUMMARY_SENTENCES = 4` stay exactly as they
are and continue to bound the extract. They are there because an extract stops being
informative — an opening that continues into detail written for the screen — and
that reasoning does not apply to a passage written to be heard.

A marked passage gets `MAX_VOICE_NOTE_CHARACTERS = 1200`, three times the extract's
bound and roughly a minute of speech at the same rate. It is not there to shape the
announcement; it is there so an agent that emits a runaway passage cannot hold the
speech session against the person, who can only take it back by pressing a capture
hotkey. There is no sentence count: sentence counting is an extract's heuristic for
where an opening ends.

Over the bound, the passage is cut at the last sentence terminator at or before it;
with no terminator to cut at, it falls back to the existing behaviour of cutting at a
word boundary and closing with a period.

### The context sentence is still spoken first

"Claude Code in murmly, on branch main." keeps its place ahead of a marked passage,
and is still sent as its own `speak` frame under the name `context`. The branch is
the part the agent does not reliably know, and it is what distinguishes one of
several concurrent agents from another when the person is not looking.

The cost is some redundancy: an agent that opens its note with "I finished the fix in
murmly" is preceded by a sentence saying much the same. The instruction text tells it
to lead with the outcome rather than with where it was, which is enough.

The second frame's name changes from `summary` to `voice_note` when a marked passage
is what is being spoken. Interruption events name the piece that was cut off, so the
name is the only place the distinction can show up in a signal that, by the
speech-output spec, may not carry the text itself.

### The convention arrives by a `SessionStart` hook

A new script installed beside `murmly-announce.py` prints the instruction and exits
0. `./setup.sh hooks claude` registers it under `hooks.SessionStart` with no matcher,
so it runs for `startup`, `resume`, `clear`, `compact`, and `fork` alike. Registering
for every source is deliberate: without `compact`, a long session loses the
convention at the moment its context is rebuilt, and the announcements would quietly
degrade to extracts partway through a day's work.

The entry is registered **without** `async`, unlike the `Stop` entry beside it, and
this is not a detail that can be tidied up later. A `SessionStart` hook delivers its
instruction as plain-text stdout; Claude Code's documentation states that an async
hook "runs in the background without blocking" and that it does not even enforce the
hook's timeout, so there is no assembled context left for background stdout to be
added to. Registered async, the hook runs, prints, succeeds, and instructs nobody —
a failure with no symptom except announcements that stay extracts forever. It also
takes a shorter timeout than the announcement's 15 seconds, because 15 seconds is
cheap for a detached announcer and not cheap for something every session start waits
on. Five is enough for a script whose entire body is a `print`.

The requirement that nothing installed may delay a turn is written to admit exactly
this: the announcement may not hold the turn up, and the instruction may run before
the session on the condition that it does no work — no socket, no configuration, no
subprocess. That is why the script is a constant and not a probe.

Rejected: writing into `~/.claude/CLAUDE.md` or a project `AGENTS.md`. Those files
are the person's, an installer editing them cannot know what it is displacing, and
`hooks off` would then have to unpick text from a file it did not write. The hook
route reuses the merge, backup, marker, and strip machinery that already exists and
is removed by the same command that removes the announcement.

Rejected: making the instruction conditional on speech output being available. There
is no cheap way to ask — `status` reports only the daemon's state, reading the
configuration file duplicates parsing that belongs to the daemon, and opening a
speech session to test it would steal the session from an announcement still being
spoken. `MURMLY_ANNOUNCE_INSTRUCT=0` suppresses the instruction instead, matching how
`MURMLY_ANNOUNCE_CHIME=0` already suppresses the notes.

The text, pinned here so it is not re-litigated during implementation:

> Murmly speaks the end of your turn aloud, for a person who is not looking at the
> terminal.
>
> End each turn with one `<voice-note>` element holding what that person needs to
> hear: two or three sentences of plain spoken English saying what you did, whether
> it worked, and anything they have to decide. Lead with the outcome. No file paths,
> no identifiers, no code, no markdown — it is read out, not displayed.
>
> Everything outside the element is for the screen and is not spoken. Leave the
> element empty to say nothing aloud for a turn that does not need announcing.

### The message comes from the payload, not the transcript

Added after the rest of this design was implemented, because verifying it on a live
session produced an announcement that did not match what was on screen.

`last_agent_message()` reads the transcript back to front. The transcript does not
hold the finished turn's message at the moment the turn ends, so reading it back to
front finds the *previous* turn's. Every announcement was one turn late. It went
unnoticed because the extract of a previous turn is still plausible English about
this project, and because the only prior test of it was a single-turn `claude -p`
run, where there is no previous turn to find and the failure presents as silence.

Reproduced in a two-turn session with a synchronous probe on `Stop`:

| | transcript's last message | payload's `last_assistant_message` |
| --- | --- | --- |
| after turn one | *(empty -- no assistant text rows yet)* | `ALPHA ... <voice-note>This is turn one, ALPHA.</voice-note>` |
| after turn two | `ALPHA ... <voice-note>This is turn one, ALPHA.</voice-note>` | `BRAVO ... <voice-note>This is turn two, BRAVO.</voice-note>` |

The payload was right both times, and carries the message whole -- the element
verbatim, not a rendering of it. So the message is taken from
`last_assistant_message` when it is there, and `last_agent_message()` stays as the
fallback.

The fallback is not ceremony. Copilot CLI's payload has not been verified to carry
an equivalent field, and neither have older Claude Code versions; a hook that
required the field would announce nothing at all for either. Reading it through the
existing `payload_field` helper also covers the camelCase alias, which is how
Copilot's `agentStop` sends the same fields.

Two things this fixes beyond the misattribution. The first turn of a session, which
had nothing in its transcript to find and so announced nothing, now announces. And
the transcript is no longer on the path for anything but `agent_name`, so a message
that never lands in the JSONL is still announced.

Rejected: waiting or retrying until the transcript catches up. It puts an unbounded
delay in front of every announcement to recover a message the payload already
handed over.

### Both registrations are one unit

`install_hooks.py` grows a second script argument and registers both events in one
write. `is_murmly_entry` recognises `murmly-announce` and `murmly-voice-note`, and
the group-rebuilding strip already in place runs over both event lists, so a group
left empty disappears rather than accumulating.

Removal is written so that an installation carrying only the older `Stop`
registration uninstalls cleanly: a `SessionStart` key that is absent is not an error,
and the existing "nothing registered" path already covers it.

Copilot CLI keeps its single-file `Stop` registration unchanged. `./setup.sh hooks
copilot` prints the instruction and says to put it in `AGENTS.md`; the README carries
the same text. Its `Stop` announcement reads a marked passage identically once the
person has placed it, because the extraction is in the shared script.

## Risks / Trade-offs

- **The element is visible in the terminal.** The hook reads the transcript after the
  turn is displayed and cannot alter what was shown. → Accepted. The instruction asks
  for the note at the end of the message, where it is easiest to ignore, and
  `MURMLY_ANNOUNCE_INSTRUCT=0` turns the whole convention off for someone who would
  rather have the extract.
- **The instruction costs tokens on every session, including compactions.** → About a
  hundred tokens, against an announcement that is otherwise assembled from text
  written for a different purpose. The opt-out is the mitigation.
- **The payload field may be absent on an agent or version not tested here.** →
  Copilot CLI and older Claude Code were not verified to send it. The fallback to the
  transcript is what makes that a degradation to the previous behaviour, one turn
  late, rather than silence.
- **An agent may ignore the instruction, or drift from it over a long session.** →
  The fallback is the current behaviour, so drift degrades to today rather than to
  silence, and the log line names which path each turn took so drift is visible
  rather than guessed at.
- **An agent may put secrets or a customer's data in the note.** → No new exposure:
  the note is a subset of a message the extract path would already have drawn from,
  and the speech-output spec's rule that signals carry names rather than text is
  unchanged.
- **A new hook script is a new thing that can fail at session start.** → It prints a
  constant and exits 0; it opens no socket, reads no configuration, and runs no
  subprocess. A failure to print leaves the session starting normally and the
  announcement falling back to the extract.
- **Copilot's instruction is manual, so its announcements stay extracts until the
  person acts.** → Stated in the README rather than papered over. If Copilot gains
  context injection, the script is already there to register.

## Migration Plan

No data, no schema, no protocol. `./setup.sh upgrade` replaces the announcement
script and re-runs the registration, which adds the `SessionStart` entry to an
installation that had only `Stop`. An installation that never re-registers keeps
working: its hook has no extraction, its agent is never instructed, and it announces
extracts exactly as before.

Rolling back is `./setup.sh hooks off` followed by installing the previous script, or
`MURMLY_ANNOUNCE_INSTRUCT=0` to keep the new hook and stop instructing the agent.
