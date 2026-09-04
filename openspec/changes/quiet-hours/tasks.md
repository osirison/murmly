## 1. Reading the setting

- [x] 1.1 Add `_quiet_window(value)` to `src/murmly/config.py`, returning
      `(start, end, rejected)`: it accepts `"HH:MM-HH:MM"` in 24-hour local time
      with optional surrounding whitespace, returns `(None, None, None)` for an
      absent or empty value, returns `(None, None, str(value))` for anything it
      cannot read, and returns `(None, None, str(value))` when start equals end —
      that parsed and it means no window, but a report showing none against a file
      that plainly sets one leaves its owner nothing to look at.
- [x] 1.2 Add `tts_quiet_start: time | None`, `tts_quiet_end: time | None`, and
      `tts_quiet_rejected_value: str | None` to `MurmlyConfig`, defaulting to no
      window, and populate them from `[tts] quiet_hours` in `load_config`.
- [x] 1.3 Add `is_quiet_at(start, end, now)` — a pure function, half-open, with
      `start > end` spanning midnight and no window when either bound is `None`.
      Place it beside the config it reads rather than in the daemon, so the tests
      for it need no daemon.
- [x] 1.4 Tests in `tests/test_config.py`: each accepted form; a leading zero and a
      missing one; the empty string and an absent setting; a non-string value; an
      hour past 23 and a minute past 59; a value with seconds; a reversed-looking
      window that is really a wrap-around; start equal to end yielding no window
      while still reported as the value that was not honoured; and the rejected
      value carried as a string for a value TOML gave as a type other than string.
- [x] 1.5 Tests for `is_quiet_at` covering, with a supplied `now`: inside a plain
      window, outside it, exactly at the start (quiet), exactly at the end (not
      quiet), inside a wrap-around window before midnight and after it, outside a
      wrap-around window in the afternoon, and no window configured at any hour.

## 2. Refusing the session

- [x] 2.1 Add `SPEECH_QUIET_HOURS = "speech_quiet_hours"` to `CommandCode` in
      `src/murmly/daemon.py`, documented alongside `SPEECH_DISABLED` and
      `SPEECH_UNAVAILABLE` as the refusal a caller should retry later.
- [x] 2.2 Give the daemon an injectable `now: Callable[[], datetime]` defaulting to
      `datetime.now`, separate from the existing monotonic `clock`, and say in its
      docstring why the two are not the same thing.
- [x] 2.3 In `_declare_session`, check the window immediately after the
      `tts_enabled` check and before `self._speech.available` — so no filesystem or
      library probe is paid for a refusal a clock decided, and so the answer at
      02:00 is quiet hours rather than `BUSY`. Refuse with the new code and a
      message naming the resume time, as in `Quiet hours until 07:00.`
- [x] 2.4 Tests in `tests/test_speech_session.py`: a declaration inside the window
      is refused with the new code; one outside it is accepted; a wrap-around window
      refuses before midnight and after it; no window configured accepts at every
      hour tested; the refusal happens with speech output enabled and the
      synthesizer available, so it is the window and nothing else doing it; and the
      refusal is returned without the output device being opened.
- [x] 2.5 Test that a session accepted before the window begins keeps speaking after
      the clock passes the start, and that text sent on it afterwards is still
      spoken.
- [x] 2.6 Test that the quiet refusal wins over `BUSY`: a declaration inside the
      window while the daemon is not idle is refused with the quiet code.

## 3. Staying silent

- [x] 3.1 Test in `tests/test_announce_hook.py` that a declaration refused with the
      new code produces no chime and no other sound, and that the hook exits
      successfully — the existing chime-after-declaration order should make this
      pass without touching `hooks/murmly-announce.py`. If it does not, fix the
      order rather than the test.
- [x] 3.2 Test that the hook's diagnostic line names the quiet-hours code, so a
      person reading it can tell quiet hours from a refusal for any other reason.

## 4. Reporting it

- [x] 4.1 In `speech_output_diagnostics` (`src/murmly/cli.py`), report
      `quiet_hours` (the configured string, or `None`), `quiet_hours_in_force`, and
      `quiet_hours_rejected_value` when there is one. Carry all of them through the
      early returns for speech output disabled and for speech output unavailable,
      for the reason the surrounding comment already gives about
      `unload_after_idle_s`.
- [x] 4.2 Give `speech_output_diagnostics` the same injectable `now`, so the report
      can be tested at a fixed hour.
- [x] 4.3 Tests in `tests/test_cli.py`: the window reported and in force; the window
      reported and not in force; no window configured; a rejected value reported
      beside no window in use; and all of these still reported when speech output is
      disabled and when it is unavailable.

## 5. Documenting it

- [x] 5.1 Add `quiet_hours` to the `[tts]` block of `config.example.toml` with its
      format, its empty default, what a wrap-around window means, and an explicit
      note that the value must be quoted — an unquoted `22:00-07:00` is not valid
      TOML and loses the whole file, not one setting.
- [x] 5.2 Add `### tts.quiet_hours { #tts-quiet-hours }` to `manual/settings.md`
      among the other `[tts]` settings, and update the whole-file listing near the
      top of that page so it still matches `config.example.toml`.
- [x] 5.3 Mention the window in `manual/announcements.md` where it says what stops
      an announcement being spoken, and in `manual/troubleshooting.md` beside the
      other reasons Murmly says nothing — naming `quiet_hours_in_force` in the
      doctor report as the way to tell.
- [x] 5.4 Check `README.md` for a list of speech settings and add it there if one
      exists; leave it alone if the README points at the manual instead.

## 6. Finishing

- [x] 6.1 Run the suite and confirm it passes with no test depending on the hour it
      was run at. In a worktree the documented command leaves an unpopulated
      `.venv`, so borrow the main checkout's interpreter as
      `docs/agent-notes/unittest-discover-in-a-worktree.md` records:
      `PYTHONPATH="$PWD/src:$PWD/tests" "$MAIN/.venv/bin/python3" -m unittest
      discover -s tests`. 1566 tests pass. The hour-independence was checked by
      re-running the whole suite under six timezones — 02:00, 04:57, 05:56, 15:55,
      21:28 and 22:59 local — rather than argued: three of those fall inside a
      typical night window.
- [x] 6.2 Run `openspec validate quiet-hours --strict`.
