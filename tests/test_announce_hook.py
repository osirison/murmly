"""The Stop hook that announces a finished turn, and the installer that wires it up.

Both scripts are loaded by path rather than imported: they ship as standalone
files that run under the system Python with no virtual environment, which is
what lets them keep working after `setup.sh uninstall --purge`.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import wave

from module_stubs import injected_module, removed_module

REPO = Path(__file__).resolve().parents[1]


def load(file_name: str) -> types.ModuleType:
    """Load a shipped hook script by path, the way the agents will run it."""
    path = REPO / "hooks" / file_name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


announce = load("murmly-announce.py")
install_hooks = load("install_hooks.py")


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


class PayloadTests(unittest.TestCase):
    """Claude Code and Copilot's `Stop` agree; Copilot's `agentStop` alias does not."""

    def test_snake_case_is_read(self) -> None:
        payload = {"transcript_path": "/tmp/a.jsonl", "cwd": "/work"}
        self.assertEqual("/tmp/a.jsonl", announce.payload_field(payload, "transcript_path", "transcriptPath"))
        self.assertEqual("/work", announce.payload_field(payload, "cwd"))

    def test_camel_case_is_read(self) -> None:
        payload = {"transcriptPath": "/tmp/b.jsonl"}
        self.assertEqual("/tmp/b.jsonl", announce.payload_field(payload, "transcript_path", "transcriptPath"))

    def test_a_missing_field_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual("", announce.payload_field({}, "transcript_path", "transcriptPath"))
        self.assertEqual("", announce.payload_field({"cwd": 12}, "cwd"))


class TranscriptTests(unittest.TestCase):
    #: The shape Claude Code writes, taken from a real transcript.
    CLAUDE = [
        {"type": "user", "message": {"content": "do the thing"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working on it."}]}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash"},
                    {"type": "text", "text": "Done. The suite passes."},
                ]
            },
        },
    ]
    #: The shape Copilot CLI writes, taken from a real events.jsonl on 1.0.44.
    COPILOT = [
        {"type": "session.start", "data": {}},
        {"type": "assistant.message", "data": {"content": "", "toolRequests": [{"name": "bash"}]}},
        {"type": "assistant.message", "data": {"content": "All four checks pass."}},
        {"type": "assistant.turn_end", "data": {}},
    ]

    def transcript(self, rows: list[dict]) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            handle.write(jsonl(rows))
        return handle.name

    def test_claude_text_after_a_tool_call_is_found(self) -> None:
        rows = announce.transcript_rows(self.transcript(self.CLAUDE))
        self.assertEqual("Done. The suite passes.", announce.last_agent_message(rows))

    def test_copilot_skips_a_message_that_was_only_tool_calls(self) -> None:
        rows = announce.transcript_rows(self.transcript(self.COPILOT))
        self.assertEqual("All four checks pass.", announce.last_agent_message(rows))

    def test_each_agent_is_named_from_its_own_transcript(self) -> None:
        claude = announce.transcript_rows(self.transcript(self.CLAUDE))
        copilot = announce.transcript_rows(self.transcript(self.COPILOT))
        self.assertEqual("Claude Code", announce.agent_name(claude))
        self.assertEqual("Copilot", announce.agent_name(copilot))

    def test_an_unreadable_transcript_is_empty_rather_than_fatal(self) -> None:
        self.assertEqual([], announce.transcript_rows("/nonexistent/transcript.jsonl"))
        self.assertEqual("", announce.last_agent_message([]))

    def test_a_line_that_is_not_json_is_skipped(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        with handle:
            handle.write("not json\n")
            handle.write(jsonl([{"type": "assistant.message", "data": {"content": "Still read."}}]))
        rows = announce.transcript_rows(handle.name)
        self.assertEqual("Still read.", announce.last_agent_message(rows))


class SummaryTests(unittest.TestCase):
    def test_code_tables_and_markup_are_not_spoken(self) -> None:
        summary = announce.executive_summary(
            "## Result\n\n"
            "The **fix** is in `audio.py` and the [PR](https://example.com) is open. "
            "Everything passes.\n\n"
            "| a | b |\n| - | - |\n\n"
            "```python\nprint('not spoken')\n```\n"
        )
        self.assertNotIn("|", summary)
        self.assertNotIn("`", summary)
        self.assertNotIn("#", summary)
        self.assertNotIn("not spoken", summary)
        self.assertIn("The fix is in audio.py", summary)

    def test_a_short_opener_takes_the_sentence_after_it(self) -> None:
        summary = announce.executive_summary("Done. The daemon now exits cleanly at logout.")
        self.assertEqual("Done. The daemon now exits cleanly at logout.", summary)

    def test_more_than_a_summary_is_left_on_the_screen(self) -> None:
        sentences = " ".join(f"This is sentence number {index}, and it says something." for index in range(12))
        summary = announce.executive_summary(sentences)
        self.assertLessEqual(len(summary), announce.MAX_SUMMARY_CHARACTERS)
        self.assertLessEqual(summary.count("."), announce.MAX_SUMMARY_SENTENCES)

    def test_one_long_sentence_is_cut_at_a_word(self) -> None:
        summary = announce.executive_summary("word " * 200)
        self.assertLessEqual(len(summary), announce.MAX_SUMMARY_CHARACTERS)
        self.assertTrue(summary.endswith("."))

    def test_nothing_to_say_says_nothing(self) -> None:
        self.assertEqual("", announce.executive_summary(""))
        self.assertEqual("", announce.executive_summary("```\njust code\n```"))


class VoiceNoteTests(unittest.TestCase):
    """The passage the agent marked to be heard, and what happens without one."""

    def test_only_the_marked_passage_is_announced(self) -> None:
        spoken, source = announce.announcement(
            "## What I did\n\n"
            "Rewrote the shutdown path in `daemon.py` so the stream closes once.\n\n"
            "<voice-note>\n"
            "The daemon was crashing at logout. It is fixed and the tests pass.\n"
            "</voice-note>\n"
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("The daemon was crashing at logout. It is fixed and the tests pass.", spoken)
        self.assertNotIn("daemon.py", spoken)
        self.assertNotIn("What I did", spoken)

    def test_several_passages_are_announced_in_the_order_they_were_written(self) -> None:
        spoken, source = announce.announcement(
            "<voice-note>First thing.</voice-note>\n"
            "Some prose in between that is not spoken.\n"
            "<voice-note>Second thing.</voice-note>"
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("First thing. Second thing.", spoken)
        self.assertNotIn("in between", spoken)

    def test_the_element_is_matched_whatever_its_case(self) -> None:
        spoken, source = announce.announcement("<Voice-Note>It still works.</VOICE-NOTE>")
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("It still works.", spoken)

    def test_an_element_inside_a_fence_is_an_example_rather_than_a_note(self) -> None:
        """Every turn that documents this convention would otherwise announce it."""
        spoken, source = announce.announcement(
            "Here is how you write one, and it took an hour to get right.\n\n"
            "```\n<voice-note>the example, not the note</voice-note>\n```\n\n"
            "<voice-note>The convention is documented now.</voice-note>"
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("The convention is documented now.", spoken)

    def test_a_fenced_element_alone_falls_back_to_a_summary(self) -> None:
        spoken, source = announce.announcement(
            "The convention is written up and the hook reads it correctly now.\n\n"
            "```\n<voice-note>the example, not the note</voice-note>\n```\n"
        )
        self.assertEqual(announce.SOURCE_SUMMARY, source)
        self.assertNotIn("the example", spoken)
        self.assertIn("The convention is written up", spoken)


class VoiceNoteFallbackTests(unittest.TestCase):
    """No element, or one that was never closed, is the announcement as it was."""

    MESSAGE = "Done. The daemon now exits cleanly at logout."

    def test_no_element_announces_the_same_summary_as_before(self) -> None:
        spoken, source = announce.announcement(self.MESSAGE)
        self.assertEqual(announce.SOURCE_SUMMARY, source)
        self.assertEqual(announce.executive_summary(self.MESSAGE), spoken)

    def test_an_opener_that_was_never_closed_is_not_read_out(self) -> None:
        spoken, source = announce.announcement(f"<voice-note>\n{self.MESSAGE}")
        self.assertEqual(announce.SOURCE_SUMMARY, source)
        self.assertNotIn("voice-note", spoken)
        self.assertNotIn("<", spoken)
        self.assertIn("The daemon now exits cleanly", spoken)

    def test_an_empty_element_announces_nothing_and_does_not_fall_back(self) -> None:
        """An agent that wrote the element knows the convention, so it meant this."""
        for message in (
            "<voice-note></voice-note>",
            f"{self.MESSAGE}\n\n<voice-note>\n   \n</voice-note>",
            f"{self.MESSAGE}\n\n<voice-note>```\nonly code\n```</voice-note>",
        ):
            with self.subTest(message=message):
                spoken, source = announce.announcement(message)
                self.assertEqual(announce.SOURCE_SUPPRESSED, source)
                self.assertEqual("", spoken)


class VoiceNoteBoundTests(unittest.TestCase):
    """The bound is a stop, not a shape."""

    def test_a_passage_longer_than_a_summary_is_announced_in_full(self) -> None:
        passage = " ".join(
            f"This is sentence number {index} and it is written to be heard." for index in range(10)
        )
        self.assertGreater(len(passage), announce.MAX_SUMMARY_CHARACTERS)
        self.assertLess(len(passage), announce.MAX_VOICE_NOTE_CHARACTERS)

        spoken, source = announce.announcement(f"<voice-note>{passage}</voice-note>")
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual(passage, spoken)
        self.assertGreater(len(spoken), announce.MAX_SUMMARY_CHARACTERS)

    def test_over_the_bound_it_ends_at_a_sentence(self) -> None:
        passage = "This sentence is one of very many just like it. " * 60
        spoken = announce.spoken_voice_note(passage)
        self.assertLessEqual(len(spoken), announce.MAX_VOICE_NOTE_CHARACTERS)
        self.assertTrue(spoken.endswith("."))
        self.assertNotIn("  ", spoken)
        # Whole sentences, so the last one is not cut off part way through.
        self.assertTrue(spoken.endswith("This sentence is one of very many just like it."))

    def test_over_the_bound_with_no_sentence_it_ends_at_a_word(self) -> None:
        spoken = announce.spoken_voice_note("word " * 500)
        self.assertLessEqual(len(spoken), announce.MAX_VOICE_NOTE_CHARACTERS)
        self.assertTrue(spoken.endswith("."))
        self.assertTrue(spoken.replace(".", "").strip().endswith("word"))

    def test_markup_inside_a_passage_is_not_read_out(self) -> None:
        spoken, source = announce.announcement(
            "<voice-note>The **fix** is in `audio.py` and the [PR](https://example.com) is open.</voice-note>"
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("The fix is in audio.py and the PR is open.", spoken)


class FinishedTurnTests(unittest.TestCase):
    """Which turn gets announced.

    The transcript lags: when a turn ends it does not yet hold that turn's
    message, so reading it back to front finds the previous turn's and every
    announcement is one turn late. Reproduced in a two-turn session before this
    was fixed, which is where ALPHA and BRAVO come from -- turn two's hook read
    ALPHA out of the transcript while the payload already held BRAVO.
    """

    PREVIOUS = "ALPHA\n\n<voice-note>This is turn one, ALPHA.</voice-note>"
    FINISHED = "BRAVO\n\n<voice-note>This is turn two, BRAVO.</voice-note>"

    def rows(self, *messages: str) -> list[dict]:
        return [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": m}]}}
            for m in messages
        ]

    def test_the_payload_wins_over_a_transcript_that_disagrees(self) -> None:
        message = announce.finished_turn_message(
            {"last_assistant_message": self.FINISHED}, self.rows(self.PREVIOUS)
        )
        self.assertEqual(self.FINISHED, message)

    def test_the_finished_turns_passage_is_the_one_announced(self) -> None:
        spoken, source = announce.announcement(
            announce.finished_turn_message(
                {"last_assistant_message": self.FINISHED}, self.rows(self.PREVIOUS)
            )
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("This is turn two, BRAVO.", spoken)
        self.assertNotIn("ALPHA", spoken)

    def test_the_camel_case_alias_is_read(self) -> None:
        """Copilot's `agentStop` sends the same fields in camelCase."""
        message = announce.finished_turn_message(
            {"lastAssistantMessage": self.FINISHED}, self.rows(self.PREVIOUS)
        )
        self.assertEqual(self.FINISHED, message)

    def test_an_absent_field_falls_back_to_the_transcript(self) -> None:
        """An agent or a version that sends no message still gets announced."""
        message = announce.finished_turn_message({}, self.rows("First.", self.PREVIOUS))
        self.assertEqual(self.PREVIOUS, message)

    def test_an_empty_field_falls_back_to_the_transcript(self) -> None:
        for payload in ({"last_assistant_message": ""}, {"last_assistant_message": None}):
            with self.subTest(payload=payload):
                self.assertEqual(self.PREVIOUS, announce.finished_turn_message(payload, self.rows(self.PREVIOUS)))

    def test_nothing_anywhere_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual("", announce.finished_turn_message({}, []))

    def test_the_first_turn_of_a_session_is_announced(self) -> None:
        """Before the fix this said nothing: the transcript holds no agent
        message at all yet, so there was nothing to find."""
        spoken, source = announce.announcement(
            announce.finished_turn_message({"last_assistant_message": self.FINISHED}, [])
        )
        self.assertEqual(announce.SOURCE_VOICE_NOTE, source)
        self.assertEqual("This is turn two, BRAVO.", spoken)


