#!/usr/bin/env bash
#
# Install, upgrade, and remove Murmly.
#
#   ./setup.sh install Meta+X [Meta+A]
#   ./setup.sh upgrade
#   ./setup.sh hooks [claude|copilot|both|off]
#   ./setup.sh uninstall [--purge]
#
# Two things used to be easy to get wrong by hand here -- carrying the extras
# already installed across a `uv sync`, since a plain sync matches the
# environment exactly to what it is given and silently removes the CUDA
# wheels or speech output, and reapplying the GPU build of ONNX Runtime, which
# every sync puts back to the CPU one. Both moved into `murmly sync` itself
# (`src/murmly/environment.py`, `design.md`'s "Installation: `murmly install`
# grows, `setup.sh` shrinks to a bootstrap"), where they are testable and do
# not vary by platform. What stays here is what does not fit that description:
# pulling the source, naming and offering this Linux system's own packages for
# a `uv sync` that has not run yet to even make `murmly` callable, the
# synthesis model download, the announcement hooks, and the service.
#
# See README.md for what each piece does; this script only sequences them.

set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO

readonly KOKORO_RELEASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
readonly MODEL_FILE="kokoro-v1.0.onnx"
readonly VOICES_FILE="voices-v1.0.bin"

ASSUME_YES=0
WANT_CUDA=auto
WANT_TTS=auto
WANT_HOOKS=auto
PURGE=0

# ---------------------------------------------------------------- output ----

if [ -t 1 ]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; YELLOW=$'\e[33m'; RESET=$'\e[0m'
else
    BOLD=''; DIM=''; RED=''; YELLOW=''; RESET=''
fi

