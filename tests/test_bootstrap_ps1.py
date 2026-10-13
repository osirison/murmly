"""`bootstrap.ps1`, task 16.2's Windows half: install `uv` if it is missing,
sync first on `install` and `upgrade`, then hand off to
`uv run --no-sync --project <repo> murmly @Arguments` directly --
Windows has no `setup.sh` to hand off to, unlike `bootstrap.sh`'s Linux side
(`test_bootstrap_sh.py`).

Until this file, `bootstrap.ps1` had never been run, executed, or even parsed
by a real PowerShell interpreter: it was written against `uv`'s own
documented Windows install command and PowerShell's own syntax, with no
Windows machine or `pwsh`/`powershell` available to check either. Every test
here skips, naming the absence, on a machine with neither interpreter on
`PATH` -- the Windows CI runner has `pwsh` preinstalled and its own
`powershell.exe` (5.1, "Desktop" edition) besides, and this suite also runs,
for real, wherever a developer happens to have `pwsh` installed for other
reasons, this one included (`dotnet tool install --global PowerShell`).

Real `uv` is never invoked. Every scenario below stubs it out with a
PowerShell function named `uv` -- functions win over an external executable
of the same name in PowerShell's own command resolution, confirmed directly
against this machine's real, `dnf`-installed `uv` binary -- so nothing here
reaches the network or a real checkout, the same guarantee
`test_bootstrap_sh.py`'s fake `setup.sh` gives its side.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PS1 = REPO_ROOT / "bootstrap.ps1"

# `pwsh` (PowerShell 7+) is preferred because it is what this development
# machine and every GitHub-hosted runner -- Linux, Windows and macOS alike --
# carry by default. `powershell` (Windows PowerShell 5.1, the "Desktop"
# edition) is the fallback: it is the interpreter `bootstrap.ps1`'s own
# instructions actually target, the one a fresh Windows machine has before
# anything else is installed, and confirmed API-compatible with `pwsh` for
# everything this file exercises -- `[System.Management.Automation.Language.
# Parser]::ParseFile` and ordinary function/scope behaviour are both part of
# `System.Management.Automation`, not something `pwsh` added.
_INTERPRETER = shutil.which("pwsh") or shutil.which("powershell")

_CALL_PATTERN = re.compile(r"^run --no-sync --project (.+) murmly (.*)$")


def _path_without_real_uv() -> str:
    """`PATH`, with any directory that holds a real `uv` removed.

    CI installs `uv` (`astral-sh/setup-uv`) before this suite runs, and this
    development machine has one as a system package -- so a scenario that
    means to test `uv` being genuinely absent has to hide it for real:
    `Test-Uv`'s own `Get-Command uv -ErrorAction SilentlyContinue` would
    otherwise find it, whatever this file's own fakes do.
    """
    executable_name = "uv.exe" if sys.platform == "win32" else "uv"
    kept = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and not (Path(entry) / executable_name).is_file()
    ]
    return os.pathsep.join(kept)


class BootstrapPs1SyntaxTests(unittest.TestCase):
    """`ParseFile` reports syntax errors without executing a single line of
    the script -- the PowerShell equivalent of `python -m py_compile`, and
    the minimum task 16.2 asks for."""

    def setUp(self) -> None:
        if _INTERPRETER is None:
            self.skipTest("no PowerShell interpreter (pwsh or powershell) on PATH")

    def test_parses_without_syntax_errors(self) -> None:
        script = (
            "$parseErrors = $null; $tokens = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{BOOTSTRAP_PS1}', [ref]$tokens, [ref]$parseErrors); "
            "if ($parseErrors.Count -gt 0) { "
            "$parseErrors | ForEach-Object { Write-Output $_.ToString() }; exit 1 "
            "} else { exit 0 }"
        )
        result = subprocess.run(
            [_INTERPRETER, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

        self.assertEqual(0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")


class BootstrapPs1ArgumentHandlingTests(unittest.TestCase):
    """The five scenarios `test_bootstrap_sh.py` already covers for
    `bootstrap.sh`, carried over to its Windows twin. Each writes a small
    driver script that dot-sources the real `bootstrap.ps1` -- confirmed by
    the guard test below to never auto-run it -- then calls `Invoke-Bootstrap`
    directly, against a stubbed `uv` and/or `Install-Uv`, exactly the
    composition `bootstrap.ps1`'s own trailing comment says a test should use.
    """

    def setUp(self) -> None:
        if _INTERPRETER is None:
            self.skipTest("no PowerShell interpreter (pwsh or powershell) on PATH")
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name)

    def _run(self, body: str, *, hide_real_uv: bool = False, timeout: float = 60):
        driver = self.dir / "driver.ps1"
        driver.write_text(body, encoding="utf-8")
        env = os.environ.copy()
        if hide_real_uv:
            env["PATH"] = _path_without_real_uv()
        return subprocess.run(
            [_INTERPRETER, "-NoProfile", "-NonInteractive", "-File", str(driver)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )

    def _assert_call(self, stdout: str, expected_arguments: str) -> None:
        """Asserts a `CALLARGS:[...]` line names this repo and forwards
        `expected_arguments` unchanged, without pinning the exact path
        separator a real Windows run would format differently from this
        development machine.
        """
        match = re.search(r"^CALLARGS:\[(.*)\]$", stdout, re.MULTILINE)
        self.assertIsNotNone(match, f"no CALLARGS line in:\n{stdout}")
        call = _CALL_PATTERN.match(match.group(1))
        self.assertIsNotNone(call, f"unexpected call shape: {match.group(1)}")
        self.assertEqual(REPO_ROOT, Path(call.group(1)), "murmly was not run inside this checkout")
        self.assertEqual(expected_arguments, call.group(2))

    def test_hands_off_to_uv_when_it_is_already_present(self) -> None:
        result = self._run(
            'function uv { $global:UvCallArgs = $args -join " "; $global:LASTEXITCODE = 0 }\n'
            f'. "{BOOTSTRAP_PS1}"\n'
            'Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            'Write-Output "EXITCODE:[$script:ExitCode]"\n'
            'Write-Output "CALLARGS:[$UvCallArgs]"\n'
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self._assert_call(result.stdout, "install Meta+X")

    def test_never_attempts_to_install_uv_when_it_is_already_present(self) -> None:
        result = self._run(
            'function uv { $global:LASTEXITCODE = 0 }\n'
            f'. "{BOOTSTRAP_PS1}"\n'
            "function Install-Uv { $global:InstallCalled = $true }\n"
            'Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            'Write-Output "EXITCODE:[$script:ExitCode]"\n'
            'Write-Output "INSTALLCALLED:[$([bool]$InstallCalled)]"\n'
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self.assertIn("INSTALLCALLED:[False]", result.stdout)

    def test_installs_uv_then_hands_off_when_it_is_missing(self) -> None:
        """`Install-Uv` is faked to define a real `uv` function as its own
        effect, the same way the real one makes a real `uv.exe` reachable --
        so the second `Test-Uv` call inside `Invoke-Bootstrap`, unfaked, finds
        it exactly the way it would find a freshly installed one.
        """
        result = self._run(
            f'. "{BOOTSTRAP_PS1}"\n'
            "function Install-Uv {\n"
            '    Write-Host "INSTALLED"\n'
            "    function global:uv {\n"
            '        $global:UvCallArgs = $args -join " "\n'
            "        $global:LASTEXITCODE = 0\n"
            "    }\n"
            "}\n"
            'Invoke-Bootstrap -Arguments @("upgrade")\n'
            'Write-Output "EXITCODE:[$script:ExitCode]"\n'
            'Write-Output "CALLARGS:[$UvCallArgs]"\n',
            hide_real_uv=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("INSTALLED", result.stdout)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self._assert_call(result.stdout, "upgrade")

    def test_refuses_and_never_hands_off_when_uv_still_is_not_on_path_afterward(self) -> None:
        """Pins the fix alongside this test: before it, the `Write-Error` on
        this path was a terminating error under the script's own
        `$ErrorActionPreference = "Stop"`, so the `return 1` right after it
        never ran and this call raised instead of returning -- confirmed by
        running the pre-fix script through exactly this harness. No `uv`
        function is ever defined here and `hide_real_uv` keeps a real one off
        `PATH`, so `Test-Uv` is answered for real, both before and after the
        no-op `Install-Uv` below, by the same `Get-Command` the script itself
        uses.
        """
        result = self._run(
            f'. "{BOOTSTRAP_PS1}"\n'
            'function Install-Uv { Write-Host "install attempted" }\n'
            "try {\n"
            '    Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            '    Write-Output "EXITCODE:[$script:ExitCode]"\n'
            "} catch {\n"
            '    Write-Output "CAUGHT:[$($_.Exception.Message)]"\n'
            "}\n",
            hide_real_uv=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("CAUGHT", result.stdout)
        self.assertIn("EXITCODE:[1]", result.stdout)
        self.assertIn("still not on PATH", result.stdout + result.stderr)

    def test_every_argument_is_forwarded_unchanged(self) -> None:
        result = self._run(
            'function uv { $global:UvCallArgs = $args -join " "; $global:LASTEXITCODE = 0 }\n'
            f'. "{BOOTSTRAP_PS1}"\n'
            'Invoke-Bootstrap -Arguments @("uninstall", "--purge", "--yes")\n'
            'Write-Output "EXITCODE:[$script:ExitCode]"\n'
            'Write-Output "CALLARGS:[$UvCallArgs]"\n'
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self._assert_call(result.stdout, "uninstall --purge --yes")

    def test_dot_sourcing_does_not_auto_run(self) -> None:
        """The guard every other test here depends on, pinned directly: a
        `uv` defined before the dot-source must still be uncalled
        immediately afterward, which is only true if
        `$MyInvocation.InvocationName -ne '.'` is false while dot-sourcing --
        confirmed empirically, not just read off the script's own comment.
        """
        result = self._run(
            'function uv { $global:UvCallArgs = "called" }\n'
            f'. "{BOOTSTRAP_PS1}"\n'
            'Write-Output "CALLARGS:[$UvCallArgs]"\n'
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("CALLARGS:[]", result.stdout)


class BootstrapPs1SpeakerModelTests(unittest.TestCase):
    """`Install-SpeakerModel`: fetched for everyone, checked against a pinned
    SHA-256, fetched again only when the file there does not match.

    `Invoke-WebRequest` is a function that copies a local file to `-OutFile`
    and counts the call, so nothing here reaches the network -- a function
    wins over the cmdlet of the same name in PowerShell's command resolution,
    the same fact the `uv` stubs above rely on. The pinned checksum is a plain
    variable, so each scenario sets it to the stand-in file's checksum: what is
    under test is the check, not the real model's bytes.
    """

    GOOD = b"a stand-in for the model file"

    def setUp(self) -> None:
        if _INTERPRETER is None:
            self.skipTest("no PowerShell interpreter (pwsh or powershell) on PATH")
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name)
        self.local = self.dir / "local"
        self.model = self.local / "murmly" / "voxceleb_resnet34_LM.onnx"
        self.served = self.dir / "served.bin"
        self.served.write_bytes(self.GOOD)

    def _run(self, body: str, *, served_throws: bool = False):
        checksum = hashlib.sha256(self.GOOD).hexdigest()
        download = (
            'Set-Content -LiteralPath $OutFile -Value partial; throw "network down"'
            if served_throws
            else f'Copy-Item -LiteralPath "{self.served}" -Destination $OutFile'
        )
        driver = self.dir / "driver.ps1"
        driver.write_text(
            "function Invoke-WebRequest {\n"
            "    param($Uri, $OutFile, [switch]$UseBasicParsing)\n"
            "    $global:Fetches++\n"
            f"    {download}\n"
            "}\n"
            f'. "{BOOTSTRAP_PS1}"\n'
            f'$SpeakerModelSha256 = "{checksum}"\n'
            f'$env:LOCALAPPDATA = "{self.local}"\n' + body,
            encoding="utf-8",
        )
        return subprocess.run(
            [_INTERPRETER, "-NoProfile", "-NonInteractive", "-File", str(driver)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_fetches_it_into_the_data_directory_when_absent(self) -> None:
        result = self._run('Install-SpeakerModel\nWrite-Output "FETCHES:[$Fetches]"\n')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FETCHES:[1]", result.stdout)
        self.assertEqual(self.GOOD, self.model.read_bytes())
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_a_verified_file_is_not_downloaded_again(self) -> None:
        result = self._run(
            "Install-SpeakerModel\n"
            "Install-SpeakerModel\n"
            'Write-Output "FETCHES:[$Fetches]"\n'
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FETCHES:[1]", result.stdout)
        self.assertIn("Already in", result.stdout)

    def test_a_corrupted_file_is_replaced(self) -> None:
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"truncated")

        result = self._run('Install-SpeakerModel\nWrite-Output "FETCHES:[$Fetches]"\n')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FETCHES:[1]", result.stdout)
        self.assertIn("does not match its checksum", result.stdout + result.stderr)
        self.assertEqual(self.GOOD, self.model.read_bytes())

    def test_a_download_that_fails_the_checksum_is_deleted_and_warned_about(self) -> None:
        self.served.write_bytes(b"something else entirely")

        result = self._run('Install-SpeakerModel\nWrite-Output "DONE"\n')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DONE", result.stdout)
        self.assertIn("did not match its checksum", result.stdout + result.stderr)
        self.assertFalse(self.model.exists())
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_a_failed_download_warns_and_does_not_throw(self) -> None:
        result = self._run('Install-SpeakerModel\nWrite-Output "DONE"\n', served_throws=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DONE", result.stdout)
        self.assertIn("Could not fetch", result.stdout + result.stderr)
        self.assertFalse(self.model.exists())
        self.assertEqual([], list(self.model.parent.glob(".*.part")))

    def test_dot_sourcing_does_not_fetch(self) -> None:
        result = self._run('Write-Output "FETCHES:[$Fetches]"\n')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("FETCHES:[]", result.stdout)


class BootstrapPs1CommandTests(unittest.TestCase):
    """The script run as a command, which is the only way to reach its trailing
    guard: `install` fetches the model and then hands off to `murmly`, while
    `upgrade` fetches the model and stops, because `murmly` has no `upgrade`
    subcommand to hand it to.

    The driver calls the real script with `&` after defining `Invoke-WebRequest`,
    `Get-FileHash` and `uv` as functions, which the script sees through
    PowerShell's dynamic scoping and which win over the cmdlets and executable
    of the same names. `Get-FileHash` answers the script's own pinned checksum
    for any file, since the pinned value is set inside the script and a test
    cannot replace it.
    """

    def setUp(self) -> None:
        if _INTERPRETER is None:
            self.skipTest("no PowerShell interpreter (pwsh or powershell) on PATH")
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name)
        self.local = self.dir / "local"
        self.model = self.local / "murmly" / "voxceleb_resnet34_LM.onnx"
        match = re.search(r'^\$SpeakerModelSha256 = "([0-9a-f]{64})"$', BOOTSTRAP_PS1.read_text(encoding="utf-8"), re.MULTILINE)
        assert match is not None
        self.checksum = match.group(1)

    def _run(self, command: str, *, download_fails: bool = False, sync_fails: bool = False):
        download = (
            'throw "network down"'
            if download_fails
            else "Set-Content -LiteralPath $OutFile -Value stand-in"
        )
        driver = self.dir / "driver.ps1"
        driver.write_text(
            "function Invoke-WebRequest {\n"
            "    param($Uri, $OutFile, [switch]$UseBasicParsing)\n"
            "    $global:Fetches++\n"
            f"    {download}\n"
            "}\n"
            f'function Get-FileHash {{ param($LiteralPath, $Algorithm) [pscustomobject]@{{ Hash = "{self.checksum}" }} }}\n'
            "function uv {\n"
            '    $global:UvCallArgs = $args -join " "\n'
            '    $global:UvCalls += ,($args -join " ")\n'
            '    if ($global:FailSync -and ($args -contains "sync")) { $global:LASTEXITCODE = 1 }\n'
            "    else { $global:LASTEXITCODE = 0 }\n"
            "}\n"
            f'$env:LOCALAPPDATA = "{self.local}"\n'
            f'{"$global:FailSync = $true" if sync_fails else ""}\n'
            f'& "{BOOTSTRAP_PS1}" {command}\n'
            'Write-Output "EXITCODE:[$LASTEXITCODE]"\n'
            'Write-Output "FETCHES:[$Fetches]"\n'
            'Write-Output "UVCALL:[$UvCallArgs]"\n'
            'Write-Output "UVCALLS:[$($UvCalls -join "|")]"\n',
            encoding="utf-8",
        )
        return subprocess.run(
            [_INTERPRETER, "-NoProfile", "-NonInteractive", "-File", str(driver)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def _murmly_calls(self, stdout: str) -> list[str]:
        """The `murmly` arguments of every `uv run` the script made, in order."""
        match = re.search(r"^UVCALLS:\[(.*)\]$", stdout, re.MULTILINE)
        self.assertIsNotNone(match, f"no UVCALLS line in:\n{stdout}")
        calls = [call for call in match.group(1).split("|") if call]
        for call in calls:
            if call.startswith("run"):
                self.assertTrue(
                    call.startswith("run --no-sync --project "),
                    f"a plain on-demand sync would put the CPU ONNX Runtime back: {call}",
                )
        return [call.split(" murmly ", 1)[1] for call in calls if call.startswith("run")]

    def test_upgrade_syncs_then_fetches_the_model_and_does_not_hand_off(self) -> None:
        result = self._run("upgrade")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self.assertIn("FETCHES:[1]", result.stdout)
        self.assertEqual(["sync"], self._murmly_calls(result.stdout))
        self.assertTrue(self.model.is_file())

    def test_upgrade_stops_when_the_sync_fails(self) -> None:
        result = self._run("upgrade", sync_fails=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[1]", result.stdout)
        self.assertIn("FETCHES:[]", result.stdout)
        self.assertFalse(self.model.exists())

    def test_upgrade_with_the_model_already_there_exits_zero_without_fetching(self) -> None:
        self.model.parent.mkdir(parents=True)
        self.model.write_bytes(b"already here")

        result = self._run("upgrade")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self.assertIn("FETCHES:[]", result.stdout)
        self.assertEqual(["sync"], self._murmly_calls(result.stdout))

    def test_upgrade_exits_one_when_the_model_could_not_be_fetched(self) -> None:
        result = self._run("upgrade", download_fails=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[1]", result.stdout)
        self.assertIn("Could not fetch", result.stdout + result.stderr)
        self.assertEqual(["sync"], self._murmly_calls(result.stdout))
        self.assertFalse(self.model.exists())

    def test_install_syncs_first_fetches_the_model_then_hands_off(self) -> None:
        result = self._run("install Meta+X")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self.assertIn("FETCHES:[1]", result.stdout)
        self.assertEqual(["sync", "install Meta+X"], self._murmly_calls(result.stdout))
        self.assertRegex(result.stdout, r"UVCALL:\[run --no-sync --project .+ murmly install Meta\+X\]")

    def test_the_sync_options_go_to_the_sync_and_not_to_install(self) -> None:
        result = self._run("install --cuda --yes Meta+X Meta+A")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["sync --cuda --yes", "install Meta+X Meta+A"], self._murmly_calls(result.stdout)
        )

    def test_upgrade_forwards_the_sync_options_too(self) -> None:
        result = self._run("upgrade --no-tts --yes")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["sync --no-tts --yes"], self._murmly_calls(result.stdout))

    def test_a_plain_sync_keeps_its_own_arguments(self) -> None:
        result = self._run("sync --cuda")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["sync --cuda"], self._murmly_calls(result.stdout))

    def test_install_does_nothing_more_when_the_sync_fails(self) -> None:
        result = self._run("install Meta+X", sync_fails=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[1]", result.stdout)
        self.assertIn("FETCHES:[]", result.stdout)
        self.assertNotIn("install Meta+X", " ".join(self._murmly_calls(result.stdout)))

    def test_a_plain_command_is_never_synced_first(self) -> None:
        result = self._run("status")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["status"], self._murmly_calls(result.stdout))

    def test_install_still_hands_off_when_the_model_could_not_be_fetched(self) -> None:
        result = self._run("install Meta+X", download_fails=True)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("EXITCODE:[0]", result.stdout)
        self.assertIn("Could not fetch", result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"UVCALL:\[run --no-sync --project .+ murmly install Meta\+X\]")


class BootstrapPs1PromptReachesThePersonTests(unittest.TestCase):
    """A real native `uv`, not a PowerShell function, because the question is how
    PowerShell wires a native command's output, which a function stub cannot show.

    The fake prints a prompt with no trailing newline, as `murmly sync` does
    ("Install the GPU runtime? [y/N] "), flushes, then waits for one line on
    standard input and exits with the number it reads. Put in a pipeline
    (`| Out-Host`, or a parenthesised call), PowerShell holds a partial line back
    until the process ends, so the person sits at a prompt they cannot see. The
    test reads the script's output on a thread and requires the prompt to arrive
    BEFORE any answer is written.
    """

    PROMPT = "Install the GPU runtime? [y/N] "
    FAKE_UV = (
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['run'] and 'murmly' in args and args[args.index('murmly') + 1:][:1] == ['sync']:\n"
        f"    sys.stdout.write({PROMPT!r})\n"
        "    sys.stdout.flush()\n"
        "    line = sys.stdin.readline()\n"
        "    sys.exit(int(line.strip() or 0))\n"
        "sys.exit(0)\n"
    )

    def setUp(self) -> None:
        if _INTERPRETER is None:
            self.skipTest("no PowerShell interpreter (pwsh or powershell) on PATH")
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name)
        script = self.dir / "fake_uv.py"
        script.write_text(self.FAKE_UV, encoding="utf-8")
        if sys.platform == "win32":
            launcher = self.dir / "uv.cmd"
            launcher.write_text(f'@"{sys.executable}" -u "{script}" %*\r\n', encoding="utf-8")
        else:
            launcher = self.dir / "uv"
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" -u "{script}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)

    def _start(self, *arguments: str) -> subprocess.Popen:
        env = os.environ.copy()
        env["PATH"] = str(self.dir) + os.pathsep + _path_without_real_uv()
        # Never let a real run write under a real profile.
        for name in ("LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            env[name] = str(self.dir / name)
        process = subprocess.Popen(
            [_INTERPRETER, "-NoProfile", "-NonInteractive", "-File", str(BOOTSTRAP_PS1), *arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.addCleanup(self._kill, process)
        return process

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def _answer_when_prompted(self, process: subprocess.Popen, answer: str, timeout: float = 20.0) -> tuple[str, int]:
        import threading
        import time

        seen = bytearray()

        def read() -> None:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    return
                seen.extend(chunk)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        while self.PROMPT.encode() not in bytes(seen):
            if time.monotonic() > deadline or process.poll() is not None:
                break
            time.sleep(0.05)
        before_answer = bytes(seen).decode("utf-8", errors="replace")
        self.assertIn(
            self.PROMPT,
            before_answer,
            "the prompt did not reach the person before the process ended: the native "
            "command's output is being held back by a pipeline",
        )

        assert process.stdin is not None
        process.stdin.write((answer + "\n").encode())
        process.stdin.flush()
        code = process.wait(timeout=timeout)
        reader.join(timeout=10)
        return before_answer, code

    def test_a_prompt_with_no_newline_is_shown_before_it_is_answered_and_the_exit_code_comes_back(self) -> None:
        process = self._start("sync", "--cuda")

        _shown, code = self._answer_when_prompted(process, "7")

        self.assertEqual(7, code)


if __name__ == "__main__":
    unittest.main()
