#!/usr/bin/env python3
"""Tell a coding agent that the end of its turn is spoken aloud.

A SessionStart hook for Claude Code. Claude Code adds a SessionStart hook's
plain-text stdout to the session's context, so printing the convention is all it
takes for the agent to know it. It is registered for every source -- startup,
resume, clear, compact, fork -- because without `compact` a long session loses
the convention at the moment its context is rebuilt, and its announcements would
quietly degrade to extracts partway through a day's work.

It must stay a constant. This is the one thing Murmly installs that a session
waits on, and it is allowed to only because it opens no socket, reads no
configuration, and starts no subprocess. A probe here -- asking the daemon
whether speech output is even enabled -- would put a connection attempt in front
of every session start, and opening a speech session to find out would take the
session away from an announcement still being spoken.

Registered without `async` for the same reason it exists: an async hook runs in
the background, so its stdout never reaches the context and it would instruct
nobody while appearing to succeed.

Exits 0 whatever happens. An agent that is not told writes no voice note, and
the announcement falls back to an extract of its message, which is what it did
before this existed.

Environment:
  MURMLY_ANNOUNCE_INSTRUCT  0 to say nothing, leaving announcements as extracts
"""

from __future__ import annotations

import os
import sys

INSTRUCTION = """\
Murmly speaks the end of your turn aloud, for a person who is not looking at the
terminal.

End each turn with one <voice-note> element holding what that person needs to
hear: two or three sentences of plain spoken English saying what you did,
whether it worked, and anything they have to decide. Lead with the outcome. No
file paths, no identifiers, no code, no markdown -- it is read out, not
displayed.

Everything outside the element is for the screen and is not spoken. Leave the
element empty to say nothing aloud for a turn that does not need announcing.
"""


def main() -> int:
    if os.environ.get("MURMLY_ANNOUNCE_INSTRUCT", "1") == "0":
        return 0
    sys.stdout.write(INSTRUCTION)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never fail a session start
        sys.exit(0)