step() { printf '\n%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
info() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$RESET"; }
warn() { printf '    %sWarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
fail() { printf '\n%sError:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

confirm() {
    local prompt="$1"
    if [ "$ASSUME_YES" -eq 1 ]; then
        info "$prompt -- assuming yes (--yes)"
        return 0
    fi
    if [ ! -t 0 ]; then
        warn "$prompt -- skipped, nothing is attached to answer. Pass --yes to accept."
        return 1
    fi
    local reply
    read -r -p "    $prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

usage() {
    cat <<'USAGE'
Usage: ./setup.sh <command> [options]

Commands:
  install [HOTKEY] [SESSION_HOTKEY]
        Install system packages, sync the environment, bind the hotkeys, and
        start the service. HOTKEY defaults to whatever is already bound.
  upgrade
        Pull, re-sync with the extras already installed, rebind the recorded
        hotkeys, and restart the service.
  hooks [claude|copilot|both|off]
        Announce finished turns out loud: register Murmly's Stop hook with
        Claude Code, GitHub Copilot CLI, or both. With no argument, offers
        whichever is installed. `off` unregisters it.
  uninstall [--purge]
        Remove the service, the hotkeys, and the announcement hook. --purge
        also removes the virtual environment, the synthesis models, and the
        configuration.

Options:
  -y, --yes        Answer every prompt with yes, including the one confirming
                   what --purge is about to delete. Required when nothing is
                   attached to the terminal, where every prompt is otherwise
                   declined.
      --cuda       Install the GPU runtime extra. Default: offered when an
                   NVIDIA driver is present.
      --no-cuda    Leave the GPU runtime extra out.
      --tts        Fetch the synthesis models without asking. The synthesizer
                   itself is installed by default.
      --no-tts     Leave speech output out entirely, models and synthesizer.
      --hooks      With install: register the announcement hook without asking.
      --no-hooks   With install: do not offer it at all.
      --purge      With uninstall: also remove the environment, models, and
                   configuration.
  -h, --help       This message.

Unless --no-cuda says otherwise, every sync keeps the extras already installed,
so upgrading never removes a feature you had. Speech output needs no such care:
it is a default dependency group, so only --no-tts leaves it out.
USAGE
}

# ------------------------------------------------------------ discovery -----

have() { command -v "$1" >/dev/null 2>&1; }

murmly() { uv run --no-sync --project "$REPO" murmly "$@"; }

# `uv pip show` reads the project environment without changing it.
installed_package() { uv pip show --project "$REPO" "$1" >/dev/null 2>&1; }

require_uv() {
    if have uv; then
        return 0
    fi
    fail "uv is not installed. Install it, then run this again:
    curl -LsSf https://astral.sh/uv/install.sh | sh
  or: sudo dnf install uv"
}

# Murmly does not claim macOS (this change's binding scope decision), and the
# rest of this script assumes a Linux desktop -- `systemctl`, `dnf`/`apt`/etc,
# XDG directories -- deeply enough that running it there would otherwise fail
# with an error naming none of that, well after a venv had already been
# created. Checked before any command below writes anything, the same "every
# command refuses immediately, before a daemon starts, a channel is created,
# or a file is written" rule `cli._unsupported_platform_message` already
# applies once `murmly` itself is reachable -- this is what covers the gap
# before it is: a fresh checkout's first `uv sync` happens from this bootstrap,
# not from a `murmly` command that could refuse it itself.
refuse_unsupported_os() {
    if [ "$(uname -s)" = "Darwin" ]; then
        fail "Murmly does not support macOS. It supports Linux and Windows."
    fi
}

# --------------------------------------------------------------- extras -----

#: Whether the next sync should carry speech output. Yes by default, because the
#: synthesizer is a default dependency group and arrives without being asked for.
#:
#: An environment that exists and has no synthesizer in it was opted out of one,
#: so an upgrade leaves it that way. Undoing that decision silently is the same
#: fault in the other direction as the sync that removed speech output from a
#: running daemon.
wants_speech_output() {
    case "$WANT_TTS" in
        yes) return 0 ;;
        no) return 1 ;;
    esac
    if [ -d "$REPO/.venv" ] && ! installed_package kokoro-onnx; then
        return 1
    fi
    return 0
}

#: Task 16.1: carrying the extras already installed across a sync, offering
#: this Linux system's own packages (task 3.4), and reapplying the ONNX
#: Runtime GPU swap every sync undoes all moved into `murmly sync`
#: (`src/murmly/environment.py`) -- this is now a thin wrapper around it.
#:
#: On a checkout with no environment yet, `murmly` is not a command this
#: script can run at all -- `uv run --no-sync` has nothing to run without-
#: syncing *in*. A first, plain `uv sync --locked` is what makes `murmly`
#: reachable; `murmly sync` then reads what that plain sync actually
#: installed (no extras, every default group) and takes it from there, the
#: same as it would for an existing environment upgrading in place.
sync_environment() {
    step "Python environment"
    if [ ! -d "$REPO/.venv" ]; then
        info "No environment yet. Creating one before Murmly's own sync runs."
        ( cd "$REPO" && uv sync --locked )
    fi

    local want_models=0
    if wants_speech_output; then
        want_models=1
    fi

    local -a arguments=(sync --project "$REPO")
    case "$WANT_CUDA" in
        yes) arguments+=(--cuda) ;;
        no)  arguments+=(--no-cuda) ;;
    esac
    case "$WANT_TTS" in
        yes) arguments+=(--tts) ;;
        no)  arguments+=(--no-tts) ;;
    esac
    if [ "$ASSUME_YES" -eq 1 ]; then
        arguments+=(--yes)
    fi

    murmly "${arguments[@]}"

    if [ "$want_models" -eq 1 ]; then
        install_models
    fi
}

# --------------------------------------------------------------- models -----

data_dir() {
    printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/murmly"
}

#: The synthesis models live directly in the data directory, which is what
#: `[tts] model_dir` defaults to.
model_dir() {
    data_dir
}

hook_dir() {
    printf '%s\n' "$(data_dir)/hooks"
}

