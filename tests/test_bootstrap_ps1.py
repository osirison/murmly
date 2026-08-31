"""`bootstrap.ps1`, task 16.2's Windows half: install `uv` if it is missing,
then hand off to `uv run --project <repo> murmly @Arguments` directly --
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

_CALL_PATTERN = re.compile(r"^run --project (.+) murmly (.*)$")


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
            '$exitCode = Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            'Write-Output "EXITCODE:[$exitCode]"\n'
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
            '$exitCode = Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            'Write-Output "EXITCODE:[$exitCode]"\n'
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
            '$exitCode = Invoke-Bootstrap -Arguments @("upgrade")\n'
            'Write-Output "EXITCODE:[$exitCode]"\n'
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
            '    $exitCode = Invoke-Bootstrap -Arguments @("install", "Meta+X")\n'
            '    Write-Output "EXITCODE:[$exitCode]"\n'
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
            '$exitCode = Invoke-Bootstrap -Arguments @("uninstall", "--purge", "--yes")\n'
            'Write-Output "EXITCODE:[$exitCode]"\n'
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


if __name__ == "__main__":
    unittest.main()
