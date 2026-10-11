## 1. Confirm the open questions and the defect

- [x] 1.1 Get the user's answers to the draft's three open questions. Answered
      2026-10-11 and recorded under design.md, Decisions:
      - the speech-session target gets the space, overriding the draft's
        recommendation;
      - a stray leading space where Murmly cannot see the cursor is accepted;
      - an undelivered segment's clipboard copy keeps the space.
      The spec and the tasks below follow these answers.
- [x] 1.2 Reproduce the run-together paste against the unchanged code. In
      `tests/test_daemon.py`, a continuous session over `SegmentSession` with a
      recording paster shows the second segment copied without a space. Done when
      that observation is noted here, with the test that shows it.
      Observed against the unchanged daemon: `test_later_segments_are_delivered_with_one_space_before_them`
      (`tests/test_daemon.py`, over `SegmentSession`) fails with
      `['first segment', ' second segment', ' final segment'] != ['first segment', 'second segment', 'final segment']`,
      and `test_unknown_voices_keep_their_numbers_across_segments_of_one_session`
      (`tests/test_daemon_speakers.py`, a real `SpeechSession` with a recording paster) pastes
      `Speaker 2: second stranger ...` straight after the first segment with nothing between.

## 2. Tests, written before the change

- [x] 2.1 Update every session double that defines `process_recording` or
      `process_for_session` so it accepts a `separator` keyword argument and records
      the text it would deliver:
      - `tests/test_daemon.py`: the doubles near lines 96, 116, 599, 633, 821, 1205,
        1334 and 1380;
      - `tests/test_speech_session.py`: both methods, near lines 71 and 75.
      Done when the unchanged suite still passes with the updated doubles.
      Also done: `tests/test_daemon_speakers.py` needed no double change. The suite passed
      (1888 tests, OK) with the updated doubles and the unchanged daemon.
- [x] 2.2 In `tests/test_daemon.py`, add tests in mode `off`. A continuous session
      pastes its first segment with no space, and every later segment, including the
      final one delivered by the toggle, with exactly one space before it. The toggle
      response's `text` is unchanged, with no two spaces in a row. A toggle recording
      and a stop-mode recording pass no separator, even straight after an earlier
      recording. Segments that yield no text before the first transcript leave the
      first transcript without a space. Done when each fails against the unchanged
      daemon for the reason it names.
- [x] 2.3 In `tests/test_speech_session.py` (which runs a real sender against the
      daemon), extend the session double so it can return a different text per
      segment and hands `separator + text` to `deliver`. Then add tests for capture
      bound for a speech session in continuous mode:
      - The sender receives the first producing segment's `transcript` frame with
        nothing before its text.
      - Every later frame, including the final segment's, has exactly one space
        before its text.
      - A segment that yields no text sends no frame and adds no space.
      - The toggle response's `text` is still `""`.
      Done when the later-frame tests fail against the unchanged daemon and the
      others pass.
- [x] 2.4 In `tests/test_daemon.py`, next to the tests that build a real
      `SpeechSession` with a fake paster (near line 690), add tests. For
      `process_recording`:
      - with a separator, `copy_and_paste` receives the separator followed by the
        transcript;
      - when the target check refuses, `copy` receives the same text;
      - when injection is unavailable or fails, the clipboard holds the same text.
      For `process_for_session`:
      - with a separator, `deliver` receives the separator followed by the
        transcript;
      - when `deliver` reports the session gone, the clipboard copy holds the same
        text.
      In every case `ProcessingResult.text` is the bare transcript, and with no
      separator behaviour is byte-identical to today. Done when the separator tests
      fail against the unchanged code.
- [x] 2.5 In `tests/test_daemon_speakers.py`, update the assertions on
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
      A fourth speaker assertion needed the space too:
      `test_a_raising_embedder_does_not_end_a_continuous_session` (mine-only, three segments).

## 3. The change

- [x] 3.1 Give `SpeechSession.process_recording` and
      `SpeechSession.process_for_session` in `src/murmly/daemon.py` a keyword argument
      `separator: str = ""`. Each forms `separator + text` once.
      `process_recording` uses it for `copy_and_paste` and for the refusal `copy`.
      `process_for_session` uses it for `deliver` and for the clipboard fallback.
      Both return the bare `text` in `ProcessingResult`. Done when the 2.4 tests
      pass.
- [x] 3.2 In `MurmlyDaemon`, have `_process_recording` take a separator and pass it
      to whichever of the two methods the capture destination selects. In
      `_run_segment` and `_finish_toggle`, read `self._segments` under `self._lock`
      and pass `" "` when it is non-empty, `""` otherwise. `_finish_auto_stop`
      passes nothing. Done when the 2.2, 2.3 and 2.5 tests pass.