class AnnouncementLogTests(unittest.TestCase):
    """Which path a turn took has to be visible, or drift is invisible."""

    def run_hook(self, message: str, *, in_payload: bool = True, transcript_path: bool = True) -> str:
        """Run the shipped script end to end.

        `in_payload` puts the message where the agent actually hands it over;
        clearing it leaves only the transcript, which is the fallback path.
        """
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        transcript = directory / "transcript.jsonl"
        transcript.write_text(
            jsonl([{"type": "assistant", "message": {"content": [{"type": "text", "text": message}]}}]),
            encoding="utf-8",
        )
        log = directory / "announce.log"
        subprocess.run(
            [sys.executable, str(REPO / "hooks" / "murmly-announce.py")],
            input=json.dumps(
                {
                    **({"transcript_path": str(transcript)} if transcript_path else {}),
                    **({"last_assistant_message": message} if in_payload else {}),
                    "cwd": str(directory),
                }
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            env={
                **os.environ,
                "MURMLY_ANNOUNCE_LOG": str(log),
                "MURMLY_ANNOUNCE_FOREGROUND": "1",
                "MURMLY_ANNOUNCE_CHIME": "0",
                # A socket that is not there, so it never reaches a daemon and
                # never takes a session from one that is running.
                "MURMLY_SOCKET": str(directory / "absent.sock"),
            },
        )
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_the_log_names_the_agents_own_voice_note(self) -> None:
        entries = self.run_hook("<voice-note>The work is finished and it works.</voice-note>")
        self.assertIn(announce.SOURCE_VOICE_NOTE, entries)
        self.assertIn("the agent's own", entries)

    def test_the_log_names_an_extract(self) -> None:
        entries = self.run_hook("Done. The daemon now exits cleanly at logout.")
        self.assertIn(announce.SOURCE_SUMMARY, entries)
        self.assertIn("an extract", entries)

    def test_the_log_says_suppressed_rather_than_nothing_to_say(self) -> None:
        entries = self.run_hook("Everything passed.\n\n<voice-note></voice-note>")
        self.assertIn(announce.SOURCE_SUPPRESSED, entries)
        self.assertNotIn("nothing worth saying", entries)

    def test_a_payload_message_is_announced_with_no_transcript_at_all(self) -> None:
        """The transcript is only needed to name the agent now."""
        entries = self.run_hook(
            "<voice-note>No transcript anywhere.</voice-note>", transcript_path=False
        )
        self.assertIn(announce.SOURCE_VOICE_NOTE, entries)
        self.assertIn("No transcript anywhere.", entries)

    def test_the_transcript_still_serves_an_agent_that_sends_no_message(self) -> None:
        entries = self.run_hook(
            "Done. The daemon now exits cleanly at logout.", in_payload=False
        )
        self.assertIn(announce.SOURCE_SUMMARY, entries)
        self.assertIn("exits cleanly at logout", entries)


class InstructionHookTests(unittest.TestCase):
    """The script that tells the agent the convention. It must stay a constant."""

    def run_script(self, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / "murmly-voice-note.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, **environment},
        )

    def test_it_prints_the_convention_and_exits_zero(self) -> None:
        finished = self.run_script(MURMLY_ANNOUNCE_INSTRUCT="1")
        self.assertEqual(0, finished.returncode)
        self.assertIn("<voice-note>", finished.stdout)
        self.assertIn("empty", finished.stdout)

    def test_it_can_be_told_to_say_nothing(self) -> None:
        finished = self.run_script(MURMLY_ANNOUNCE_INSTRUCT="0")
        self.assertEqual(0, finished.returncode)
        self.assertEqual("", finished.stdout)

    def test_it_opens_nothing_and_runs_nothing(self) -> None:
        """Every session start waits on this, which is only acceptable while it
        does no work. A socket or a subprocess here would put a connection
        attempt or a fork in front of every session Claude Code opens."""
        source = (REPO / "hooks" / "murmly-voice-note.py").read_text(encoding="utf-8")
        imported = {
            node.module or ""
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertEqual({"os", "sys", "__future__"}, imported)


class SessionSentenceTests(unittest.TestCase):
    def repository(self, branch: str) -> Path:
        """A repository of its own, rather than the one the tests are running in.

        A CI checkout is a detached HEAD, so asking this repository for its
        branch answers differently there than on a workstation. What the
        sentence says should not depend on how the tests were checked out.
        """
        directory = Path(tempfile.mkdtemp(suffix="-project"))
        self.addCleanup(shutil.rmtree, directory, True)
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "init", "-b", branch, str(directory)], check=True, capture_output=True)
        subprocess.run(
            [*git, "-C", str(directory), "commit", "--allow-empty", "-m", "first"],
            check=True,
            capture_output=True,
        )
        return directory

    def test_the_branch_is_named_when_there_is_one(self) -> None:
        directory = self.repository("release/2.0")
        sentence = announce.session_sentence("Claude Code", str(directory))
        self.assertEqual(f"Claude Code in {directory.name}, on branch release/2.0.", sentence)

    def test_a_detached_head_names_only_the_project(self) -> None:
        """Which is what a CI checkout is. "On branch HEAD" says nothing."""
        directory = self.repository("main")
        head = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(directory), "checkout", "--detach", head],
            check=True,
            capture_output=True,
        )

        self.assertEqual(
            f"Claude Code in {directory.name}.",
            announce.session_sentence("Claude Code", str(directory)),
        )

    def test_a_directory_that_is_not_a_repository_names_only_the_project(self) -> None:
        with tempfile.TemporaryDirectory(suffix="-scratch") as directory:
            sentence = announce.session_sentence("Copilot", directory)
        self.assertEqual(f"Copilot in {Path(directory).name}.", sentence)

    def test_no_directory_still_produces_a_sentence(self) -> None:
        self.assertEqual("Copilot has finished.", announce.session_sentence("Copilot", ""))


