# The Windows half of task 16.2. `bootstrap.sh`'s own header explains the
# split: this is the one precondition Windows needs before any `murmly`
# command can run at all -- `uv` on PATH -- and nothing else, because
# Windows has no `setup.sh` equivalent to hand off to. The system packages,
# extras-carrying sync, and GPU swap `setup.sh` still wraps for Linux
# (task 16.1) already live in `murmly sync` itself, which is what this hands
# off to directly.
#
#   .\bootstrap.ps1 install Meta+X
#   .\bootstrap.ps1 sync --cuda
#
# Every argument is passed straight through to `murmly`, run inside this
# checkout's own environment (`uv run`, which syncs it on demand -- the same
# "no environment yet" bootstrap `setup.sh`'s own `sync_environment` performs
# with a plain `uv sync --locked` before it can call `murmly sync` itself).
#
# Task 16.2: parsed and its argument handling exercised against a stubbed
# `uv` in `tests/test_bootstrap_ps1.py`, on both `pwsh` and, on the Windows CI
# runner, `powershell.exe` (5.1) besides -- which is what caught and fixed a
# real defect here (see the comment on the `-ErrorAction Continue` below).
# What remains unconfirmed is the one thing no CI job can stand in for: a
# real `uv` install running for real on a real Windows machine, since that
# is exactly the network call this file's own tests stub out rather than run.
# Report a failure there in exactly the terms `docs/agent-notes/` records
# one, the same as any other Windows-only path in this codebase not yet
# confirmed on hardware.

$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Uv {
    return [bool](Get-Command uv -ErrorAction SilentlyContinue)
}

function Install-Uv {
    Write-Host "uv is not installed. Installing it now:"
    Write-Host "    powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    # The installer places `uv.exe` under `%USERPROFILE%\.local\bin` and
    # updates the registry's persisted `PATH`, which this already-running
    # process does not re-read on its own. Added directly instead, ahead of
    # what is already on `PATH`, so the freshly installed `uv` is the one
    # `Test-Uv` (and `murmly`, once handed off to) finds next.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

function Invoke-Bootstrap {
    param([string[]]$Arguments)

    if (-not (Test-Uv)) {
        Install-Uv
        if (-not (Test-Uv)) {
            # -ErrorAction Continue overrides the script-wide "Stop" above for
            # this one call: with it inherited, `Write-Error` becomes a
            # terminating error and the `return 1` right after it never runs
            # -- confirmed by dot-sourcing this file and calling
            # `Invoke-Bootstrap` directly, which is exactly the composition
            # the guard below exists for. Run as the top-level script this
            # still happened to exit non-zero, because an uncaught terminating
            # error at the top level is itself PowerShell's own non-zero exit
            # -- but a caller that dot-sources this file and reads the
            # returned code, the way the guard below's own comment describes,
            # got an unhandled exception instead of a 1.
            Write-Error "uv installed but is still not on PATH. Open a new terminal and run:`n    uv run --project `"$RepoDir`" murmly $Arguments" -ErrorAction Continue
            return 1
        }
    }

    & uv run --project $RepoDir murmly @Arguments
    return $LASTEXITCODE
}

# `$MyInvocation.InvocationName` is `.` when this file has been dot-sourced
# rather than run, the same "sourced and exercised on its own" convention
# `setup.sh`'s and `bootstrap.sh`'s own trailing guards use -- so a test can
# dot-source this file and call `Invoke-Bootstrap` directly, against faked
# `Test-Uv`/`Install-Uv`, without it running for real.
if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-Bootstrap -Arguments $args)
}
