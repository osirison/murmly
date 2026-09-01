---
title: Testing bootstrap.ps1 without a Windows machine
description: dotnet tool install --global PowerShell gives a real pwsh to test against; that pwsh hangs forever on any file write made through this harness's Bash tool, so PowerShell test harnesses in this repo must capture results via stdout, never a file
trigger: bootstrap.ps1, pwsh, powershell, test_bootstrap_ps1.py, dotnet tool install, Add-Content, Out-File

depends_on: bootstrap.ps1, tests/test_bootstrap_ps1.py
recorded: 2026-09-01
---

# Testing `bootstrap.ps1` without a Windows machine

## Getting a local `pwsh`

This machine has no `pwsh`/`powershell` and Fedora's own repos do not carry
one. If a .NET SDK is already installed (`dotnet --version` succeeds), a real
`pwsh` 7+ is one command away and needs no `sudo`:

```bash
dotnet tool install --global PowerShell
```

This installs to `~/.dotnet/tools/pwsh` as a *dotnet global tool* -- a shim
that shells out to `dotnet exec ...` under the hood, not a self-contained
binary. That distinction matters for the hang below.

## A real Windows CI runner has this too

`pwsh` is preinstalled on every GitHub-hosted runner (`ubuntu-latest`,
`windows-latest`, `macos-latest`), so `tests/test_bootstrap_ps1.py` runs for
real everywhere CI runs it, not only on a genuine Windows box.
`shutil.which("pwsh") or shutil.which("powershell")` is the right gate: it
prefers `pwsh` (what this machine and every runner carry) and falls back to
`powershell` (Windows PowerShell 5.1, "Desktop" edition -- what a fresh
Windows machine has before anything else is installed, and the interpreter
`bootstrap.ps1`'s own instructions actually target).

## A file write from this `pwsh` hangs forever under the Bash tool

**Symptom:** any PowerShell script invoked as `pwsh -File ...` or
`pwsh -Command ...` through this harness's `Bash` tool that performs a file
write -- `Add-Content`, `Out-File`, plain `>` redirection, even raw
`[System.IO.File]::AppendAllText(...)` -- hangs indefinitely rather than
erroring or succeeding. It does not matter whether the write target is new or
existing, or whether the surrounding script logic is otherwise correct.
Confirmed minimal repro: a one-line script containing only
`Add-Content -Path "/tmp/x.txt" -Value "hello"` hangs; the same script with
that line removed returns instantly. `Ctrl-C`/`timeout`'s `SIGTERM` does not
reliably kill it either -- the dotnet-hosted process tree needs `pkill -9`.

Everything *else* about a dotnet-tool-hosted `pwsh` process works normally
under this Bash tool: `Write-Output`/`Write-Host` to stdout, reading files,
function definitions and scoping (including dynamically defining
`function global:foo {}` at runtime and finding it afterward with
`Get-Command`), dot-sourcing, `try`/`catch`, and external process invocation
all behave exactly as they do outside this harness and return fast.

**Fix:** never have a PowerShell test harness in this repo write to a file to
record what happened. Capture everything through a `$global:` variable and a
final `Write-Output "MARKER:[$value]"` line, then parse it out of
`subprocess.run(...).stdout` from the Python side. `tests/test_bootstrap_ps1.py`
does this throughout -- see `_assert_call`'s `CALLARGS:[...]` convention.

**Why it was not obvious:** the hang gives no error, no stack trace, and no
output at all (output appears to buffer until the process exits, so even a
`Write-Output` placed before the write never shows up while it hangs) --
indistinguishable from the interpreter itself being broken until the file
write is specifically isolated as the cause. Root cause not confirmed (it
looks like a sandboxed syscall a dotnet-hosted grandchild process makes for
file I/O gets silently dropped rather than rejected, but this was not traced
further once the workaround was found).