class ChimeTests(unittest.TestCase):
    def test_the_notes_are_a_playable_wav(self) -> None:
        data = announce.chime_wav()
        self.assertEqual(b"RIFF", data[:4])
        with wave.open(io.BytesIO(data)) as handle:
            self.assertEqual(1, handle.getnchannels())
            self.assertEqual(2, handle.getsampwidth())
            self.assertEqual(announce.CHIME_RATE_HZ, handle.getframerate())
            expected = int(announce.CHIME_RATE_HZ * announce.CHIME_NOTE_SECONDS) * len(announce.CHIME_NOTES_HZ)
            self.assertEqual(expected, handle.getnframes())

    def test_the_notes_start_and_end_at_silence(self) -> None:
        """A sine cut off at full amplitude clicks, and three clicks is a fault."""
        with wave.open(io.BytesIO(announce.chime_wav())) as handle:
            frames = handle.readframes(handle.getnframes())
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        self.assertEqual(0, samples[0])
        self.assertLess(abs(samples[-1]), 1_000)
        self.assertLess(max(abs(sample) for sample in samples), 32_768)


class ChimePlatformTests(unittest.TestCase):
    """Which player is tried is a function of the platform, taken as a
    parameter -- pinned per test rather than read from `sys.platform` -- so
    each answer is checked on whatever machine happens to run the suite."""

    def test_linux_tries_pipewire_then_pulse_then_alsa(self) -> None:
        self.assertEqual(
            (("pw-play",), ("paplay",), ("aplay", "-q")),
            announce.chime_player_candidates("linux"),
        )

    def test_macos_uses_afplay_alone(self) -> None:
        self.assertEqual((("afplay",),), announce.chime_player_candidates("darwin"))

    def test_an_unrecognised_platform_falls_back_to_the_linux_candidates(self) -> None:
        self.assertEqual(announce.LINUX_CHIME_PLAYERS, announce.chime_player_candidates("freebsd13"))