install_models() {
    step "Synthesis models"
    local directory
    directory="$(model_dir)"
    mkdir -p "$directory"

    local file
    local -a needed=()
    for file in "$MODEL_FILE" "$VOICES_FILE"; do
        if [ ! -s "$directory/$file" ]; then
            needed+=("$file")
        fi
    done

    if [ ${#needed[@]} -eq 0 ]; then
        info "Already in $directory."
        return 0
    fi

    # The one thing left to decide. The synthesizer package arrives with every
    # sync now, so the question this step used to ask -- whether to install
    # speech output at all -- is no longer a question. 340 MB still is, and
    # speech output stays off in configuration either way.
    if [ "$WANT_TTS" = "auto" ]; then
        info "Speech output lets Murmly speak text an agent sends it. The synthesizer is"
        info "installed; what is missing is about 340 MB of model files. It stays off in"
        info "configuration until you enable it."
        if ! confirm "Download the synthesis models?"; then
            note "Skipped. Place ${needed[*]} in $directory when you want them."
            return 0
        fi
    fi

    if ! have curl; then
        warn "curl is not installed, so the models were not fetched. Place ${needed[*]} in $directory."
        return 0
    fi

    info "Fetching ${needed[*]} into $directory"
    info "From $KOKORO_RELEASE"
    local temporary
    for file in "${needed[@]}"; do
        # Downloaded beside the target and moved into place, so an interrupted
        # fetch never leaves a truncated file that looks installed.
        temporary="$directory/.$file.part"
        if curl --fail --location --progress-bar --output "$temporary" "$KOKORO_RELEASE/$file"; then
            mv -- "$temporary" "$directory/$file"
        else
            rm -f -- "$temporary"
            warn "Could not fetch $file. Place it in $directory by hand."
        fi
    done
}

# ------------------------------------------------- agent announcements ------

#: The agents this machine has. Only what is here is offered.
detected_agents() {
    if have claude || [ -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" ]; then
        printf 'claude\n'
    fi
    if have copilot || [ -d "${COPILOT_HOME:-$HOME/.copilot}" ]; then
        printf 'copilot\n'
    fi
}

agent_label() {
    case "$1" in
        claude) printf 'Claude Code\n' ;;
        copilot) printf 'GitHub Copilot CLI\n' ;;
        *) printf '%s\n' "$1" ;;
    esac
}

#: The command socket's resolved path, straight from `murmly.config` --
#: `murmly doctor` already reports it, running under the venv `murmly()` wraps
#: -- rather than a second, shell-side copy of the fallback formula. Empty
#: when it cannot be asked (no synced virtual environment yet, as when
#: `./setup.sh hooks` runs before `./setup.sh install` ever has); a caller
#: gets to decide what emptiness means rather than this guessing on its
#: behalf.
resolved_socket_path() {
    murmly doctor 2>/dev/null | python3 -c '
import json, sys

try:
    report = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
path = report.get("socket_path")
if isinstance(path, str) and path:
    print(path)
' || true
}

install_announce_hook() {
    local agents="$1"
    local source="$REPO/hooks/murmly-announce.py"
    local instruction_source="$REPO/hooks/murmly-voice-note.py"
    local installer="$REPO/hooks/install_hooks.py"
    local target instruction_target
    target="$(hook_dir)/murmly-announce.py"
    instruction_target="$(hook_dir)/murmly-voice-note.py"

    if [ ! -f "$source" ] || [ ! -f "$installer" ]; then
        warn "This checkout has no announcement hook to install."
        return 0
    fi

    mkdir -p "$(hook_dir)"
    # Copied out of the checkout rather than pointed at in place. The hook runs
    # under the system Python with no virtual environment, and a registration
    # that names a path inside a moved checkout is a hook that stops firing
    # without saying so.
    install -m 0755 "$source" "$target"
    info "Installed $target"

    # The script that tells the agent to write a voice note in the first place.
    # A checkout without it still installs the announcement, which then reads
    # whatever the agent wrote for the screen, exactly as it did before.
    local instruction_flag=()
    if [ -f "$instruction_source" ]; then
        install -m 0755 "$instruction_source" "$instruction_target"
        info "Installed $instruction_target"
        instruction_flag=(--instruction-script "$instruction_target")
    fi

    # Resolved once here and baked into the registration, so the hook itself
    # never has to guess it at run time. Left unset -- rather than reimplemented
    # in bash -- when it cannot be resolved; the hook's own last-resort
    # fallback then applies, exactly as it always has.
    local socket_flag=()
    local socket
    socket="$(resolved_socket_path)"
    if [ -n "$socket" ]; then
        socket_flag=(--socket "$socket")
    fi

    python3 "$installer" --script "$target" "${instruction_flag[@]}" "${socket_flag[@]}" \
        --agents "$agents" | while IFS= read -r line; do
        info "$line"
    done
    note "A finished turn now plays three notes, names the project, and speaks the"
    note "agent's own <voice-note> when it writes one, or a summary of its message"
    note "when it does not."
    note "Silence the notes with MURMLY_ANNOUNCE_CHIME=0, stop asking the agent for a"
    note "voice note with MURMLY_ANNOUNCE_INSTRUCT=0; remove it all with"
    note "./setup.sh hooks off."

    case ",$agents," in
        *,copilot,*) announce_copilot_instruction ;;
    esac
}

