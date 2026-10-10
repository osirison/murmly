"""The speaker model's identity, written in three places, has to agree.

`setup.sh` and `bootstrap.ps1` each carry the model's URL, filename and SHA-256
because neither can import Python. `murmly.speakers` carries them for the daemon
and `doctor`. A pin that moved in one place and not the others would fetch a
file the checksum then refuses, or accept one the daemon will not recognise, and
nothing else in the suite would notice.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from murmly import speakers

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "setup.sh"
BOOTSTRAP_PS1 = REPO_ROOT / "bootstrap.ps1"


def _one(pattern: str, text: str, where: str) -> str:
    found = re.findall(pattern, text, re.MULTILINE)
    if len(found) != 1:
        raise AssertionError(f"{where}: expected exactly one match for {pattern!r}, found {found}")
    return found[0]


def constants_in_setup_sh() -> tuple[str, str, str]:
    text = SETUP_SH.read_text(encoding="utf-8")
    url, filename, sha256 = (
        _one(rf'^readonly {name}="([^"]*)"$', text, "setup.sh")
        for name in ("SPEAKER_MODEL_URL", "SPEAKER_MODEL_FILE", "SPEAKER_MODEL_SHA256")
    )
    return url, filename, sha256


def constants_in_bootstrap_ps1() -> tuple[str, str, str]:
    text = BOOTSTRAP_PS1.read_text(encoding="utf-8")
    url, filename, sha256 = (
        _one(rf'^\${name} = "([^"]*)"$', text, "bootstrap.ps1")
        for name in ("SpeakerModelUrl", "SpeakerModelFile", "SpeakerModelSha256")
    )
    return url, filename, sha256


class SpeakerModelPinTests(unittest.TestCase):
    def test_setup_sh_carries_the_same_pin_as_the_module(self) -> None:
        self.assertEqual(
            (
                speakers.SPEAKER_MODEL_URL,
                speakers.SPEAKER_MODEL_FILENAME,
                speakers.SPEAKER_MODEL_SHA256,
            ),
            constants_in_setup_sh(),
        )

    def test_bootstrap_ps1_carries_the_same_pin_as_the_module(self) -> None:
        self.assertEqual(
            (
                speakers.SPEAKER_MODEL_URL,
                speakers.SPEAKER_MODEL_FILENAME,
                speakers.SPEAKER_MODEL_SHA256,
            ),
            constants_in_bootstrap_ps1(),
        )

    def test_the_pin_is_an_exact_file_not_a_moving_branch(self) -> None:
        """A branch name in the URL would let the publisher change the bytes
        under a checksum that then refuses them for everyone."""
        self.assertRegex(speakers.SPEAKER_MODEL_SHA256, r"^[0-9a-f]{64}$")
        self.assertRegex(
            speakers.SPEAKER_MODEL_URL,
            r"^https://huggingface\.co/[^/]+/[^/]+/resolve/[0-9a-f]{40}/",
        )
        self.assertTrue(speakers.SPEAKER_MODEL_URL.endswith("/" + speakers.SPEAKER_MODEL_FILENAME))


if __name__ == "__main__":
    unittest.main()