class WindowsChimeTests(unittest.TestCase):
    """`winsound` only exists on Windows. A fake module in `sys.modules`
    stands in for it so the dispatch and its failure handling are checked
    without a Windows machine -- the one thing that cannot be faked this way
    is whether the real `winsound.PlaySound` behaves as documented, which is
    what the Windows CI job's ordinary run of this suite covers instead."""

    def fake_winsound(self, play_sound) -> types.ModuleType:
        module = types.ModuleType("winsound")
        module.SND_FILENAME = 0x00020000
        module.SND_NODEFAULT = 0x00000002
        module.PlaySound = play_sound
        return module

    def test_playsound_is_called_with_no_default_fallback(self) -> None:
        """`SND_NODEFAULT` matters: without it a file `winsound` cannot play
        falls back to the system's default sound and reports success anyway."""
        calls = []
        module = self.fake_winsound(lambda path, flags: calls.append((path, flags)))
        with injected_module("winsound", module):
            result = announce._play_chime_windows("/tmp/chime.wav", "chime")
        self.assertEqual("chime", result)
        self.assertEqual([("/tmp/chime.wav", module.SND_FILENAME | module.SND_NODEFAULT)], calls)

    def test_a_runtime_error_from_playsound_is_reported_not_raised(self) -> None:
        """`winsound.PlaySound` raises `RuntimeError` on failure, not `OSError`."""

        def play_sound(path, flags):
            raise RuntimeError("no waveform-audio device enabled")

        module = self.fake_winsound(play_sound)
        with injected_module("winsound", module):
            result = announce._play_chime_windows("/tmp/chime.wav", "chime")
        self.assertIn("failed", result)
        self.assertIn("no waveform-audio device enabled", result)

    def test_a_missing_winsound_is_a_graceful_failure(self) -> None:
        """The path Linux and macOS actually take, since neither ships the
        module: this must fail gracefully rather than raise, because it feeds
        straight into a hook that must exit 0 regardless (17.3)."""
        with removed_module("winsound"):
            result = announce._play_chime_windows("/tmp/chime.wav", "chime")
        self.assertIn("failed", result)


