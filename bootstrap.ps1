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
# Not run on a real Windows machine: written against `uv`'s own documented
# Windows install command and PowerShell's own environment-variable and
# process-launching syntax, not exercised on a live interpreter. Report a
# failure here in exactly the terms `docs/agent-notes/` records one, the same
# as any other Windows-only path in this codebase not yet confirmed live.

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
            Write-Error "uv installed but is still not on PATH. Open a new terminal and run:`n    uv run --project `"$RepoDir`" murmly $Arguments"
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
