#!/usr/bin/env python3
"""Register Murmly's announcement hook with Claude Code and GitHub Copilot CLI.

The two agents keep hook configuration in different places and in different
shapes, and only one of them offers a drop-in directory:

  Claude Code   ~/.claude/settings.json, under `hooks.Stop`. There is nowhere
                else to put it, so this merges into a file full of settings that
                are none of Murmly's business. Every write is backed up first,
                and only entries Murmly recognises as its own are touched.

  Copilot CLI   ~/.copilot/hooks/murmly-announce.json, a file of its own.
                Installing is writing it and removing is deleting it, so there
                is no merge to get wrong.

Both agents send the same `Stop` payload, so both get the same script.

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
MARKER = "murmly-announce"

COPILOT_HOOK_FILE = "murmly-announce.json"
TIMEOUT_SECONDS = 15


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
        if isinstance(value, str) and MARKER in value:
            return True
    return False


def strip_murmly(stop_groups: list) -> tuple[list, int]:
    """Every Murmly hook out of a Claude `Stop` list, and how many there were.

    Groups are rebuilt rather than filtered in place so that a group left with
    no hooks disappears with them, instead of accumulating as an empty matcher
    every time this runs.
    """
    kept: list = []
    removed = 0
    for group in stop_groups:
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


def install_claude(settings_path: Path, script: Path) -> str:
    document = read_json(settings_path)
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"Refusing to touch {settings_path}: its `hooks` is not an object.")

    stop = hooks.get("Stop")
    stop = stop if isinstance(stop, list) else []
    stop, replaced = strip_murmly(stop)
    stop.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": f"python3 '{script}'",
                    "timeout": TIMEOUT_SECONDS,
                    "async": True,
                    "statusMessage": "Announcing through Murmly",
                }
            ]
        }
    )
    hooks["Stop"] = stop
    write_json(settings_path, document)
    if replaced:
        return f"Claude Code: registered in {settings_path} (replaced {replaced} earlier Murmly hook(s))"
    return f"Claude Code: registered in {settings_path}"


def remove_claude(settings_path: Path) -> str:
    document = read_json(settings_path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("Stop"), list):
        return f"Claude Code: nothing registered in {settings_path}"

    stop, removed = strip_murmly(hooks["Stop"])
    if not removed:
        return f"Claude Code: nothing registered in {settings_path}"
    if stop:
        hooks["Stop"] = stop
    else:
        hooks.pop("Stop")
    if not hooks:
        document.pop("hooks")
    write_json(settings_path, document)
    return f"Claude Code: removed {removed} hook(s) from {settings_path}"


def install_copilot(hooks_dir: Path, script: Path) -> str:
    """Register the `Stop` event, and only that one.

    Copilot fires `Stop` and its `agentStop` alias for the same turn, so a file
    naming both would announce every turn twice.
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

    settings = Path(arguments.claude_settings) if arguments.claude_settings else claude_settings_path()
    hooks_dir = Path(arguments.copilot_hooks_dir) if arguments.copilot_hooks_dir else copilot_hooks_dir()

    messages = []
    if "claude" in names:
        messages.append(remove_claude(settings) if arguments.remove else install_claude(settings, script))
    if "copilot" in names:
        messages.append(remove_copilot(hooks_dir) if arguments.remove else install_copilot(hooks_dir, script))

    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
