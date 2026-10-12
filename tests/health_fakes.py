"""A stand-in for `murmly doctor`, for the app's Health tests.

It is the model `HealthController` takes: `check()` returns what a run produced
and `cancel()` stops one, so a test sets the answer and reads back which thread
asked, without ever starting a process or opening an audio device.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from murmly.app import health

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "doctor_report.json"


def recorded_text() -> str:
    """What `murmly doctor` printed on a real machine, home path and names scrubbed."""
    return FIXTURE.read_text(encoding="utf-8")


def recorded_report() -> dict:
    return json.loads(recorded_text())


def result_for(report: dict) -> health.DoctorResult:
    """A successful run printing `report` the way `murmly doctor` does."""
    return health.parse_doctor_output(json.dumps(report, indent=2) + "\n", "", 0)


class FakeHealthModel:
    def __init__(self, result: health.DoctorResult | None = None) -> None:
        self.result = result if result is not None else health.parse_doctor_output(recorded_text(), "", 0)
        self.checks = 0
        self.threads: set[int] = set()
        self.cancelled = False
        # Set to hold a run open until a test releases it.
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def check(self) -> health.DoctorResult:
        with self._lock:
            self.checks += 1
            self.threads.add(threading.get_ident())
        if self.gate is not None:
            self.gate.wait(10)
        return self.result

    def cancel(self) -> None:
        self.cancelled = True
        if self.gate is not None:
            self.gate.set()