class PlayChimeDispatchTests(unittest.TestCase):
    """`play_chime` picks a mechanism by `sys.platform`, patched here rather
    than pinned by parameter since `play_chime` itself takes none -- it is
    the one place platform selection has to read the real attribute, for
    `_speak_in_background` below to be able to do the same."""

    def test_windows_goes_through_winsound_not_a_subprocess_player(self) -> None:
        calls = []
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(announce, "_play_chime_windows", lambda path, label: calls.append(label) or "chime"),
        ):
            result = announce.play_chime()
        self.assertEqual("chime", result)
        self.assertEqual(["chime"], calls)

    def test_linux_goes_through_a_subprocess_player(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(shutil, "which", lambda name: f"/usr/bin/{name}" if name == "pw-play" else None),
            patch.object(announce, "_run_player", lambda player, path, label: f"used {player[0]}"),
        ):
            result = announce.play_chime()
        self.assertEqual("used pw-play", result)

    def test_macos_goes_through_a_subprocess_player_too(self) -> None:
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(shutil, "which", lambda name: f"/usr/bin/{name}" if name == "afplay" else None),
            patch.object(announce, "_run_player", lambda player, path, label: f"used {player[0]}"),
        ):
            result = announce.play_chime()
        self.assertEqual("used afplay", result)

    def test_no_player_on_linux_is_reported_not_raised(self) -> None:
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(shutil, "which", lambda name: None),
        ):
            result = announce.play_chime()
        self.assertEqual("no audio player for the chime", result)


class DetachedAnnouncementTests(unittest.TestCase):
    """17.1: the hook must not hold up the turn waiting on `announce()`, which
    can hold a connection open for up to `HEARD_ALL_TIMEOUT_SECONDS`. These
    drive the real, shipped script end to end -- without
    `MURMLY_ANNOUNCE_FOREGROUND` -- which is the only way to see the actual
    detach mechanism (`subprocess.Popen` with real creation flags) rather
    than the in-process shortcut every other test in this file takes."""

    def run_detached(self, message: str, *, socket_path: str) -> tuple[subprocess.CompletedProcess, Path]:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        transcript = directory / "transcript.jsonl"
        transcript.write_text(
            jsonl([{"type": "assistant", "message": {"content": [{"type": "text", "text": message}]}}]),
            encoding="utf-8",
        )
        log = directory / "announce.log"
        finished = subprocess.run(
            [sys.executable, str(REPO / "hooks" / "murmly-announce.py")],
            input=json.dumps(
                {
                    "transcript_path": str(transcript),
                    "last_assistant_message": message,
                    "cwd": str(directory),
                }
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            env={
                **os.environ,
                "MURMLY_ANNOUNCE_LOG": str(log),
                "MURMLY_ANNOUNCE_CHIME": "0",
                "MURMLY_SOCKET": socket_path,
            },
        )
        return finished, log

    def read(self, log: Path) -> str:
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def poll_for(self, log: Path, needle: str, *, seconds: float = 10.0) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            content = self.read(log)
            if needle in content:
                return content
            time.sleep(0.1)
        self.fail(f"{needle!r} never appeared in the log within {seconds}s; got: {self.read(log)!r}")

    def test_exits_zero_and_stays_quiet_with_no_daemon_listening(self) -> None:
        """Runs identically on every CI platform: nothing here depends on
        AF_UNIX actually working, only on the hook exiting 0 either way
        (17.3) and the detached child eventually recording why it stayed
        quiet (17.1) -- proof the `DETACHED_PROCESS`/`start_new_session`
        child was really started rather than merely not-crashing."""
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        # The non-ASCII character pins the handoff's encoding: the parent
        # writes the JSON request as UTF-8 and the child must read it back
        # the same way, rather than in the platform's locale encoding, which
        # is not UTF-8 on Windows and would mangle this silently there.
        finished, log = self.run_detached(
            "<voice-note>Nobody is listening, café.</voice-note>",
            socket_path=str(directory / "absent.sock"),
        )
        self.assertEqual(0, finished.returncode)
        self.poll_for(log, "café")
        self.poll_for(log, "no daemon")

    def test_the_parent_does_not_wait_on_the_child_to_be_heard(self) -> None:
        """Pins the ordering, not the speed: a fake daemon accepts the
        connection and then withholds its answer, so `announce()` inside the
        child cannot finish until `Session.declare()` times out on its own
        (`CONNECT_TIMEOUT_SECONDS`). If the parent process were waiting on
        that outcome rather than merely spawning the child, the log would
        already show it by the time `subprocess.run` returns; instead it
        shows up only afterward, on its own schedule.
        """
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no AF_UNIX on this platform")
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        sock_path = str(directory / "slow.sock")
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
        except OSError:
            self.skipTest("could not create a UNIX socket on this platform")
        self.addCleanup(server.close)
        server.listen(1)

        accepted = threading.Event()
        release = threading.Event()

        def slow_daemon() -> None:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            accepted.set()
            release.wait(10.0)
            connection.close()

        thread = threading.Thread(target=slow_daemon, daemon=True)
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(2.0)))

        finished, log = self.run_detached(
            "<voice-note>A slow daemon is listening.</voice-note>", socket_path=sock_path
        )

        self.assertTrue(accepted.wait(5.0), "the detached child never reached the fake daemon")
        self.assertEqual(0, finished.returncode)
        self.assertNotIn("->", self.read(log), "the parent waited for the child's outcome before returning")

        self.poll_for(log, "->")
        release.set()


