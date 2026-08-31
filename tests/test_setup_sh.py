"""`setup.sh`'s own functions, sourced and exercised on their own -- exactly
the use its trailing guard (`if [ "${BASH_SOURCE[0]}" = "$0" ]; then main
"$@"; fi`) exists for. Nothing here runs a real `uv`, `dnf`, or `systemctl`:
every command `setup.sh` would run is replaced by a bash function that
records what it was asked to do, the same seam every Python test in this
suite uses `run_command`/`which` for.

Covers what moved out of `setup.sh` in task 16.1 (the delegation to
`murmly sync` is a thin, correctly-shaped wrapper, not a silent no-op), what
task 16.3 adds (the macOS refusal that has to live here because a fresh
checkout's first `uv sync` runs before any `murmly` command exists to refuse
it), and what task 16.5 asks to keep working (every subcommand and flag).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_SH = REPO_ROOT / "setup.sh"


def run_bash(script: str, *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class SetupShTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        shutil.copy(SETUP_SH, self.repo / "setup.sh")

    def fake_command(self, name: str, record: Path, *, bin_dir: Path | None = None) -> Path:
        """A script on `PATH` under `name` that appends its own arguments to
        `record` and exits 0, standing in for a real `uv`/`uname`/etc."""
        directory = bin_dir if bin_dir is not None else self.repo / "fake-bin"
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{record}"\n')
        script.chmod(0o755)
        return directory


class RefuseUnsupportedOsTests(SetupShTestCase):
    """Task 16.3: the guard that covers macOS before `murmly sync` (or any
    `murmly` command) is even reachable to refuse it itself."""

    def test_refuses_on_darwin_naming_the_platform_and_what_is_supported(self) -> None:
        bin_dir = self.repo / "fake-bin"
        bin_dir.mkdir()
        uname = bin_dir / "uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)

        result = run_bash(
            f'PATH="{bin_dir}:$PATH"\nsource ./setup.sh\nrefuse_unsupported_os',
            cwd=self.repo,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("macOS", result.stderr)
        self.assertIn("Linux and Windows", result.stderr)

    def test_proceeds_on_linux(self) -> None:
        result = run_bash(
            'source ./setup.sh\nrefuse_unsupported_os\necho REACHED',
            cwd=self.repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("REACHED", result.stdout)

    def test_help_and_no_command_are_never_refused(self) -> None:
        """Printing usage writes nothing and starts nothing (task 16.3's own
        "no daemon started, no channel created, no file written" rule), so
        `main` must not run the guard for either -- even on an unsupported
        kernel, where every other command would be refused."""
        bin_dir = self.repo / "fake-bin"
        bin_dir.mkdir()
        uname = bin_dir / "uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)

        # Run as two separate processes rather than sequenced with `;`: `main`
        # returning nonzero for the empty-command case would otherwise trip
        # this script's own inherited `set -e` and abort before either
        # exit code was captured.
        help_result = run_bash(f'PATH="{bin_dir}:$PATH"\nsource ./setup.sh\nmain --help', cwd=self.repo)
        self.assertEqual(0, help_result.returncode, help_result.stderr)

        empty_result = run_bash(f'PATH="{bin_dir}:$PATH"\nsource ./setup.sh\nmain', cwd=self.repo)
        # No command at all is still a usage error (exit 2), never the
        # unsupported-platform refusal (`fail` always exits 1) -- distinguishing
        # the two is what proves the guard itself did not run here either.
        self.assertEqual(2, empty_result.returncode)
        self.assertNotIn("macOS", empty_result.stderr)

    def test_a_real_command_is_refused_before_the_command_function_runs(self) -> None:
        bin_dir = self.repo / "fake-bin"
        bin_dir.mkdir()
        uname = bin_dir / "uname"
        uname.write_text("#!/bin/sh\necho Darwin\n")
        uname.chmod(0o755)

        result = run_bash(
            f'PATH="{bin_dir}:$PATH"\n'
            "source ./setup.sh\n"
            'command_install() { echo "SHOULD NOT RUN"; }\n'
            "main install Meta+X",
            cwd=self.repo,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("SHOULD NOT RUN", result.stdout)
        self.assertIn("macOS", result.stderr)


class SyncEnvironmentDelegationTests(SetupShTestCase):
    """Task 16.1: `sync_environment` is now a thin wrapper around `murmly
    sync`, not a reimplementation -- and task 16.5's flags reach it unchanged."""

    def _run(self, script_body: str) -> subprocess.CompletedProcess[str]:
        return run_bash(f"source ./setup.sh\n{script_body}", cwd=self.repo)

    def test_delegates_with_no_flags_when_nothing_was_asked_for(self) -> None:
        # No `.venv`: `wants_speech_output`'s own `[ -d "$REPO/.venv" ]` half
        # short-circuits false without needing `installed_package` to answer
        # anything, so this is genuinely WANT_CUDA=auto/WANT_TTS=auto (both
        # left at their real defaults) producing no flags at all -- not
        # merely a test that forgot to ask about one of them.
        uv_record = self.repo / "uv-calls.txt"
        bin_dir = self.fake_command("uv", uv_record)
        record = self.repo / "murmly-calls.txt"
        result = self._run(
            f'PATH="{bin_dir}:$PATH"\n'
            f'murmly() {{ printf \'%s\\n\' "$*" >> "{record}"; }}\n'
            "install_models() { :; }\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(f"sync --project {self.repo}", record.read_text().strip())

    def test_forwards_cuda_tts_and_yes(self) -> None:
        (self.repo / ".venv").mkdir()
        record = self.repo / "murmly-calls.txt"
        result = self._run(
            f'murmly() {{ printf \'%s\\n\' "$*" >> "{record}"; }}\n'
            "install_models() { :; }\n"
            "WANT_CUDA=yes\n"
            "WANT_TTS=no\n"
            "ASSUME_YES=1\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        recorded = record.read_text()
        self.assertIn("--cuda", recorded)
        self.assertIn("--no-tts", recorded)
        self.assertIn("--yes", recorded)
        self.assertNotIn("--no-cuda", recorded)

    def test_no_cuda_and_explicit_tts(self) -> None:
        (self.repo / ".venv").mkdir()
        record = self.repo / "murmly-calls.txt"
        result = self._run(
            f'murmly() {{ printf \'%s\\n\' "$*" >> "{record}"; }}\n'
            "install_models() { :; }\n"
            "WANT_CUDA=no\n"
            "WANT_TTS=yes\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        recorded = record.read_text()
        self.assertIn("--no-cuda", recorded)
        self.assertIn("--tts", recorded)
        self.assertNotIn("--yes", recorded)

    def test_does_not_bootstrap_when_an_environment_already_exists(self) -> None:
        # Only `murmly sync` should run. If the bootstrap branch also ran, it
        # would try to exec a real `uv` -- absent from this test's `PATH` --
        # and `set -e` would fail the whole script rather than merely
        # recording a second call.
        (self.repo / ".venv").mkdir()
        record = self.repo / "murmly-calls.txt"
        result = self._run(
            f'murmly() {{ printf \'%s\\n\' "$*" >> "{record}"; }}\n'
            "install_models() { :; }\n"
            "WANT_TTS=no\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(record.read_text().splitlines()))

    def test_bootstraps_a_plain_sync_first_when_no_environment_exists_yet(self) -> None:
        # No `.venv` here: `murmly` (`uv run --no-sync`) has nothing to run
        # without syncing into, so a plain `uv sync --locked` has to make it
        # reachable first.
        uv_record = self.repo / "uv-calls.txt"
        bin_dir = self.fake_command("uv", uv_record)
        murmly_record = self.repo / "murmly-calls.txt"
        result = self._run(
            f'PATH="{bin_dir}:$PATH"\n'
            f'murmly() {{ printf \'%s\\n\' "$*" >> "{murmly_record}"; }}\n'
            "install_models() { :; }\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("sync --locked", uv_record.read_text())
        self.assertEqual(f"sync --project {self.repo}", murmly_record.read_text().strip())

    def test_downloads_models_only_when_speech_output_is_wanted(self) -> None:
        (self.repo / ".venv").mkdir()
        models_record = self.repo / "models-calls.txt"
        result = self._run(
            "murmly() { :; }\n"
            f'install_models() {{ printf \'ran\\n\' >> "{models_record}"; }}\n'
            "WANT_TTS=yes\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(models_record.exists())

    def test_skips_models_when_speech_output_was_declined(self) -> None:
        (self.repo / ".venv").mkdir()
        models_record = self.repo / "models-calls.txt"
        result = self._run(
            "murmly() { :; }\n"
            f'install_models() {{ printf \'ran\\n\' >> "{models_record}"; }}\n'
            "WANT_TTS=no\n"
            "sync_environment"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(models_record.exists())


class ConfirmDeclinesWithoutATerminalTests(SetupShTestCase):
    """Task 16.6, at the bash layer: `setup.sh`'s own `confirm()` (unchanged
    by this task, and exercised here for the first time)."""

    def test_declines_with_no_yes_and_nothing_attached_to_stdin(self) -> None:
        result = run_bash(
            'source ./setup.sh\nif confirm "Proceed?"; then echo YES; else echo NO; fi',
            cwd=self.repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("NO", result.stdout)
        self.assertIn("skipped, nothing is attached to answer", result.stderr)

    def test_assume_yes_accepts_without_reading_anything(self) -> None:
        result = run_bash(
            'source ./setup.sh\nASSUME_YES=1\nif confirm "Proceed?"; then echo YES; else echo NO; fi',
            cwd=self.repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("YES", result.stdout)


class SetupShSurfaceTests(SetupShTestCase):
    """Task 16.5: every subcommand and flag keeps working. Enumerated from
    `setup.sh`'s own option-parsing loop and command dispatch rather than
    from task 16.5's own list -- which turns out to omit `--hooks` and
    `--no-hooks`, both still present here and both exercised below."""

    def test_every_subcommand_and_flag_still_parses(self) -> None:
        result = run_bash(
            "source ./setup.sh\n"
            'command_install() { echo "install:$*"; }\n'
            'command_upgrade() { echo "upgrade:$*"; }\n'
            'command_hooks() { echo "hooks:$*"; }\n'
            'command_uninstall() { echo "uninstall:$*"; }\n'
            "refuse_unsupported_os() { :; }\n"
            "main install -y --cuda --no-cuda --tts --no-tts --hooks --no-hooks --purge Meta+X",
            cwd=self.repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("install:Meta+X", result.stdout)
        self.assertNotIn("Unknown option", result.stderr)

    def test_an_unknown_option_is_still_refused(self) -> None:
        """Confirms the parsing loop above is real -- not a stub that would
        accept anything -- by checking its one negative case still works."""
        result = run_bash(
            "source ./setup.sh\n"
            "refuse_unsupported_os() { :; }\n"
            "main install --not-a-real-flag",
            cwd=self.repo,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("Unknown option", result.stderr)

    def test_every_documented_subcommand_dispatches(self) -> None:
        for command in ("install", "upgrade", "hooks", "uninstall"):
            result = run_bash(
                "source ./setup.sh\n"
                "refuse_unsupported_os() { :; }\n"
                f'command_{command}() {{ echo "ran:{command}"; }}\n'
                f"main {command}",
                cwd=self.repo,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(f"ran:{command}", result.stdout)


if __name__ == "__main__":
    unittest.main()
