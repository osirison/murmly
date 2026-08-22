"""The Stop hook that announces a finished turn, and the installer that wires it up.

Both scripts are loaded by path rather than imported: they ship as standalone
files that run under the system Python with no virtual environment, which is
what lets them keep working after `setup.sh uninstall --purge`.
"""

from __future__ import annotations

import importlib.util
import io
import json
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


class SessionSentenceTests(unittest.TestCase):
    def test_the_branch_is_named_when_there_is_one(self) -> None:
        sentence = announce.session_sentence("Claude Code", str(REPO))
        self.assertTrue(sentence.startswith("Claude Code in murmly, on branch "), sentence)
        self.assertTrue(sentence.endswith("."))

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

    def stop_entries(self) -> list[dict]:
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        return [hook for group in document.get("hooks", {}).get("Stop", []) for hook in group.get("hooks", [])]

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
