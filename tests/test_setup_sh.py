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

import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
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
        if sys.platform == "win32":
            # `setup.sh` is the Linux entry point (its own header: "pulling
            # the source, naming and offering this Linux system's own
            # packages" -- none of it is meant to run anywhere else);
            # `bootstrap.ps1` is Windows's equivalent, covered by
            # `test_bootstrap_ps1.py`. This is not "no bash is available" --
            # a real interpreter runs and exits 1 with empty stdout and
            # stderr, which does not look like Git Bash at all and does look
            # like the `bash.exe` stub Windows ships for WSL launching
            # silently when no distribution is registered. Either way, a
            # script that never runs on Windows by design does not need a
            # working shell there to prove anything.
            self.skipTest("setup.sh is Linux-only; bootstrap.ps1 is the Windows entry point")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved once here, not left as the raw tempdir path: on macOS
        # `/var` is a symlink to `/private/var`, and bash sets `$PWD` from
        # the physical (symlink-resolved) working directory when launched via
        # `subprocess.run(cwd=...)`. Without this, setup.sh's own `$PWD`-based
        # `$REPO` and this test's expectation name the same directory by two
        # different strings. A no-op on Linux and Windows, where the tempdir
        # is already its own canonical path.
        self.repo = Path(self._tmp.name).resolve()
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
        # Steered the same way `test_refuses_on_darwin...` steers the other
        # branch: a fake `uname` on `PATH`, not the ambient host's real one.
        # Without this, the assertion was really "the CI runner's OS is
        # Linux", which is true on two of the three runners for a reason that
        # has nothing to do with this guard, and false on the macOS runner --
        # where the guard correctly refuses, and the test wrongly fails.
        bin_dir = self.repo / "fake-bin"
        bin_dir.mkdir()
        uname = bin_dir / "uname"
        uname.write_text("#!/bin/sh\necho Linux\n")
        uname.chmod(0o755)

        result = run_bash(
            f'PATH="{bin_dir}:$PATH"\nsource ./setup.sh\nrefuse_unsupported_os\necho REACHED',
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


class InstallSpeakerModelTests(SetupShTestCase):
    """The speaker model is fetched for everyone, checked against a pinned
    SHA-256, and fetched again only when the file there does not match.

    `curl` is a script on `PATH` that copies a local file to the `--output`
    path and records that it ran, so nothing here reaches the network. The
    pinned checksum is `readonly` in `setup.sh`, so the copy under test has it
    swapped for the checksum of the stand-in file: what is under test is the
    check, not the real model's bytes.
    """

    GOOD = b"a stand-in for the model file"

    def setUp(self) -> None:
        super().setUp()
        pinned = re.compile(r'^readonly SPEAKER_MODEL_SHA256=".*"$', re.MULTILINE)
        script = self.repo / "setup.sh"
        text = script.read_text()
        self.assertRegex(text, pinned)
        script.write_text(
            pinned.sub(
                f'readonly SPEAKER_MODEL_SHA256="{hashlib.sha256(self.GOOD).hexdigest()}"',
                text,
            )
        )
        self.data_home = self.repo / "data"
        self.model = self.data_home / "murmly" / "voxceleb_resnet34_LM.onnx"
        self.curl_record = self.repo / "curl-calls.txt"
        self.served = self.repo / "served.bin"
        self.served.write_bytes(self.GOOD)

    def _fake_curl(self, *, exit_code: int = 0) -> Path:
        bin_dir = self.repo / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        curl = bin_dir / "curl"
        curl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "' + str(self.curl_record) + '"\n'
            "while [ $# -gt 0 ]; do\n"
            '    if [ "$1" = "--output" ]; then output="$2"; fi\n'
            "    shift\n"
            "done\n"
            f'if [ {exit_code} -eq 0 ]; then cp "{self.served}" "$output"; fi\n'
            f"exit {exit_code}\n"
        )
        curl.chmod(0o755)
        return bin_dir

    def _run(self, bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
        path = f'PATH="{bin_dir}:$PATH"\n' if bin_dir is not None else ""
        return run_bash(
            f'export XDG_DATA_HOME="{self.data_home}"\n{path}source ./setup.sh\ninstall_speaker_model',
            cwd=self.repo,
        )

    def test_fetches_the_model_into_the_data_directory_when_absent(self) -> None:
        result = self._run(self._fake_curl())

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(self.GOOD, self.model.read_bytes())
        self.assertEqual(1, len(self.curl_record.read_text().splitlines()))
        call = self.curl_record.read_text()
        self.assertIn("--fail", call)
        self.assertIn("--location", call)
        self.assertIn("voxceleb_resnet34_LM.onnx", call)
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_a_corrupted_file_is_replaced(self) -> None:
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"truncated")

        result = self._run(self._fake_curl())

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("does not match its checksum", result.stderr)
        self.assertEqual(self.GOOD, self.model.read_bytes())
        self.assertEqual(1, len(self.curl_record.read_text().splitlines()))

    def test_a_verified_file_is_not_downloaded_again(self) -> None:
        bin_dir = self._fake_curl()

        first = self._run(bin_dir)
        second = self._run(bin_dir)

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(1, len(self.curl_record.read_text().splitlines()))
        self.assertIn("Already in", second.stdout)

    def test_a_download_that_fails_the_checksum_is_deleted_and_warned_about(self) -> None:
        self.served.write_bytes(b"something else entirely")

        result = self._run(self._fake_curl())

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("did not match its checksum", result.stderr)
        self.assertFalse(self.model.exists())
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_a_failed_download_warns_and_does_not_stop_the_install(self) -> None:
        result = self._run(self._fake_curl(exit_code=22))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Could not fetch", result.stderr)
        self.assertFalse(self.model.exists())
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_a_missing_curl_warns_and_does_not_stop_the_install(self) -> None:
        # An empty directory first on `PATH` is not enough to hide the real
        # curl, so `have` is replaced, the seam the rest of the suite uses.
        result = run_bash(
            f'export XDG_DATA_HOME="{self.data_home}"\n'
            "source ./setup.sh\n"
            "have() { return 1; }\n"
            "install_speaker_model",
            cwd=self.repo,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("curl is not installed", result.stderr)

    def test_install_and_upgrade_both_fetch_it_without_being_asked(self) -> None:
        for command in ("install", "upgrade"):
            record = self.repo / f"{command}-calls.txt"
            result = run_bash(
                "source ./setup.sh\n"
                "require_uv() { :; }\n"
                "sync_environment() { :; }\n"
                "bind_hotkeys() { :; }\n"
                "restart_service() { :; }\n"
                "offer_announce_hook() { :; }\n"
                "report_state() { :; }\n"
                "recorded_hotkeys() { :; }\n"
                f'install_speaker_model() {{ echo ran >> "{record}"; }}\n'
                f"command_{command}",
                cwd=self.repo,
            )
            with self.subTest(command=command):
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue(record.exists())


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