- [x] 3.3 Confirm by mutation that each path is covered. Each of these fails at least
      one test:
      - dropping the separator from `_run_segment`
      - dropping it from `_finish_toggle`
      - not passing it to `process_for_session`
      - using it only for `copy_and_paste` and not for the refusal `copy`
      - using it only for `deliver` and not for the session clipboard fallback
      - adding it to `ProcessingResult.text`
      Clear `__pycache__` around each mutation (see
      `docs/agent-notes/mutation-testing-stale-pycache.md`). Done when each result is
      recorded here.
      Results (full suite, `__pycache__` cleared before each run):
      - dropping the separator from `_run_segment`: 5 failures
      - dropping it from `_finish_toggle`: 10 failures
      - not passing it to `process_for_session`: 3 failures
      - using it only for `copy_and_paste`, not the refusal `copy`: 1 failure
      - using it only for `deliver`, not the session clipboard fallback: 1 failure
      - adding it to `ProcessingResult.text` (window path): 7 failures

## 4. Manual

- [x] 4.1 In `manual/pause-to-finish.md`, extend the `continuous` bullet. Each
      segment after the first is typed with a space in front of it, so segments do
      not run together. The first segment, and a recording ended by the hotkey or by
      `stop`, are typed as they are. Add one sentence saying that if you move the
      cursor, for example to a new line, during a session, the next segment still
      starts with a space. Done when the page states both.
- [x] 4.2 In `manual/typing-only-your-own-voice.md`, in "With continuous listening",
      say that segments are typed with a space between them in every mode, with a
      short `label-everyone` example (`… work You: Yeah, maybe`). Done when the
      paragraph says it.
- [x] 4.3 Check the "What it cannot do" list on `manual/typing-only-your-own-voice.md`
      for a run-together or missing-space limitation and remove it if present. At
      proposal time no such entry exists, so this is expected to be a confirmation
      only. Done when
      `grep -n -i "run together\|space" manual/typing-only-your-own-voice.md` shows
      no stale limitation.
- [x] 4.4 In `manual/for-developers.md`, add to the `transcript` frame row or the
      notes below it what a caller receives in continuous mode:
      - each segment arrives as its own frame;
      - every frame after the first one that carried text begins with a single
        space, so frames joined as they arrive read as one text;
      - a caller that shows frames separately can strip that space.
      Done when the page says it.
- [x] 4.5 Build the manual with the command in `docs/agent-notes/building-the-manual.md`
      (`uvx --from "mkdocs==1.6.1" --with "mkdocs-material==9.7.7" mkdocs build --strict -d <scratch dir>`),
      never `uv run mkdocs`. Done when the build passes with no warnings.
      `mkdocs build --strict` passed (built in 0.6 s, no warnings other than mkdocs-material's
      standing notice about MkDocs 2.0).

## 5. Verify

- [x] 5.1 Run the full suite with the main checkout's interpreter, as
      `docs/agent-notes/unittest-discover-in-a-worktree.md` describes. Use
      `PYTHONPATH=<worktree>/src /home/qp/Cloud/Projects/murmly/.venv/bin/python3 -m unittest discover -s tests`
      after confirming `murmly.__file__` is under the worktree. Never run `uv sync` or
      plain `uv run`. Done when the suite passes.
      `murmly.__file__` resolved under the worktree. 1906 tests, OK (skipped=34).
- [x] 5.2 Run `openspec validate --all --strict`. Done when it passes.
      12 passed, 0 failed.
- [x] 5.3 Live check on the desktop with a verification daemon
      (`docs/agent-notes/running-a-verification-daemon.md`), not the installed service.
      In a text editor with `auto_transcribe = "continuous"`, dictate three sentences
      with pauses in speaker mode `off`, then in `label-everyone`. Done when the
      editor shows one space between every pair of segments and none before the
      first, and the result is recorded here.
      Done 2026-10-11 by the user, a different way: they installed the service from
      this branch's own environment rather than a verification daemon, and dictated
      with `auto_transcribe = "continuous"` and speaker mode `mine-only` (their
      config at the time). They reported that it works. The service journal confirms
      the branch build ran and loaded the model during the check; the pasted text
      itself was not seen, since Murmly logs no transcript text. Modes `off` and
      `label-everyone` were not checked live; the tests cover all three modes.

## Workflow follow-up

- Archive this change after it is merged, and only after `add-speaker-recognition`
  has been archived. The two deltas touch different capabilities, but this one's
  scenarios name `mine-only` and `label-everyone`, which only that change's
  `speaker-recognition` capability defines.