announce_copilot_instruction() {
    # Copilot CLI documents no hook whose output reaches the model, so nothing
    # can install this for the person. Printing it is the whole of what Murmly
    # can do; until it is placed, Copilot's turns are announced from a summary.
    local instruction_source="$REPO/hooks/murmly-voice-note.py"
    [ -f "$instruction_source" ] || return 0
    note ""
    note "Copilot CLI cannot be told this automatically. Put the following in your"
    note "AGENTS.md to have it write voice notes too:"
    note ""
    MURMLY_ANNOUNCE_INSTRUCT=1 python3 "$instruction_source" | while IFS= read -r line; do
        note "    $line"
    done
}

remove_announce_hook() {
    local installer="$REPO/hooks/install_hooks.py"
    step "Agent announcements"
    if [ -f "$installer" ]; then
        python3 "$installer" --script "$(hook_dir)/murmly-announce.py" \
            --agents claude,copilot --remove | while IFS= read -r line; do
            info "$line"
        done
    else
        warn "This checkout has no hook installer, so any registration was left in place."
    fi
    rm -f -- "$(hook_dir)/murmly-announce.py"
    rm -f -- "$(hook_dir)/murmly-voice-note.py"
    rmdir -- "$(hook_dir)" 2>/dev/null || true
}

#: Offered during an install, where it is one more question rather than the
#: point of the run. `./setup.sh hooks` is the way to add it on its own.
offer_announce_hook() {
    if [ "$WANT_HOOKS" = "no" ]; then
        return 0
    fi
    local agents name
    agents="$(detected_agents | paste -sd, -)"
    if [ -z "$agents" ]; then
        return 0
    fi
    step "Agent announcements"
    if [ "$WANT_HOOKS" != "yes" ]; then
        for name in ${agents//,/ }; do
            info "Found $(agent_label "$name")"
        done
        info "Murmly can speak a summary out loud when one of these finishes a turn."
        if ! confirm "Set that up?"; then
            note "Skipped. Run './setup.sh hooks' whenever you want it."
            return 0
        fi
    fi
    install_announce_hook "$agents"
}

# -------------------------------------------------------------- hotkeys -----

#: The keys currently bound, read back from Murmly rather than guessed, as
#: "<window hotkey><tab><session hotkey>". Either field may be empty.
recorded_hotkeys() {
    murmly doctor 2>/dev/null | python3 -c '
import json, sys

try:
    report = json.load(sys.stdin)
except ValueError:
    sys.exit(0)

bound = {
    entry.get("purpose"): entry.get("hotkey")
    for entry in report.get("installation", {}).get("hotkeys", [])
}
print("\t".join([bound.get("window") or "", bound.get("session") or ""]))
' || true
}

bind_hotkeys() {
    local requested="${1:-}" session_hotkey="${2:-}"

    local recorded window_recorded session_recorded
    recorded="$(recorded_hotkeys)"
    window_recorded="$(printf '%s' "$recorded" | cut -f1)"
    session_recorded="$(printf '%s' "$recorded" | cut -f2)"

    local hotkey="$requested"
    if [ -z "$hotkey" ]; then
        hotkey="$window_recorded"
    fi

    # The recorded session key is reused only when the caller named nothing at
    # all. Naming one key deliberately rebinds that one alone, which is what
    # `murmly install` already means.
    if [ -z "$requested" ] && [ -z "$session_hotkey" ]; then
        session_hotkey="$session_recorded"
    fi

    if [ -z "$hotkey" ]; then
        fail "No hotkey was given and none is bound yet. Name one, for example:
    ./setup.sh install Meta+X"
    fi

    step "Hotkeys and service"
    if [ -n "$session_hotkey" ]; then
        info "Binding $hotkey (focused window) and $session_hotkey (speech session)"
        murmly install "$hotkey" "$session_hotkey"
    else
        info "Binding $hotkey (focused window)"
        murmly install "$hotkey"
    fi
}

# -------------------------------------------------------------- service -----

restart_service() {
    have systemctl || return 0
    systemctl --user list-unit-files murmly.service >/dev/null 2>&1 || return 0

    step "Service"
    # A unit left `failed` by an earlier crash refuses to start until it is
    # cleared, so this is not optional tidying.
    if [ "$(systemctl --user is-failed murmly.service 2>/dev/null || true)" = "failed" ]; then
        info "The unit was in a failed state. Clearing it."
        systemctl --user reset-failed murmly.service || true
    fi
    info "Restarting murmly.service"
    systemctl --user restart murmly.service
}

report_state() {
    step "State"
    # Captured in one call rather than run twice. Murmly logs an unavailable
    # feature to stderr as it builds the report, and the same line is already in
    # the body below.
    local report
    if ! report="$(murmly doctor 2>/dev/null)"; then
        warn "murmly doctor could not run. The environment may not be synced."
        return 0
    fi
    printf '%s\n' "$report" | python3 -c '
import json, sys

report = json.load(sys.stdin)
installation = report.get("installation", {})
speech = report.get("speech_output", {})
paste = report.get("paste_injection", {})


def line(label, value):
    print("    {:<16}{}".format(label, value))


line("installed:", installation.get("installed"))
line("service active:", installation.get("service_active"))
for entry in installation.get("hotkeys", []):
    purpose = entry.get("purpose")
    key = entry.get("hotkey") or "not bound"
    line(purpose + ":", "{}  ({})".format(key, entry.get("description")))
line("transcription:", "{} / {}".format(report.get("runtime_device"), report.get("runtime_compute_type")))
line("paste method:", paste.get("method") or "none")
line("speech output:", "available" if speech.get("available") else "unavailable")
if not speech.get("available") and speech.get("detail"):
    line("", speech["detail"])
'
    note "Full report: uv run --no-sync murmly doctor"
}

# ------------------------------------------------------------- commands -----

command_hooks() {
    local choice="${1:-}"
    case "$choice" in
        off|remove|none)
            remove_announce_hook
            return 0
            ;;
        both|"") ;;
        claude|copilot|claude,copilot|copilot,claude) ;;
        *) fail "Unknown agent: $choice. Use claude, copilot, both, or off." ;;
    esac

    step "Agent announcements"
    local agents="$choice"
    if [ "$agents" = "both" ]; then
        agents="claude,copilot"
    fi
    if [ -z "$agents" ]; then
        agents="$(detected_agents | paste -sd, -)"
        if [ -z "$agents" ]; then
            fail "Neither Claude Code nor Copilot CLI was found here. Name one anyway:
    ./setup.sh hooks claude"
        fi
        local name
        for name in ${agents//,/ }; do
            info "Found $(agent_label "$name")"
        done
        if ! confirm "Announce finished turns through Murmly for these?"; then
            info "Left alone."
            return 0
        fi
    fi
    install_announce_hook "$agents"
}

