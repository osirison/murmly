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
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import wave


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


class AnnouncementLogTests(unittest.TestCase):
    """Which path a turn took has to be visible, or drift is invisible."""

    def run_hook(self, message: str) -> str:
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
            input=json.dumps({"transcript_path": str(transcript), "cwd": str(directory)}),
            capture_output=True,
            text=True,
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


class InstructionHookTests(unittest.TestCase):
    """The script that tells the agent the convention. It must stay a constant."""

    def run_script(self, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "hooks" / "murmly-voice-note.py")],
            capture_output=True,
            text=True,
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
        )
        self.assertNotEqual(0, finished.returncode)
        self.assertIn("Refusing to touch", finished.stderr)
        self.assertEqual("{ this is not json", self.settings.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
