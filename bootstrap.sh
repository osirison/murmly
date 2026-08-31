#!/usr/bin/env bash
#
# The one thing a fresh machine needs before `setup.sh` can run at all:
# `uv` on PATH. `setup.sh`'s own `require_uv` only ever names the command to
# run by hand and refuses (task 16.5 keeps that refusal exactly as it is, for
# anyone already used to it); this is the actual "install uv, then hand off"
# bootstrap design.md's "Installation: `murmly install` grows, `setup.sh`
# shrinks to a bootstrap" describes -- kept in its own script rather than
# folded into `setup.sh` because it is the one piece of this that is not
# Linux-specific in the way the rest of `setup.sh` is: `bootstrap.ps1` is the
# same two steps for Windows, where none of `setup.sh`'s own body applies.
#
#   ./bootstrap.sh install Meta+X
#   ./bootstrap.sh upgrade
#
# Every argument is passed straight through to `setup.sh`, which is what
# actually installs the system packages, syncs the environment (itself now
# delegated to `murmly sync`, task 16.1), binds the hotkey, and starts the
# service.

set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly DIR

have_uv() { command -v uv >/dev/null 2>&1; }

install_uv() {
    echo "uv is not installed. Installing it now:"
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer places `uv` under `~/.local/bin` or `~/.cargo/bin` and
    # updates the shell profile files it finds, neither of which this
    # already-running, non-interactive script's own environment picks up
    # without starting a new shell. Both are added directly instead, ahead of
    # what is already on `PATH`, so the freshly installed `uv` is the one
    # `have_uv` (and `setup.sh`, once handed off to) finds next.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
}

main() {
    if ! have_uv; then
        install_uv
        if ! have_uv; then
            echo "uv installed but is still not on PATH. Open a new terminal and run:" >&2
            echo "    $DIR/setup.sh $*" >&2
            return 1
        fi
    fi
    exec "$DIR/setup.sh" "$@"
}

# Guarded so `main` can be sourced and exercised on its own, the same
# convention `setup.sh`'s own trailing guard uses.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