command_install() {
    require_uv
    sync_environment
    bind_hotkeys "${1:-}" "${2:-}"
    restart_service
    offer_announce_hook
    report_state
    step "Done"
    info "Press your hotkey once to confirm the desktop actually delivers it."
}

command_upgrade() {
    require_uv

    step "Source"
    if [ ! -d "$REPO/.git" ]; then
        note "Not a git checkout, so there is nothing to pull."
    elif [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        warn "The working tree has uncommitted changes. Leaving the source alone."
    else
        info "Pulling into $(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
        git -C "$REPO" pull --ff-only
    fi

    sync_environment

    # Rebound rather than left alone: the entrypoint Murmly recorded goes stale
    # when the environment is rebuilt, and the unit file changes between versions.
    local recorded
    recorded="$(recorded_hotkeys)"
    if [ -n "$(printf '%s' "$recorded" | cut -f1)" ]; then
        bind_hotkeys
    else
        note "Nothing is installed, so no hotkey was rebound."
        note "Run './setup.sh install <hotkey>' to install."
    fi

    restart_service
    report_state
    step "Done"
}

command_uninstall() {
    require_uv

    remove_announce_hook

    step "Service and hotkeys"
    if ! murmly uninstall; then
        warn "murmly uninstall reported a failure. Check what is left with: uv run --no-sync murmly doctor"
    fi

    local models config
    models="$(model_dir)"
    config="${XDG_CONFIG_HOME:-$HOME/.config}/murmly"

    if [ "$PURGE" -eq 0 ]; then
        step "Kept"
        note "Virtual environment: $REPO/.venv"
        note "Synthesis models:    $models"
        note "Configuration:       $config"
        note "Pass --purge to remove these too."
        return 0
    fi

    local -a targets=()
    local candidate
    for candidate in "$REPO/.venv" "$models" "$config"; do
        if [ -e "$candidate" ]; then
            targets+=("$candidate")
        fi
    done

    step "Purge"
    if [ ${#targets[@]} -eq 0 ]; then
        info "Nothing left to remove."
        return 0
    fi

    for candidate in "${targets[@]}"; do
        info "$candidate  ($(du -sh "$candidate" 2>/dev/null | cut -f1))"
    done
    if ! confirm "Remove these permanently?"; then
        info "Left in place."
        return 0
    fi
    for candidate in "${targets[@]}"; do
        rm -rf -- "$candidate"
        info "Removed $candidate"
    done
}

# ----------------------------------------------------------------- main -----

main() {
    local command="${1:-}"
    if [ $# -gt 0 ]; then
        shift
    fi

    local -a positional=()
    while [ $# -gt 0 ]; do
        case "$1" in
            -y|--yes) ASSUME_YES=1 ;;
            --cuda) WANT_CUDA=yes ;;
            --no-cuda) WANT_CUDA=no ;;
            --tts) WANT_TTS=yes ;;
            --no-tts) WANT_TTS=no ;;
            --hooks) WANT_HOOKS=yes ;;
            --no-hooks) WANT_HOOKS=no ;;
            --purge) PURGE=1 ;;
            -h|--help) usage; return 0 ;;
            -*) fail "Unknown option: $1" ;;
            *) positional+=("$1") ;;
        esac
        shift
    done

    # Skipped for `-h`/`--help`/no command at all: printing usage writes
    # nothing and starts nothing, so there is no reason to refuse it.
    case "$command" in
        -h|--help|help|"") ;;
        *) refuse_unsupported_os ;;
    esac

    case "$command" in
        install) command_install "${positional[@]}" ;;
        upgrade) command_upgrade ;;
        hooks) command_hooks "${positional[0]:-}" ;;
        uninstall) command_uninstall ;;
        -h|--help|help) usage ;;
        "") usage >&2; return 2 ;;
        *) printf 'Unknown command: %s\n\n' "$command" >&2; usage >&2; return 2 ;;
    esac
}

# Guarded so the functions above can be sourced and exercised on their own.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
