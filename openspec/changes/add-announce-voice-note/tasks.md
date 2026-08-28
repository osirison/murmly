## 1. Extraction and precedence in the announcement hook

- [ ] 1.1 Add `MAX_VOICE_NOTE_CHARACTERS = 1200` to `hooks/murmly-announce.py` alongside the existing caps, with a comment saying it bounds a runaway passage rather than shapes an announcement, and leave `MAX_SUMMARY_CHARACTERS` and `MAX_SUMMARY_SENTENCES` untouched.
- [ ] 1.2 Add `voice_notes(text)`: remove fenced code blocks first, then return every `<voice-note>` element in what remains, case-insensitive, non-greedy, across lines, attribute-free, in document order. Removing fences first is what stops a turn that discusses the convention from announcing its own example. Return the list, not a joined string, so 1.3 can tell an empty element from no element.
- [ ] 1.3 Add `announcement(text)` returning `(spoken, source)` with `source` one of `voice_note`, `suppressed`, `summary`: matches present and any of them carrying text after `plain_text()` gives the joined passage; matches present and all empty gives `("", "suppressed")`; no match gives the existing `executive_summary(text)` under `summary`.
- [ ] 1.4 In `announcement`'s summary branch, remove stray `<voice-note>` and `</voice-note>` markers before extracting, so an unclosed opener produces a summary rather than the words read aloud.
- [ ] 1.5 Add `spoken_voice_note(text)`: `plain_text()`, then cut at the last sentence terminator at or before `MAX_VOICE_NOTE_CHARACTERS`, falling back to the existing word-boundary-plus-period cut when there is no terminator. No sentence count and no opener-joining — those belong to the extract.
- [ ] 1.6 Rewrite `main()` to call `announcement()`: return 0 with `note("suppressed by the agent")` on `suppressed`, return 0 with the existing `note("nothing worth saying")` on an empty summary, and otherwise announce.
- [ ] 1.7 Pass the source through to `announce()` so the second `speak` frame is named `voice_note` or `summary` accordingly, and record which path the turn took in the `MURMLY_ANNOUNCE_LOG` line.

## 2. Delivering the convention to the agent

- [ ] 2.1 Add `hooks/murmly-voice-note.py`: prints the instruction text pinned in `design.md` and exits 0. It opens no socket, reads no configuration, and runs no subprocess. `MURMLY_ANNOUNCE_INSTRUCT=0` makes it print nothing and still exit 0.
- [ ] 2.2 Wrap the whole script in the same catch-all `try`/`except` that `murmly-announce.py` uses, so an unexpected failure exits 0 rather than surfacing at session start.

## 3. Registering both hooks as one unit

- [ ] 3.1 In `hooks/install_hooks.py`, replace the single `MARKER` with a tuple of `murmly-announce` and `murmly-voice-note`, and make `is_murmly_entry` match any of them.
- [ ] 3.2 Generalise `strip_murmly` and the `Stop`-specific parts of `install_claude`/`remove_claude` to take an event name, keeping the group-rebuilding behaviour that drops a group left with no hooks.
- [ ] 3.3 Add `--instruction-script` and register it under `hooks.SessionStart` with no matcher, in the same write as the `Stop` entry, so it runs for `startup`, `resume`, `clear`, `compact`, and `fork`. The entry omits `async` and takes its own `INSTRUCTION_TIMEOUT_SECONDS = 5`: an async hook runs in the background, so its stdout never reaches the context and the instruction silently does nothing. Comment the omission where it would otherwise look like an oversight next to the `Stop` entry.
- [ ] 3.4 Make `remove_claude` strip both events, treat an absent `SessionStart` key as nothing registered rather than an error, and keep the existing "nothing registered" message when neither event holds a Murmly entry.
- [ ] 3.5 Leave `install_copilot` and `remove_copilot` registering only `Stop`, with a comment naming why: Copilot CLI documents no hook that injects context.

## 4. setup.sh

- [ ] 4.1 In `install_announce_hook`, install `murmly-voice-note.py` into `hook_dir` beside `murmly-announce.py` with the same `install -m 0755`, and pass it as `--instruction-script`.
- [ ] 4.2 Update the two `note` lines to say a turn is announced from the agent's own `<voice-note>` when it writes one, and to name `MURMLY_ANNOUNCE_INSTRUCT=0` alongside `MURMLY_ANNOUNCE_CHIME=0`.
- [ ] 4.3 In `remove_announce_hook`, remove `murmly-voice-note.py` as well, before the `rmdir`.
- [ ] 4.4 When `copilot` is among the agents being registered, print the instruction text and say to place it in `AGENTS.md`, since nothing installs it there.

## 5. Tests

- [ ] 5.1 Extraction: a passage surrounded by prose, headings, and code announces only the passage; several passages announce in document order as one passage; the match is case-insensitive; an element written inside a fenced code block is not extracted, and a message whose only element is fenced falls back to a summary.
- [ ] 5.2 Precedence and fallback: no element announces the same summary the current tests assert; an unclosed opener announces a summary with no `voice-note` markers in it; an empty element announces nothing and does not fall back.
- [ ] 5.3 Bounds: a passage longer than `MAX_SUMMARY_CHARACTERS` and inside `MAX_VOICE_NOTE_CHARACTERS` is announced in full; one beyond the bound ends at a sentence terminator with no half word; one beyond the bound with no terminator ends at a word boundary.
- [ ] 5.4 Markup inside a passage: inline code, a link, and emphasis are announced without their markers.
- [ ] 5.5 The log line names `voice_note`, `summary`, or `suppressed` for the corresponding message.
- [ ] 5.6 The instruction script prints non-empty text and exits 0, prints nothing and exits 0 under `MURMLY_ANNOUNCE_INSTRUCT=0`, and opens no socket and starts no subprocess in either case.
- [ ] 5.7 Registration: registering writes both `Stop` and `SessionStart`; the `SessionStart` entry carries no `async` key and its own timeout, while the `Stop` entry keeps `async`; registering twice leaves one of each; removal takes both out; a settings file carrying only the older `Stop` registration removes cleanly; an unrelated `SessionStart` hook belonging to someone else survives both directions.
- [ ] 5.8 Run `uv run --no-sync python -m unittest discover -s tests` and confirm the existing announcement tests still pass unchanged.

## 6. Documentation

- [ ] 6.1 Rewrite the README announcement section's "what you hear" table so the third row is the agent's `<voice-note>` when present and the extract when not, and replace the paragraph claiming the summary is extractive and costs no tokens — it holds only for the fallback now.
- [ ] 6.2 Add the instruction text to the README with the `AGENTS.md` placement note for Copilot CLI, and add `MURMLY_ANNOUNCE_INSTRUCT` to the environment variable table.
- [ ] 6.3 Say in the README that the element is visible in the terminal because the hook reads the transcript after the turn is displayed.
- [ ] 6.4 Update the registration paragraph to say both `Stop` and `SessionStart` are written to `~/.claude/settings.json` and that `./setup.sh hooks off` takes both out.

## 7. Verification

- [ ] 7.1 Register against a real Claude Code session, confirm the instruction reaches it, and confirm a turn is announced from the `<voice-note>` rather than from the message.
- [ ] 7.2 Confirm an empty element announces nothing and that a turn with no element still announces as it did before.
- [ ] 7.3 Run `./setup.sh hooks off` and confirm `~/.claude/settings.json` is left with no Murmly entry under either event and unrelated hooks intact.
- [ ] 7.4 Run `openspec validate add-announce-voice-note --strict`.
