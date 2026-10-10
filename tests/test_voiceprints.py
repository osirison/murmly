"""The voiceprint file: atomic writes, privacy, model identity and change detection."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from murmly.speakers import EMBEDDING_DIMENSION, SPEAKER_MODEL_SHA256
from murmly.voiceprints import VOICEPRINTS_FILENAME, VoiceprintStore

OTHER_MODEL = "0" * 64


def embedding(seed: int) -> list[float]:
    """A deterministic, non-trivial 256-value vector."""
    rng = np.random.default_rng(seed)
    return [float(value) for value in rng.standard_normal(EMBEDDING_DIMENSION)]


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.directory = Path(self._temporary.name) / "data"
        self.store = VoiceprintStore(self.directory)
        self.path = self.directory / VOICEPRINTS_FILENAME
        # The warnings are the point of several tests; keep them off the test run.
        self.logger = patch("murmly.voiceprints.logger").start()
        self.addCleanup(patch.stopall)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.directory.iterdir() if p.name != VOICEPRINTS_FILENAME)


class WriteTests(StoreTestCase):
    def test_a_voice_round_trips_through_a_fresh_store(self) -> None:
        self.assertFalse(self.store.add_or_replace("Milo", embedding(1)))

        fresh = VoiceprintStore(self.directory)
        self.assertTrue(fresh.refresh())

        self.assertEqual(["Milo"], fresh.names)
        self.assertAlmostEqual(1.0, float(np.linalg.norm(fresh.voices[0].embedding)), 9)
        self.assertEqual(self.store.voices, fresh.voices)

    def test_adding_the_same_name_in_another_case_replaces_and_says_so(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        self.store.add_or_replace("Ana", embedding(2))

        replaced = self.store.add_or_replace("milo", embedding(3))

        self.assertTrue(replaced)
        self.assertEqual(["Ana", "milo"], sorted(self.store.names))
        self.assertEqual(2, len(self.store.voices))

    def test_an_interrupted_write_leaves_the_previous_file_and_no_temporary(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        before = self.path.read_bytes()

        for target in ("json.dump", "os.replace"):
            with self.subTest(failing=target):
                with patch(f"murmly.voiceprints.{target}", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        self.store.add_or_replace("Ana", embedding(2))

                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual([], self.leftovers())
                fresh = VoiceprintStore(self.directory)
                fresh.refresh()
                self.assertEqual(["Milo"], fresh.names)

    def test_a_failed_first_write_creates_no_voiceprint_file(self) -> None:
        with patch("murmly.voiceprints.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.add_or_replace("Milo", embedding(1))

        self.assertFalse(self.path.exists())
        self.assertEqual([], self.leftovers())

    def test_an_embedding_of_the_wrong_size_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_or_replace("Milo", [1.0, 2.0])

        self.assertFalse(self.path.exists())

    def test_the_serialisation_is_deterministic(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        first = self.path.read_bytes()
        self.store.add_or_replace("Milo", embedding(1))

        self.assertEqual(first, self.path.read_bytes())


@unittest.skipIf(sys.platform == "win32", "POSIX modes only; Windows inherits the directory ACL")
class PermissionTests(StoreTestCase):
    def test_the_file_is_private_to_the_account(self) -> None:
        old = os.umask(0o000)  # a permissive umask must not widen the file
        self.addCleanup(os.umask, old)

        self.store.add_or_replace("Milo", embedding(1))

        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))

    def test_every_directory_murmly_creates_is_private(self) -> None:
        old = os.umask(0o000)
        self.addCleanup(os.umask, old)
        nested = Path(self._temporary.name) / "one" / "two"
        store = VoiceprintStore(nested)

        store.add_or_replace("Milo", embedding(1))

        for directory in (nested, nested.parent):
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode), directory)

    def test_a_directory_that_already_exists_keeps_its_mode(self) -> None:
        existing = Path(self._temporary.name) / "shared"
        existing.mkdir(mode=0o755)
        os.chmod(existing, 0o755)

        VoiceprintStore(existing).add_or_replace("Milo", embedding(1))

        self.assertEqual(0o755, stat.S_IMODE(existing.stat().st_mode))

    def test_a_rewrite_keeps_the_file_private(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        self.store.add_or_replace("Ana", embedding(2))

        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))


class ModelIdentityTests(StoreTestCase):
    def test_voiceprints_from_another_model_are_ignored_and_flagged(self) -> None:
        VoiceprintStore(self.directory, model_sha256=OTHER_MODEL).add_or_replace(
            "Milo", embedding(1)
        )

        self.store.refresh()

        self.assertEqual([], self.store.voices)
        self.assertTrue(self.store.needs_reenrolment)
        self.assertIn("different speaker model", self.store.detail)
        self.logger.warning.assert_called_once()

    def test_voiceprints_from_the_installed_model_are_used_and_not_flagged(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))

        fresh = VoiceprintStore(self.directory)
        fresh.refresh()

        self.assertEqual(["Milo"], fresh.names)
        self.assertFalse(fresh.needs_reenrolment)
        self.assertIsNone(fresh.detail)

    def test_the_stored_identity_is_the_pinned_checksum(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))

        self.assertEqual(SPEAKER_MODEL_SHA256, json.loads(self.path.read_text())["model_sha256"])

    def test_enrolling_over_another_models_file_starts_a_file_for_this_one(self) -> None:
        old = VoiceprintStore(self.directory, model_sha256=OTHER_MODEL)
        old.add_or_replace("Milo", embedding(1))

        replaced = self.store.add_or_replace("Ana", embedding(2))

        self.assertFalse(replaced)
        fresh = VoiceprintStore(self.directory)
        fresh.refresh()
        self.assertEqual(["Ana"], fresh.names)
        self.assertFalse(fresh.needs_reenrolment)

    def test_a_removed_name_from_another_models_file_is_not_enrolled(self) -> None:
        VoiceprintStore(self.directory, model_sha256=OTHER_MODEL).add_or_replace(
            "Milo", embedding(1)
        )

        self.assertFalse(self.store.remove("Milo"))
        self.assertTrue(self.path.exists())
        self.assertTrue(self.store.remove_all())
        self.assertFalse(self.path.exists())


class ChangeDetectionTests(StoreTestCase):
    def test_a_missing_file_is_no_voices(self) -> None:
        self.store.refresh()

        self.assertEqual([], self.store.voices)
        self.assertIsNone(self.store.detail)
        self.assertFalse(self.store.needs_reenrolment)

    def test_an_unchanged_file_is_not_read_again(self) -> None:
        VoiceprintStore(self.directory).add_or_replace("Milo", embedding(1))
        reader = VoiceprintStore(self.directory)
        self.assertTrue(reader.refresh())

        with patch("murmly.voiceprints.json.load", side_effect=AssertionError("read again")):
            self.assertFalse(reader.refresh())

        self.assertEqual(["Milo"], reader.names)

    def test_a_changed_file_is_read_again(self) -> None:
        writer = VoiceprintStore(self.directory)
        writer.add_or_replace("Milo", embedding(1))
        reader = VoiceprintStore(self.directory)
        reader.refresh()

        writer.add_or_replace("Ana", embedding(2))

        self.assertTrue(reader.refresh())
        self.assertEqual(["Milo", "Ana"], reader.names)

    def test_a_file_changed_with_the_same_size_and_a_new_mtime_is_read_again(self) -> None:
        writer = VoiceprintStore(self.directory)
        writer.add_or_replace("Milo", embedding(1))
        reader = VoiceprintStore(self.directory)
        reader.refresh()

        writer.add_or_replace("Milo", embedding(2))  # same size, new values
        stats = self.path.stat()
        os.utime(self.path, ns=(stats.st_atime_ns, stats.st_mtime_ns + 1_000_000_000))

        self.assertTrue(reader.refresh())
        self.assertEqual(writer.voices, reader.voices)

    def test_a_deleted_file_drops_the_voices_from_memory(self) -> None:
        writer = VoiceprintStore(self.directory)
        writer.add_or_replace("Milo", embedding(1))
        reader = VoiceprintStore(self.directory)
        reader.refresh()
        self.assertEqual(["Milo"], reader.names)

        self.path.unlink()

        self.assertTrue(reader.refresh())
        self.assertEqual([], reader.voices)
        self.assertFalse(reader.refresh())


class DamagedFileTests(StoreTestCase):
    def write(self, content: str | bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        self.path.write_bytes(data)

    def test_unreadable_contents_read_as_no_voices_with_a_detail(self) -> None:
        good = json.dumps(
            {
                "version": 1,
                "model_sha256": SPEAKER_MODEL_SHA256,
                "dimension": EMBEDDING_DIMENSION,
                "voices": [{"name": "Milo", "embedding": [0.0] * 3}],
            }
        )
        cases = {
            "not json": "{not json",
            "binary": b"\xff\xfe\x00\x01",
            "empty": "",
            "a list": "[]",
            "wrong version": json.dumps({"version": 2, "model_sha256": "x", "voices": []}),
            "missing voices": json.dumps({"version": 1, "model_sha256": SPEAKER_MODEL_SHA256}),
            "short embedding": good,
            "name not a string": json.dumps(
                {
                    "version": 1,
                    "model_sha256": SPEAKER_MODEL_SHA256,
                    "dimension": EMBEDDING_DIMENSION,
                    "voices": [{"name": 5, "embedding": [0.0] * EMBEDDING_DIMENSION}],
                }
            ),
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.write(content)
                store = VoiceprintStore(self.directory)

                store.refresh()  # must not raise

                self.assertEqual([], store.voices)
                self.assertIn("cannot be used", store.detail)
                self.assertFalse(store.needs_reenrolment)

    def test_enrolling_over_a_damaged_file_replaces_it(self) -> None:
        self.write("{not json")

        self.store.add_or_replace("Milo", embedding(1))

        fresh = VoiceprintStore(self.directory)
        fresh.refresh()
        self.assertEqual(["Milo"], fresh.names)
        self.assertIsNone(fresh.detail)

    def test_the_detail_carries_no_voiceprint_values(self) -> None:
        self.write(json.dumps({"version": 1, "model_sha256": SPEAKER_MODEL_SHA256,
                               "dimension": EMBEDDING_DIMENSION,
                               "voices": [{"name": "Milo", "embedding": [0.123456] * 3}]}))
        self.store.refresh()

        self.assertNotIn("0.123456", self.store.detail)


class RemoveTests(StoreTestCase):
    def test_removing_one_leaves_the_other_byte_identical(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        self.store.add_or_replace("Ana", embedding(2))
        before = json.loads(self.path.read_text())["voices"]
        ana_before = json.dumps(before[1], ensure_ascii=False)

        self.assertTrue(self.store.remove("milo"))

        after = json.loads(self.path.read_text())["voices"]
        self.assertEqual(["Ana"], [voice["name"] for voice in after])
        self.assertEqual(ana_before, json.dumps(after[0], ensure_ascii=False))
        self.assertNotIn(b"Milo", self.path.read_bytes())

    def test_removing_the_last_voice_deletes_the_file(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))

        self.assertTrue(self.store.remove("Milo"))

        self.assertFalse(self.path.exists())
        self.assertEqual([], self.store.voices)
        self.assertEqual([], self.leftovers())

    def test_removing_all_deletes_the_file(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        self.store.add_or_replace("Ana", embedding(2))

        self.assertTrue(self.store.remove_all())

        self.assertFalse(self.path.exists())
        self.assertEqual([], self.store.voices)
        self.assertFalse(self.store.remove_all())

    def test_removing_a_name_that_is_not_enrolled_changes_nothing(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        before = self.path.read_bytes()

        self.assertFalse(self.store.remove("Zed"))

        self.assertEqual(before, self.path.read_bytes())

    def test_removing_from_a_store_with_no_file_reports_not_enrolled(self) -> None:
        self.assertFalse(self.store.remove("Milo"))
        self.assertFalse(self.directory.exists())


class ContentTests(StoreTestCase):
    def test_the_file_holds_only_the_documented_keys(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))
        self.store.add_or_replace("Ana", embedding(2))

        payload = json.loads(self.path.read_text())

        self.assertEqual({"version", "model_sha256", "dimension", "voices"}, set(payload))
        self.assertEqual(1, payload["version"])
        self.assertEqual(EMBEDDING_DIMENSION, payload["dimension"])
        for voice in payload["voices"]:
            self.assertEqual({"name", "embedding"}, set(voice))
            self.assertEqual(EMBEDDING_DIMENSION, len(voice["embedding"]))
            self.assertTrue(all(isinstance(value, float) for value in voice["embedding"]))

    def test_nothing_but_the_voiceprint_file_is_written(self) -> None:
        self.store.add_or_replace("Milo", embedding(1))

        self.assertEqual([VOICEPRINTS_FILENAME], sorted(p.name for p in self.directory.iterdir()))


if __name__ == "__main__":
    unittest.main()
