---
title: Test a Stop hook across two turns, never a single claude -p run
description: A one-turn print-mode run removes the previous turn, which is exactly what a hook reading stale conversation state needs in order to look correct
trigger: claude -p, claude --settings, claude --resume, testing a Stop hook, hooks/murmly-announce.py

depends_on: hooks/murmly-announce.py, hooks/install_hooks.py
recorded: 2026-08-28
---

# Test a Stop hook across two turns, never a single `claude -p` run

**Symptom:** a `Stop` hook behaves one way under `claude -p` and another way in a
real session, and the print-mode result is the reassuring one. Murmly's
announcement hook logged "nothing worth saying" under `-p`, which reads as an
empty-transcript edge case. In a real session the same code announced the
*previous* turn's message, every turn, out loud.

**Why one turn hides it:** the transcript does not contain the finished turn's
message when `Stop` fires. With one turn there is nothing behind it, so a hook
reading the transcript finds nothing and goes quiet -- an absence, easy to file as
a quirk of print mode. With two turns it finds the turn before and announces it
confidently. The bug needs a previous turn to be visible at all, and a single-turn
harness is defined by not having one.

**Fix:** drive two turns in one session and assert on the second.

```bash
SID=$(claude --settings "$CFG" --output-format json -p 'Say ALPHA' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
claude --settings "$CFG" --resume "$SID" -p 'Say BRAVO'
```

Distinct markers per turn are what make the failure legible: the probe either
reports BRAVO, or it reports ALPHA and names the bug for you.

**Two things the probe itself needs.** Register it **without** `async` -- an async
hook is torn down when `-p` exits and may never run. And have it dump what it
actually received rather than what it concluded: the row count, the transcript's
last message, and the payload fields side by side. The payload turned out to carry
the correct message all along, which is only visible if the probe prints both.

**Use `claude --settings <file>`** to register the probe. It leaves
`~/.claude/settings.json` alone, so a test cannot disturb the real registration,
and it composes with `--resume`.

**Why it was not obvious:** print mode is the obvious harness -- scriptable,
isolated, no TUI to drive. Its isolation is the problem. Whenever a harness
simplifies the world, ask which failure that simplification puts out of reach;
here it was every fault that depends on conversation history existing.
