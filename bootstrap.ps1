# The Windows half of task 16.2. `bootstrap.sh`'s own header explains the
# split: this is the one precondition Windows needs before any `murmly`
# command can run at all -- `uv` on PATH -- and the speaker model download, and
# nothing else, because Windows has no `setup.sh` equivalent to hand off to. The system packages,
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

# The speaker-embedding model. These three values are the same ones as in
# `src/murmly/speakers.py` and `setup.sh`; `tests/test_speakers_model_pin.py`
# fails when they differ. The URL is pinned to one commit of the publisher's
# repository.
$SpeakerModelUrl = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx"
$SpeakerModelFile = "voxceleb_resnet34_LM.onnx"
$SpeakerModelSha256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"

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

# Where Murmly keeps what it downloads: `%LOCALAPPDATA%\murmly`, the same
# directory `default_data_dir` in `src/murmly/config.py` answers.
function Get-DataDir {
    $base = $env:LOCALAPPDATA
    if (-not $base) {
        $base = Join-Path $HOME "AppData\Local"
    }
    return Join-Path $base "murmly"
}

function Test-SpeakerModel {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ieq $SpeakerModelSha256
}

# The speaker-embedding model is fetched for everyone, whether or not a speaker
# mode is ever selected, so that enrolling a voice and switching a mode on never
# wait on a download. A file already there is kept only when its checksum
# matches: a corrupted or replaced one is fetched again. A failure warns and
# leaves the rest of the install alone, because speaker recognition is an option
# and the daemon runs without it -- which is why nothing here throws past the
# `catch`, and why it returns nothing.
function Install-SpeakerModel {
    $directory = Get-DataDir
    $target = Join-Path $directory $SpeakerModelFile
    $temporary = Join-Path $directory ".$SpeakerModelFile.part"

    Write-Host "Speaker model"
    try {
        if (Test-SpeakerModel -Path $target) {
            Write-Host "    Already in $directory."
            return
        }
        if (Test-Path -LiteralPath $target) {
            Write-Warning "$SpeakerModelFile in $directory does not match its checksum. Fetching it again."
            Remove-Item -LiteralPath $target -Force
        }

        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        Write-Host "    Fetching $SpeakerModelFile into $directory"
        Write-Host "    From $SpeakerModelUrl"
        # Windows PowerShell 5.1 draws a progress bar that slows a 26 MB
        # download to a crawl; the preference is put back afterwards.
        $previousProgress = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"
        try {
            # Downloaded beside the target and moved into place only once it
            # checks out, so an interrupted or altered fetch never leaves a file
            # that looks installed.
            Invoke-WebRequest -Uri $SpeakerModelUrl -OutFile $temporary -UseBasicParsing
        } finally {
            $ProgressPreference = $previousProgress
        }
        if (-not (Test-SpeakerModel -Path $temporary)) {
            Remove-Item -LiteralPath $temporary -Force
            Write-Warning "$SpeakerModelFile did not match its checksum and was deleted. Speaker recognition stays unavailable until it is in $directory."
            return
        }
        Move-Item -LiteralPath $temporary -Destination $target -Force
    } catch {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        Write-Warning "Could not fetch $SpeakerModelFile ($($_.Exception.Message)). Speaker recognition stays unavailable until it is in $directory."
    }
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
#
# The speaker model is fetched on `install` and `upgrade`, the two commands
# `setup.sh` fetches it on, and outside `Invoke-Bootstrap` so that a test of the
# hand-off to `uv` never reaches the network.
if ($MyInvocation.InvocationName -ne '.') {
    if ($args.Count -gt 0 -and $args[0] -in @("install", "upgrade")) {
        Install-SpeakerModel
    }
    exit (Invoke-Bootstrap -Arguments $args)
}
