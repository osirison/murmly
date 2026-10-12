"""A stand-in for the daemon's command socket, for the app's Home tests.

It is the `send(command)` callable `HomeModel` takes, so a test sets the state
the daemon would report and reads back every request, with when it arrived.
"""

from __future__ import annotations

import threading
import time


class FakeChannel:
    """Answers `status`, `last_words` and `clear_last_words` from fields a test sets."""

    def __init__(self) -> None:
        self.state = "IDLE"
        self.hotkeys_held: list[str] = []
        self.last_words: dict | None = None
        # None answers; an exception instance is raised, as an unreachable socket does.
        self.failure: Exception | None = None
        self.requests: list[tuple[float, str]] = []
        # Which threads asked, so a test can show the GUI thread never did.
        self.threads: set[int] = set()
        self._lock = threading.Lock()

    def __call__(self, command: str) -> dict:
        with self._lock:
            self.requests.append((time.monotonic(), command))
            self.threads.add(threading.get_ident())
        if self.failure is not None:
            raise self.failure
        if command == "status":
            return {
                "ok": True,
                "state": self.state,
                "restart_ready": self.state == "IDLE",
                "hotkeys_held": list(self.hotkeys_held),
            }
        if command == "last_words":
            return {"ok": True, "last_words": dict(self.last_words) if self.last_words else None}
        if command == "clear_last_words":
            self.last_words = None
            return {"ok": True}
        return {"ok": False, "error": f"unsupported: {command}"}

    def count(self, command: str) -> int:
        with self._lock:
            return sum(1 for _, name in self.requests if name == command)


def refused(text: str = "Move the review to Thursday.", detail: str = "Transcript copied to the clipboard but not pasted.") -> dict:
    return {"text": text, "produced_at": "2026-10-12T10:00:00+00:00", "delivered": False, "detail": detail}


def delivered(text: str = "Move the review to Thursday.") -> dict:
    return {"text": text, "produced_at": "2026-10-12T10:00:00+00:00", "delivered": True}
