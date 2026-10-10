## 1. Confirm the open questions and the defect

- [ ] 1.1 Get the user's answers to design.md Open questions 1 to 3 (speech-session
      frames, a stray space when the cursor or window cannot be known, the space on a
      refused segment's clipboard copy). If any answer differs from the
      recommendation, update the matching scenario in
      `specs/live-transcription/spec.md` and the tests below before writing code.
      Done when design.md records each answer.
- [ ] 1.2 Reproduce the run-together paste against the unchanged code. In
      `tests/test_daemon.py`, a continuous session over `SegmentSession` with a
      recording paster shows the second segment copied without a space. Done when
      that observation is noted here, with the test that shows it.

## 2. Tests, written before the change

- [ ] 2.1 Update every session double that defines `process_recording` so it accepts
      a `separator` keyword argument and records the text it would paste:
      `tests/test_daemon.py` (the doubles near lines 96, 116, 599, 633, 821, 1205, 1334
      and 1380) and `tests/test_speech_session.py` (near line 71). Done when the
      unchanged suite still passes with the updated doubles.
- [ ] 2.2 In `tests/test_daemon.py`, add tests in mode `off`. A continuous session
      pastes its first segment with no space, and every later segment, including the
      final one delivered by the toggle, with exactly one space before it. The toggle
      response's `text` is unchanged, with no two spaces in a row. A toggle recording
      and a stop-mode recording pass no separator, even straight after an earlier
      recording. Segments that yield no text before the first transcript leave the
      first transcript without a space. Done when each fails against the unchanged
      daemon for the reason it names.
- [ ] 2.3 In `tests/test_daemon.py`, add a test that capture bound for a speech
      session in continuous mode sends each segment's transcript with nothing added
      before it. Done when it passes before and after the change, because it
      guards against the space leaking into that target.
- [ ] 2.4 In `tests/test_daemon.py`, next to the tests that build a real
      `SpeechSession` with a fake paster (near line 690), add tests:
      - With a separator, `copy_and_paste` receives the separator followed by the
        transcript.
      - When the target check refuses, `copy` receives the same text.
      - When injection is unavailable or fails, the clipboard holds the same text.
      - `ProcessingResult.text` is the bare transcript in all three cases.
      - With no separator, behaviour is byte-identical to today.
      Done when the separator tests fail against the unchanged code.
- [ ] 2.5 In `tests/test_daemon_speakers.py`, update the assertions on
      `self.paster.pasted` for continuous sessions to expect the leading space on
      later segments. These are in
      `test_continuous_mode_delivers_only_the_owner_per_segment`,
      `test_a_dropped_continuous_segment_keeps_listening_and_is_not_counted` and
      `test_unknown_voices_keep_their_numbers_across_segments_of_one_session`. Leave
      the `response["text"]` and `daemon._segments` assertions unchanged. Add a
      mine-only test where a dropped segment between two kept ones leaves exactly one
      space before the last. Add a label-everyone test where the second segment is
      pasted as `" You: ..."`. Done when the updated and new assertions fail against
      the unchanged daemon and the unchanged ones still pass.

## 3. The change

- [ ] 3.1 Give `SpeechSession.process_recording` in `src/murmly/daemon.py` a keyword
      argument `separator: str = ""`. It forms `separator + text` once, uses it for
      `copy_and_paste` and for the refusal `copy`, and returns the bare `text` in
      `ProcessingResult`. Leave `process_for_session` unchanged. Done when the 2.4
      tests pass.
- [ ] 3.2 In `MurmlyDaemon`, have `_process_recording` take a separator and pass it
      only for window-bound capture. In `_run_segment` and `_finish_toggle`, read
      `self._segments` under `self._lock` and pass `" "` when it is non-empty, `""`
      otherwise. `_finish_auto_stop` passes nothing. Done when the 2.2, 2.3 and 2.5
      tests pass.
- [ ] 3.3 Confirm by mutation that each path is covered. Each of these fails at least
      one test:
      - dropping the separator from `_run_segment`
      - dropping it from `_finish_toggle`
      - passing it for session-bound capture
      - using it only for `copy_and_paste` and not for the refusal `copy`
      - adding it to `ProcessingResult.text`
      Clear `__pycache__` around each mutation (see
      `docs/agent-notes/mutation-testing-stale-pycache.md`). Done when each result is
      recorded here.

## 4. Manual

- [ ] 4.1 In `manual/pause-to-finish.md`, extend the `continuous` bullet. Each
      segment after the first is typed with a space in front of it, so segments do
      not run together. The first segment, and a recording ended by the hotkey or by
      `stop`, are typed as they are. Add one sentence saying that if you move the
      cursor, for example to a new line, during a session, the next segment still
      starts with a space. Done when the page states both.
- [ ] 4.2 In `manual/typing-only-your-own-voice.md`, in "With continuous listening",
      say that segments are typed with a space between them in every mode, with a
      short `label-everyone` example (`… work You: Yeah, maybe`). Done when the
      paragraph says it.
- [ ] 4.3 Check the "What it cannot do" list on `manual/typing-only-your-own-voice.md`
      for a run-together or missing-space limitation and remove it if present. At
      proposal time no such entry exists, so this is expected to be a confirmation
      only. Done when
      `grep -n -i "run together\|space" manual/typing-only-your-own-voice.md` shows
      no stale limitation.
- [ ] 4.4 In `manual/for-developers.md`, add to the `transcript` frame row or the
      notes below it that in continuous mode each segment arrives as its own frame,
      with its text as transcribed and nothing added between frames. Done when the
      page says it.
- [ ] 4.5 Build the manual with the command in `docs/agent-notes/building-the-manual.md`
      (`uvx --from "mkdocs==1.6.1" --with "mkdocs-material==9.7.7" mkdocs build --strict -d <scratch dir>`),
      never `uv run mkdocs`. Done when the build passes with no warnings.

## 5. Verify

- [ ] 5.1 Run the full suite with the main checkout's interpreter, as
      `docs/agent-notes/unittest-discover-in-a-worktree.md` describes. Use
      `PYTHONPATH=<worktree>/src /home/qp/Cloud/Projects/murmly/.venv/bin/python3 -m unittest discover -s tests`
      after confirming `murmly.__file__` is under the worktree. Never run `uv sync` or
      plain `uv run`. Done when the suite passes.
- [ ] 5.2 Run `openspec validate --all --strict`. Done when it passes.
- [ ] 5.3 Live check on the desktop with a verification daemon
      (`docs/agent-notes/running-a-verification-daemon.md`), not the installed service.
      In a text editor with `auto_transcribe = "continuous"`, dictate three sentences
      with pauses in speaker mode `off`, then in `label-everyone`. Done when the
      editor shows one space between every pair of segments and none before the
      first, and the result is recorded here.

## Workflow follow-up

- Archive this change after it is merged, and only after `add-speaker-recognition`
  has been archived. The two deltas touch different capabilities, but this one's
  scenarios name `mine-only` and `label-everyone`, which only that change's
  `speaker-recognition` capability defines.
