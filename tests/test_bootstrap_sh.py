"""`bootstrap.sh`, task 16.2's Linux half: install `uv` if it is missing, then
hand off to `setup.sh` unchanged. Sourced and exercised the same way
`test_setup_sh.py` exercises `setup.sh` itself -- no real `curl`, and
`setup.sh` itself is replaced by a recording stub, so nothing here reaches the
network or touches a real checkout.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_SH = REPO_ROOT / "bootstrap.sh"


def run_bash(script: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class BootstrapShTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.platform == "win32":
            # `bootstrap.sh` is explicitly the Linux half of task 16.2 (see
            # this module's own docstring and `bootstrap.sh`'s own header);
            # `bootstrap.ps1` is the Windows half, covered by
            # `test_bootstrap_ps1.py`. Not "no bash is available" -- a real
            # interpreter runs and exits 1 with empty stdout and stderr,
            # which is not what Git Bash does and does look like the
            # `bash.exe` stub Windows ships for launching WSL failing
            # silently with no distribution registered. Either way, a script
            # that never runs on Windows by design does not need a working
            # shell there to prove anything.
            self.skipTest("bootstrap.sh is Linux-only; bootstrap.ps1 is the Windows entry point")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        shutil.copy(BOOTSTRAP_SH, self.dir / "bootstrap.sh")

        # A fake `setup.sh` next to it: hand-off writes its own argv to a
        # file and exits 0, standing in for the real, much larger script
        # `test_setup_sh.py` already covers on its own.
        self.setup_record = self.dir / "setup-calls.txt"
        fake_setup = self.dir / "setup.sh"
        fake_setup.write_text(
            "#!/usr/bin/env bash\n"
            f'printf \'%s\\n\' "$*" >> "{self.setup_record}"\n'
        )
        fake_setup.chmod(0o755)

    def test_hands_off_to_setup_sh_when_uv_is_already_present(self) -> None:
        result = run_bash(
            "source ./bootstrap.sh\nhave_uv() { return 0; }\nmain install Meta+X",
            cwd=self.dir,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("install Meta+X", self.setup_record.read_text().strip())

    def test_never_attempts_to_install_uv_when_it_is_already_present(self) -> None:
        result = run_bash(
            "source ./bootstrap.sh\n"
            "have_uv() { return 0; }\n"
            'install_uv() { echo "SHOULD NOT RUN" >&2; }\n'
            "main install Meta+X",
            cwd=self.dir,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("SHOULD NOT RUN", result.stderr)

    def test_installs_uv_then_hands_off_when_it_is_missing(self) -> None:
        marker = self.dir / "uv-installed"
        result = run_bash(
            "source ./bootstrap.sh\n"
            f'have_uv() {{ [ -f "{marker}" ]; }}\n'
            f'install_uv() {{ touch "{marker}"; echo INSTALLED; }}\n'
            "main upgrade",
            cwd=self.dir,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("INSTALLED", result.stdout)
        self.assertEqual("upgrade", self.setup_record.read_text().strip())

    def test_refuses_and_never_hands_off_when_uv_still_is_not_on_path_afterward(self) -> None:
        result = run_bash(
            "source ./bootstrap.sh\n"
            "have_uv() { return 1; }\n"
            "install_uv() { :; }\n"  # a real install that did not land on PATH
            "main install Meta+X",
            cwd=self.dir,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("still not on PATH", result.stderr)
        self.assertFalse(self.setup_record.exists())

    def test_every_argument_is_forwarded_unchanged(self) -> None:
        result = run_bash(
            "source ./bootstrap.sh\nhave_uv() { return 0; }\nmain uninstall --purge --yes",
            cwd=self.dir,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("uninstall --purge --yes", self.setup_record.read_text().strip())


if __name__ == "__main__":
    unittest.main()
