#!/usr/bin/env python3
"""Register Murmly's announcement hook with Claude Code and GitHub Copilot CLI.

The two agents keep hook configuration in different places and in different
shapes, and only one of them offers a drop-in directory:

  Claude Code   ~/.claude/settings.json, under `hooks.Stop` and, when there is
                an instruction hook to register, `hooks.SessionStart`. There is
                nowhere else to put them, so this merges into a file full of
                settings that are none of Murmly's business. Every write is
                backed up first, and only entries Murmly recognises as its own
                are touched.

  Copilot CLI   ~/.copilot/hooks/murmly-announce.json, a file of its own.
                Installing is writing it and removing is deleting it, so there
                is no merge to get wrong.

Both agents send the same `Stop` payload, so both get the same announcement
script. Only Claude Code takes the instruction hook, because only Claude Code
documents a hook whose output reaches the model.

Run with --remove to take the registration out again. Both directions are
idempotent: running twice leaves one registration, and removing what was never
installed is not an error.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

#: What marks a hook entry as Murmly's. Matching on the script name rather than
#: the whole command is what lets an entry installed to a different path -- an
#: older hand-written one, say -- still be recognised and replaced.
MARKERS = ("murmly-announce", "murmly-voice-note")

#: The Claude Code events Murmly registers, and the only ones it will strip.
STOP_EVENT = "Stop"
SESSION_START_EVENT = "SessionStart"
CLAUDE_EVENTS = (STOP_EVENT, SESSION_START_EVENT)

COPILOT_HOOK_FILE = "murmly-announce.json"
TIMEOUT_SECONDS = 15
#: The announcement detaches and is waited on by nobody, so 15 seconds costs
#: nothing. The instruction is waited on by every session start, so it gets a
#: bound of its own that is short enough to be unnoticeable.
INSTRUCTION_TIMEOUT_SECONDS = 5


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def copilot_hooks_dir() -> Path:
    return Path(os.environ.get("COPILOT_HOME") or Path.home() / ".copilot") / "hooks"


def read_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Refusing to touch {path}: it could not be read as JSON ({error}).")
    if not isinstance(loaded, dict):
        raise SystemExit(f"Refusing to touch {path}: its top level is not an object.")
    return loaded


def write_json(path: Path, document: dict) -> None:
    """Write it, keeping a copy of whatever was there before.

    The backup is not ceremony. This file holds the person's own settings for a
    tool that is not Murmly, and a bad merge with no way back would be the worst
    thing an installer could do to them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".murmly-backup"))
    temporary = path.with_name(f".{path.name}.murmly-tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def is_murmly_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for key in ("command", "bash", "powershell"):
        value = entry.get(key)
        if isinstance(value, str) and any(marker in value for marker in MARKERS):
            return True
    return False


def strip_murmly(groups: list) -> tuple[list, int]:
    """Every Murmly hook out of one Claude event's list, and how many there were.

    Groups are rebuilt rather than filtered in place so that a group left with
    no hooks disappears with them, instead of accumulating as an empty matcher
    every time this runs.
    """
    kept: list = []
    removed = 0
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            kept.append(group)
            continue
        surviving = [hook for hook in hooks if not is_murmly_entry(hook)]
        removed += len(hooks) - len(surviving)
        if not surviving:
            continue
        kept.append({**group, "hooks": surviving})
    return kept, removed


def claude_entries(script: Path, instruction_script: Path | None) -> dict[str, dict]:
    """The hook entry Murmly registers for each Claude Code event.

    `Stop` is async. It detaches before it speaks and nobody waits on it, so
    fifteen seconds and a background run cost the turn nothing.

    `SessionStart` must not be async, and this is not a detail to tidy up later.
    Claude Code adds a SessionStart hook's plain-text stdout to the session's
    context; an async hook runs in the background, so there is no assembled
    context left for its output to reach. Registered async it would run, print,
    succeed, and instruct nobody -- a failure whose only symptom is announcements
    that stay extracts forever. It takes a shorter timeout for the same reason it
    is synchronous: every session start waits on it.
    """
    entries = {
        STOP_EVENT: {
            "type": "command",
            "command": f"python3 '{script}'",
            "timeout": TIMEOUT_SECONDS,
            "async": True,
            "statusMessage": "Announcing through Murmly",
        }
    }
    if instruction_script is not None:
        entries[SESSION_START_EVENT] = {
            "type": "command",
            "command": f"python3 '{instruction_script}'",
            "timeout": INSTRUCTION_TIMEOUT_SECONDS,
        }
    return entries


def install_claude(settings_path: Path, script: Path, instruction_script: Path | None = None) -> str:
    document = read_json(settings_path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"Refusing to touch {settings_path}: its `hooks` is not an object.")

    entries = claude_entries(script, instruction_script)
    replaced = 0
    for event in CLAUDE_EVENTS:
        groups = hooks.get(event)
        groups = groups if isinstance(groups, list) else []
        # Every event is stripped, not only the ones being written. Registering
        # without an instruction script has to take out a SessionStart entry an
        # earlier run left behind, or it goes on naming a script that is gone.
        groups, stripped = strip_murmly(groups)
        replaced += stripped
        if event in entries:
            # No matcher, so SessionStart runs for startup, resume, clear,
            # compact, and fork alike. Skipping compact would lose the
            # convention exactly when a long session rebuilds its context.
            groups.append({"hooks": [entries[event]]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)

    write_json(settings_path, document)
    if replaced:
        return f"Claude Code: registered in {settings_path} (replaced {replaced} earlier Murmly hook(s))"
    return f"Claude Code: registered in {settings_path}"


def remove_claude(settings_path: Path) -> str:
    document = read_json(settings_path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return f"Claude Code: nothing registered in {settings_path}"

    removed = 0
    for event in CLAUDE_EVENTS:
        groups = hooks.get(event)
        # An installation made before the instruction hook existed has no
        # SessionStart key at all. That is nothing to remove, not a failure.
        if not isinstance(groups, list):
            continue
        groups, stripped = strip_murmly(groups)
        if not stripped:
            continue
        removed += stripped
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event)

    if not removed:
        return f"Claude Code: nothing registered in {settings_path}"
    if not hooks:
        document.pop("hooks")
    write_json(settings_path, document)
    return f"Claude Code: removed {removed} hook(s) from {settings_path}"


def install_copilot(hooks_dir: Path, script: Path) -> str:
    """Register the `Stop` event, and only that one.

    Copilot fires `Stop` and its `agentStop` alias for the same turn, so a file
    naming both would announce every turn twice.

    The instruction hook has no counterpart here. Copilot CLI documents its hook
    events as running shell commands and documents none of them as putting
    anything into the model's context, so there is nothing to register it under.
    Its announcement reads a voice note exactly as Claude Code's does -- the
    extraction is in the shared script -- once the person has placed the
    instruction in `AGENTS.md` themselves.
    """
    target = hooks_dir / COPILOT_HOOK_FILE
    document = {
        "version": 1,
        "hooks": {
            "Stop": [
                {
                    "type": "command",
                    "bash": f"python3 '{script}'",
                    "timeoutSec": TIMEOUT_SECONDS,
                }
            ]
        },
    }
    write_json(target, document)
    return f"Copilot CLI: registered in {target}"


def remove_copilot(hooks_dir: Path) -> str:
    target = hooks_dir / COPILOT_HOOK_FILE
    if not target.exists():
        return f"Copilot CLI: nothing registered in {target}"
    target.unlink()
    return f"Copilot CLI: removed {target}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", required=True, help="Path of the installed announcement hook.")
    parser.add_argument(
        "--instruction-script",
        default=None,
        help="Path of the installed instruction hook. Registered under SessionStart for Claude Code.",
    )
    parser.add_argument(
        "--agents",
        default="claude,copilot",
        help="Comma-separated: claude, copilot, or both. Default: both.",
    )
    parser.add_argument("--remove", action="store_true", help="Unregister rather than register.")
    parser.add_argument("--claude-settings", default=None, help="Override the settings.json path.")
    parser.add_argument("--copilot-hooks-dir", default=None, help="Override the hooks directory.")
    arguments = parser.parse_args(argv)

    names = {name.strip() for name in arguments.agents.split(",") if name.strip()}
    if "both" in names:
        names = {"claude", "copilot"}
    unknown = names - {"claude", "copilot"}
    if unknown:
        parser.error(f"Unknown agent(s): {', '.join(sorted(unknown))}")

    script = Path(arguments.script).expanduser()
    if not arguments.remove and not script.is_file():
        raise SystemExit(f"No announcement hook at {script}.")

    instruction = None
    if arguments.instruction_script:
        instruction = Path(arguments.instruction_script).expanduser()
        if not arguments.remove and not instruction.is_file():
            raise SystemExit(f"No instruction hook at {instruction}.")

    settings = Path(arguments.claude_settings) if arguments.claude_settings else claude_settings_path()
    hooks_dir = Path(arguments.copilot_hooks_dir) if arguments.copilot_hooks_dir else copilot_hooks_dir()

    messages = []
    if "claude" in names:
        messages.append(
            remove_claude(settings)
            if arguments.remove
            else install_claude(settings, script, instruction)
        )
    if "copilot" in names:
        messages.append(remove_copilot(hooks_dir) if arguments.remove else install_copilot(hooks_dir, script))

    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