class RunDetachedUnitTests(unittest.TestCase):
    """The detached child's own entry point, exercised directly rather than
    through a real subprocess so its edge cases do not need a real socket."""

    class FakeStdin:
        """A stand-in for `sys.stdin`: only `.buffer` is read, and `sys.stdin`
        itself has to be replaced wholesale since `TextIOWrapper.buffer` is a
        read-only attribute that `patch.object` cannot set in place."""

        def __init__(self, data: bytes) -> None:
            self.buffer = io.BytesIO(data)

    def run_with_stdin(self, request: dict) -> tuple[int, list[tuple[str, str, str]]]:
        calls: list[tuple[str, str, str]] = []
        with (
            patch.object(sys, "stdin", self.FakeStdin(json.dumps(request).encode("utf-8"))),
            patch.object(
                announce,
                "announce",
                lambda context, spoken, source: calls.append((context, spoken, source)) or "spoken",
            ),
        ):
            result = announce._run_detached()
        return result, calls

    def test_nothing_to_say_opens_no_session_at_all(self) -> None:
        """A truncated or empty handoff must not open a speech session to
        speak nothing -- that would take a session from whoever else might
        want one for no reason."""
        result, calls = self.run_with_stdin({"context": "Claude Code in murmly.", "spoken": "", "source": "summary"})
        self.assertEqual(0, result)
        self.assertEqual([], calls)

    def test_a_broken_handoff_is_quiet_rather_than_fatal(self) -> None:
        with patch.object(sys, "stdin", self.FakeStdin(b"not json at all")):
            result = announce._run_detached()
        self.assertEqual(0, result)

    def test_a_well_formed_handoff_announces_it(self) -> None:
        result, calls = self.run_with_stdin(
            {"context": "Claude Code in murmly.", "spoken": "It works.", "source": announce.SOURCE_SUMMARY}
        )
        self.assertEqual(0, result)
        self.assertEqual([("Claude Code in murmly.", "It works.", announce.SOURCE_SUMMARY)], calls)


class SpeakInBackgroundUnitTests(unittest.TestCase):
    """The Popen call itself: which creation flags go with which platform,
    and that the request is handed over and the process is never waited on."""

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdin_closed = False

            def close() -> None:
                # Tracked rather than performed for real: a truly closed
                # `BytesIO` cannot be read back by `getvalue()` afterward,
                # which is exactly what the tests below need to do to check
                # what was written.
                self.stdin_closed = True

            self.stdin.close = close

    def test_posix_uses_a_new_session_rather_than_creation_flags(self) -> None:
        captured = {}

        def fake_popen(argv, **keywords):
            captured["argv"] = argv
            captured["keywords"] = keywords
            return self.FakeProcess()

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(subprocess, "Popen", fake_popen),
        ):
            announce._speak_in_background("context", "spoken", announce.SOURCE_SUMMARY)

        self.assertTrue(captured["keywords"].get("start_new_session"))
        self.assertNotIn("creationflags", captured["keywords"])
        self.assertEqual(announce.SPEAK_ARGV, captured["argv"][-1])

    def test_windows_uses_detached_creation_flags_rather_than_a_new_session(self) -> None:
        captured = {}

        def fake_popen(argv, **keywords):
            captured["keywords"] = keywords
            return self.FakeProcess()

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(subprocess, "Popen", fake_popen),
            patch.object(
                subprocess, "DETACHED_PROCESS", 0x00000008, create=True
            ),
            patch.object(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True
            ),
        ):
            announce._speak_in_background("context", "spoken", announce.SOURCE_SUMMARY)

        self.assertNotIn("start_new_session", captured["keywords"])
        self.assertEqual(0x00000008 | 0x00000200, captured["keywords"]["creationflags"])

    def test_the_request_is_written_as_utf8_and_the_pipe_is_closed(self) -> None:
        process = self.FakeProcess()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(subprocess, "Popen", lambda argv, **keywords: process),
        ):
            announce._speak_in_background("café", "spoken", announce.SOURCE_SUMMARY)

        request = json.loads(process.stdin.getvalue().decode("utf-8"))
        self.assertEqual({"context": "café", "spoken": "spoken", "source": announce.SOURCE_SUMMARY}, request)
        self.assertTrue(process.stdin_closed, "the child's stdin was never closed, so it would block on EOF")

    def test_a_popen_failure_is_logged_rather_than_raised(self) -> None:
        """17.3's property one level up: even the handoff itself must not
        turn into an uncaught exception that would fail the turn."""
        log = Path(tempfile.mkdtemp()) / "announce.log"
        self.addCleanup(shutil.rmtree, log.parent, True)

        def fake_popen(argv, **keywords):
            raise OSError("no such interpreter")

        with (
            patch.object(announce, "LOG_PATH", str(log)),
            patch.object(subprocess, "Popen", fake_popen),
        ):
            announce._speak_in_background("context", "spoken", announce.SOURCE_SUMMARY)

        self.assertIn("no such interpreter", log.read_text(encoding="utf-8"))


class SilenceWhenRefusedTests(unittest.TestCase):
    """A refusal makes no sound at all, attention notes included.

    The notes exist to tell someone who is not looking at the terminal that
    words are arriving. Played in front of a refusal they promise an
    announcement that never comes, and at one per turn they teach the person to
    ignore the signal -- which costs the announcements that do work.

    The session is therefore opened first and the notes played only after it is
    accepted, which is also why the daemon has to answer the declaration from
    what is true now rather than from what was true when it started.
    """

    def _announce(self, refusal: str) -> tuple[str, list[str]]:
        played: list[str] = []

        def chime() -> str:
            played.append("chime")
            return "chime"

        # `patch.object` rather than assignment: these are class attributes on a
        # module every other test in this file shares, and restoring them by
        # hand is exactly the kind of thing that gets one name short. It did --
        # `speak`, `end` and `wait_until_heard` stayed stubbed for the rest of
        # the process, so any later test on the Session path would have run
        # against the lambdas instead of the code.
        with (
            patch.object(announce.Session, "declare", lambda self: refusal),
            patch.object(announce, "play_chime", chime),
            patch.object(announce.Session, "speak", lambda self, name, text: None),
            patch.object(announce.Session, "end", lambda self: None),
            patch.object(announce.Session, "wait_until_heard", lambda self: "spoken"),
        ):
            outcome = announce.announce("context", "spoken", announce.SOURCE_VOICE_NOTE)
        return outcome, played

    def test_speech_output_unavailable_produces_no_notes(self) -> None:
        outcome, played = self._announce("refused: speech_unavailable")

        self.assertEqual([], played, "the notes played in front of a refusal")
        self.assertIn("speech_unavailable", outcome)

    def test_a_session_already_held_produces_no_notes(self) -> None:
        outcome, played = self._announce("refused: speech_session_in_use")

        self.assertEqual([], played)
        self.assertIn("speech_session_in_use", outcome)

    def test_quiet_hours_produce_no_notes(self) -> None:
        """The window exists because someone is asleep.

        Notes announcing an announcement that never comes would wake them for
        nothing, which is the one outcome the window is there to prevent. No
        change to this file was needed for it: the session is declared before
        the notes sound, so every refusal the daemon can give is already silent.
        """
        outcome, played = self._announce("refused: speech_quiet_hours")

        self.assertEqual([], played, "the notes played in front of a quiet window")

    def test_the_diagnostic_line_names_quiet_hours(self) -> None:
        """`refused: <code>` is the whole of what a person reading the log gets.

        Without the code it says only that nothing was spoken, which is what a
        disabled synthesizer, a held session and a working quiet window all look
        like from the outside.
        """
        outcome, _played = self._announce("refused: speech_quiet_hours")

        self.assertIn("speech_quiet_hours", outcome)
        self.assertNotIn("speech_unavailable", outcome)
        self.assertNotIn("speech_disabled", outcome)

    def test_an_accepted_session_does_play_them(self) -> None:
        """Pins the tests above, which would pass with the notes deleted."""
        outcome, played = self._announce("")

        self.assertEqual(["chime"], played)
        self.assertEqual("spoken", outcome)


class InstallHooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.settings = self.directory / "settings.json"
        self.copilot = self.directory / "copilot-hooks"
        self.copilot.mkdir()
        self.script = self.directory / "murmly-announce.py"
        self.script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.instruction = self.directory / "murmly-voice-note.py"
        self.instruction.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def run_installer(self, *extra: str) -> str:
        finished = subprocess.run(
            [
                sys.executable,
                str(REPO / "hooks" / "install_hooks.py"),
                "--script",
                str(self.script),
                "--claude-settings",
                str(self.settings),
                "--copilot-hooks-dir",
                str(self.copilot),
                *extra,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return finished.stdout

    def entries(self, event: str = "Stop") -> list[dict]:
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        return [hook for group in document.get("hooks", {}).get(event, []) for hook in group.get("hooks", [])]

    def stop_entries(self) -> list[dict]:
        return self.entries("Stop")

    def with_instruction(self, *extra: str) -> str:
        return self.run_installer("--instruction-script", str(self.instruction), *extra)

    def test_registering_twice_leaves_one_registration(self) -> None:
        self.run_installer()
        self.run_installer()
        murmly = [entry for entry in self.stop_entries() if "murmly-announce" in entry["command"]]
        self.assertEqual(1, len(murmly))

    def test_a_hook_installed_by_hand_at_another_path_is_replaced(self) -> None:
        self.settings.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "Stop": [
                            {"hooks": [{"type": "command", "command": "python3 ~/old/murmly-announce.py"}]}
                        ],
                        "SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}],
                    },
                }
            ),
            encoding="utf-8",
        )
        output = self.run_installer("--agents", "claude")

        self.assertIn("replaced 1", output)
        commands = [entry["command"] for entry in self.stop_entries()]
        self.assertEqual([f"python3 '{self.script}'"], commands)

        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual("opus", document["model"], "an unrelated setting was lost")
        self.assertIn("SessionStart", document["hooks"], "an unrelated hook was lost")

    def test_another_stop_hook_survives_registration_and_removal(self) -> None:
        self.settings.write_text(
            json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]}}),
            encoding="utf-8",
        )
        self.run_installer("--agents", "claude")
        self.run_installer("--agents", "claude", "--remove")

        commands = [entry["command"] for entry in self.stop_entries()]
        self.assertEqual(["notify-send done"], commands)

    def test_removal_takes_the_group_with_the_last_hook_in_it(self) -> None:
        self.run_installer("--agents", "claude")
        self.run_installer("--agents", "claude", "--remove")

        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("Stop", document.get("hooks", {}))

    def test_both_events_are_registered_in_one_write(self) -> None:
        self.with_instruction("--agents", "claude")

        stop = [entry for entry in self.entries("Stop") if "murmly-announce" in entry["command"]]
        session_start = [
            entry for entry in self.entries("SessionStart") if "murmly-voice-note" in entry["command"]
        ]
        self.assertEqual(1, len(stop))
        self.assertEqual(1, len(session_start))

    def test_the_instruction_is_registered_synchronously(self) -> None:
        """An async hook runs in the background, so its stdout never reaches the
        session's context. Registered async it would instruct nobody and look
        like it had worked."""
        self.with_instruction("--agents", "claude")

        stop = self.entries("Stop")[0]
        session_start = self.entries("SessionStart")[0]

        self.assertTrue(stop["async"], "the announcement is waited on by nobody")
        self.assertNotIn("async", session_start)
        self.assertEqual(install_hooks.INSTRUCTION_TIMEOUT_SECONDS, session_start["timeout"])
        self.assertLess(session_start["timeout"], stop["timeout"])

    def test_the_instruction_runs_for_every_source(self) -> None:
        """No matcher, so it survives a compaction. A long session that lost the
        convention halfway through would degrade to extracts without saying so."""
        self.with_instruction("--agents", "claude")
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        for group in document["hooks"]["SessionStart"]:
            self.assertNotIn("matcher", group)

    def test_registering_twice_leaves_one_of_each(self) -> None:
        self.with_instruction("--agents", "claude")
        self.with_instruction("--agents", "claude")

        self.assertEqual(1, len(self.entries("Stop")))
        self.assertEqual(1, len(self.entries("SessionStart")))

    def test_removal_takes_both_out(self) -> None:
        self.with_instruction("--agents", "claude")
        output = self.run_installer("--agents", "claude", "--remove")

        self.assertIn("removed 2", output)
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("Stop", document.get("hooks", {}))
        self.assertNotIn("SessionStart", document.get("hooks", {}))

    def test_an_installation_that_predates_the_instruction_removes_cleanly(self) -> None:
        """`hooks off` after an upgrade meets settings with no SessionStart key
        at all. That is nothing to remove rather than a failure."""
        self.run_installer("--agents", "claude")
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", document["hooks"])

        output = self.run_installer("--agents", "claude", "--remove")
        self.assertIn("removed 1", output)
        self.assertEqual({}, json.loads(self.settings.read_text(encoding="utf-8")))

    def test_someone_elses_session_start_hook_survives_both_directions(self) -> None:
        self.settings.write_text(
            json.dumps(
                {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "direnv export"}]}]}}
            ),
            encoding="utf-8",
        )
        self.with_instruction("--agents", "claude")
        self.assertIn("direnv export", [entry["command"] for entry in self.entries("SessionStart")])

        self.run_installer("--agents", "claude", "--remove")
        self.assertEqual(["direnv export"], [entry["command"] for entry in self.entries("SessionStart")])

    def test_registering_without_one_takes_out_a_stale_instruction(self) -> None:
        """Otherwise a downgrade leaves an entry naming a script that is gone."""
        self.with_instruction("--agents", "claude")
        self.run_installer("--agents", "claude")

        document = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertNotIn("SessionStart", document["hooks"])

    def test_copilot_takes_no_instruction_registration(self) -> None:
        """Copilot CLI documents no hook whose output reaches the model."""
        self.with_instruction("--agents", "copilot")
        document = json.loads((self.copilot / install_hooks.COPILOT_HOOK_FILE).read_text(encoding="utf-8"))
        self.assertEqual(["Stop"], list(document["hooks"]))

    def test_removing_what_was_never_installed_is_not_a_failure(self) -> None:
        output = self.run_installer("--remove")
        self.assertIn("nothing registered", output)

    def test_copilot_registers_one_event_in_a_file_of_its_own(self) -> None:
        self.run_installer("--agents", "copilot")
        target = self.copilot / install_hooks.COPILOT_HOOK_FILE
        document = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(1, document["version"])
        # `Stop` and `agentStop` are aliases that both fire, so naming both
        # would announce every turn twice.
        self.assertEqual(["Stop"], list(document["hooks"]))
        self.assertEqual(f"python3 '{self.script}'", document["hooks"]["Stop"][0]["bash"])

        self.run_installer("--agents", "copilot", "--remove")
        self.assertFalse(target.exists())

    def test_the_previous_settings_are_kept_beside_the_new_ones(self) -> None:
        self.settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        self.run_installer("--agents", "claude")

        backup = self.settings.with_suffix(".json.murmly-backup")
        self.assertTrue(backup.is_file())
        self.assertEqual({"model": "opus"}, json.loads(backup.read_text(encoding="utf-8")))

    def test_socket_is_baked_into_the_claude_command_as_an_environment_prefix(self) -> None:
        """Task 2.6: one resolution, baked in at install time, rather than the
        hook guessing `XDG_RUNTIME_DIR` itself at run time."""
        self.run_installer("--agents", "claude", "--socket", "/run/user/1000/murmly.sock")

        commands = [entry["command"] for entry in self.stop_entries()]
        self.assertEqual([f"MURMLY_SOCKET='/run/user/1000/murmly.sock' python3 '{self.script}'"], commands)

    def test_socket_is_baked_into_the_copilot_command_too(self) -> None:
        self.run_installer("--agents", "copilot", "--socket", "/run/user/1000/murmly.sock")

        document = json.loads((self.copilot / install_hooks.COPILOT_HOOK_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            f"MURMLY_SOCKET='/run/user/1000/murmly.sock' python3 '{self.script}'",
            document["hooks"]["Stop"][0]["bash"],
        )

    def test_without_socket_the_command_is_bare_exactly_as_before(self) -> None:
        """A caller with no way to resolve it (no venv yet) omits `--socket`
        entirely; the registered command must not change shape for it."""
        self.run_installer("--agents", "claude")

        commands = [entry["command"] for entry in self.stop_entries()]
        self.assertEqual([f"python3 '{self.script}'"], commands)

    def test_a_socket_prefixed_command_is_still_recognised_as_murmlys_own(self) -> None:
        """Idempotence must survive the prefix: a second install with a
        (possibly different) socket has to replace the first, not double it."""
        self.run_installer("--agents", "claude", "--socket", "/run/user/1000/murmly.sock")
        self.run_installer("--agents", "claude", "--socket", "/run/user/1000/murmly.sock")

        murmly = [entry for entry in self.stop_entries() if "murmly-announce" in entry["command"]]
        self.assertEqual(1, len(murmly))

    def test_a_reinstall_without_socket_does_not_leave_the_old_one_baked_in(self) -> None:
        """The downgrade path: a first install resolved a socket (a synced
        venv existed), a later one could not (the venv was removed and
        `./setup.sh hooks` was rerun on its own). The stale `MURMLY_SOCKET=`
        prefix must not survive -- `strip_murmly` matches on the
        `murmly-announce` substring, which the prefix does not hide."""
        self.run_installer("--agents", "claude", "--socket", "/run/user/1000/murmly.sock")
        self.run_installer("--agents", "claude")

        commands = [entry["command"] for entry in self.stop_entries()]
        self.assertEqual([f"python3 '{self.script}'"], commands)

    def test_settings_that_are_not_json_are_refused_rather_than_overwritten(self) -> None:
        self.settings.write_text("{ this is not json", encoding="utf-8")
        finished = subprocess.run(
            [
                sys.executable,
                str(REPO / "hooks" / "install_hooks.py"),
                "--script",
                str(self.script),
                "--agents",
                "claude",
                "--claude-settings",
                str(self.settings),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertNotEqual(0, finished.returncode)
        self.assertIn("Refusing to touch", finished.stderr)
        self.assertEqual("{ this is not json", self.settings.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
