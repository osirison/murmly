"""The Qt side of Home: a timer, a worker thread and the `view` QML reads.

The logic is in `home.py`; this only wires it to Qt. A QTimer asks the poller
whether a request is due, `HomeModel.refresh` runs on one worker thread so a
slow daemon never freezes the window, and the answer comes back by signal.

Nothing here raises, activates or focuses a window, whatever the daemon says.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication

from murmly.app import health, home
from murmly.config import default_config_path, load_config
from murmly.hotkey_record import HotkeyRecordStore, default_hotkey_record_path

logger = logging.getLogger(__name__)

# How often the timer asks the poller. The poller enforces the 500 ms; this
# only sets how soon after it elapses a request goes out.
TICK_MS = 100


def default_model() -> home.HomeModel:
    """The model for the real daemon: its socket, the config and the hotkey record."""
    config = load_config()
    store = HotkeyRecordStore(default_hotkey_record_path())
    return home.HomeModel(
        home.default_send(str(config.socket_path)),
        config_loader=lambda: load_config(default_config_path()),
        key_reader=store.read,
    )


def copy_to_clipboard(text: str) -> None:
    QGuiApplication.clipboard().setText(text)


class HomeController(QObject):
    """QML's `home`: the view, and the few things a person can do from Home."""

    viewChanged = Signal()
    # Emitted on the worker thread, received on the GUI thread (a queued call,
    # because the receiving slot belongs to this object).
    _resultReady = Signal(str, object)

    def __init__(
        self,
        model: home.HomeModel,
        poller: home.Poller | None = None,
        copy_text: Callable[[str], None] = copy_to_clipboard,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._poller = poller if poller is not None else home.Poller()
        self._copy_text = copy_text
        self._clock = clock
        self._now = now
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="murmly-home")
        self._closed = False
        self._snapshot = home.Snapshot(reachable=True)
        self._seen_snapshot = False
        self._dismissed: str | None = None
        self._starting_until: float | None = None
        self._start_error = ""
        self._view: dict = {}
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._resultReady.connect(self._on_result)

    # -- what QML reads -----------------------------------------------------

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    # -- what is visible ----------------------------------------------------

    @Slot(str, bool)
    def setSurfaceVisible(self, surface: str, visible: bool) -> None:
        """The window ("window") or the tray panel ("tray") showing or hiding.

        Polling runs while any surface is visible and stops when none is.
        """
        if self._closed:
            return
        changed = self._poller.set_visible(surface, visible)
        if not changed:
            return
        if self._poller.visible:
            self._model.reload()
            self._timer.start()
            self._tick()
        else:
            self._timer.stop()

    # -- what a person can do ----------------------------------------------

    @Slot()
    def copyAgain(self) -> None:
        words = self._snapshot.last_words
        if words is not None:
            self._copy_text(words.text)

    @Slot()
    def clearLastWords(self) -> None:
        self._submit("snapshot", self._model.clear_last_words)

    @Slot()
    def dismissRefusal(self) -> None:
        """Acknowledge the refused delivery. The words stay in Last words."""
        words = self._snapshot.last_words
        if words is not None:
            self._dismissed = words.produced_at
            self._rebuild()

    @Slot()
    def startService(self) -> None:
        self._start_error = ""
        self._starting_until = self._clock() + home.START_GRACE_S
        self._rebuild()
        self._submit("start", self._model.start_service)

    def shutdown(self) -> None:
        """Stop polling and let the worker finish. Safe to call twice."""
        self._closed = True
        self._timer.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)

    # -- plumbing -------------------------------------------------------------

    def _tick(self) -> None:
        if self._closed or not self._poller.begin():
            return
        self._submit("snapshot", self._model.refresh, polled=True)

    def _submit(self, kind: str, work: Callable[[], object], polled: bool = False) -> None:
        if self._closed:
            return
        try:
            future = self._executor.submit(work)
        except RuntimeError:  # the executor is shut down
            if polled:
                self._poller.finish()
            return
        future.add_done_callback(lambda done, kind=kind, polled=polled: self._finished(kind, polled, done))

    def _finished(self, kind: str, polled: bool, future: Future) -> None:
        # Worker thread: only hand the outcome to the GUI thread.
        try:
            outcome = future.result()
        except Exception as error:  # noqa: BLE001 - reported as an unreachable daemon
            # No transcript is in an exception message; the type is enough to log.
            logger.warning("Home request failed: %s", type(error).__name__)
            outcome = home.Snapshot(reachable=False, error=str(error))
            kind = "snapshot"
        if polled:
            outcome = (outcome, True)
        if not self._closed:
            self._resultReady.emit(kind, outcome)

    @Slot(str, object)
    def _on_result(self, kind: str, outcome: object) -> None:
        if self._closed:
            return
        if kind == "start":
            result: home.StartResult = outcome  # type: ignore[assignment]
            if not result.ok:
                self._starting_until = None
                self._start_error = result.detail
            self._rebuild()
            return
        polled = isinstance(outcome, tuple)
        snapshot = outcome[0] if polled else outcome
        if polled:
            self._poller.finish()
        # A late answer to a poll sent just before every surface hid is dropped.
        if polled and not self._poller.visible:
            return
        self._snapshot = snapshot
        self._seen_snapshot = True
        if snapshot.reachable:
            self._starting_until = None
            self._start_error = ""
        self._rebuild()

    def _rebuild(self) -> None:
        if not self._seen_snapshot:
            # Nothing has answered yet: the view stays empty rather than saying
            # "ready" or "not running" before it knows.
            return
        starting = self._starting_until is not None and self._clock() < self._starting_until
        view = home.build_view(
            self._snapshot,
            now=self._now(),
            dismissed=self._dismissed,
            starting=starting and not self._snapshot.reachable,
            start_error=self._start_error,
        )
        if view != self._view:
            self._view = view
            self.viewChanged.emit()


def default_health_model() -> health.HealthModel:
    """The model that runs the real `murmly doctor`."""
    return health.HealthModel()


class HealthController(QObject):
    """QML's `health`: the report, and what a person can do with it.

    Doctor runs in a child process on one worker thread, never on the GUI
    thread. It runs when Health becomes the page being shown and when asked to
    run again, and never on a timer. The text offered for copying is doctor's
    own output, kept exactly as printed.
    """

    viewChanged = Signal()
    _resultReady = Signal(object)

    def __init__(
        self,
        model: health.HealthModel,
        copy_text: Callable[[str], None] = copy_to_clipboard,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._copy_text = copy_text
        self._now = now
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="murmly-health")
        self._closed = False
        self._active = False
        self._checking = False
        self._result: health.DoctorResult | None = None
        self._checked_at: datetime | None = None
        self._tray_found: bool | None = None
        self._view: dict = health.build_view(None, checking=False, checked_at=None)
        self._resultReady.connect(self._on_result)

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        """Health is, or is no longer, the page a person is looking at."""
        if self._closed or active == self._active:
            return
        self._active = active
        if active:
            self.check()

    @Slot()
    def check(self) -> None:
        """Run the checks again. Ignored while a run is in progress."""
        if self._closed or self._checking:
            return
        try:
            future = self._executor.submit(self._model.check)
        except RuntimeError:  # the executor is shut down
            return
        self._checking = True
        self._rebuild()
        future.add_done_callback(self._finished)

    @Slot()
    def copyReport(self) -> None:
        """Copy the report as `murmly doctor` printed it."""
        if self._result is not None and self._result.report is not None:
            self._copy_text(self._result.text)

    def set_tray_found(self, found: bool | None) -> None:
        """What the tray code found: True, False, or None for "not looked yet".

        Health lists an app-side entry for it. It is not part of the copied report.
        """
        if found == self._tray_found:
            return
        self._tray_found = found
        self._rebuild()

    def shutdown(self) -> None:
        """Stop a run in progress and let the worker finish. Safe to call twice."""
        self._closed = True
        self._model.cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _finished(self, future: Future) -> None:
        # Worker thread: only hand the outcome to the GUI thread.
        try:
            outcome = future.result()
        except Exception as error:  # noqa: BLE001 - shown as a failed check
            logger.warning("Health check failed: %s", type(error).__name__)
            outcome = health.DoctorResult(error=str(error) or type(error).__name__)
        if not self._closed:
            self._resultReady.emit(outcome)

    @Slot(object)
    def _on_result(self, outcome: object) -> None:
        if self._closed:
            return
        self._checking = False
        self._result = outcome  # type: ignore[assignment]
        self._checked_at = self._now()
        self._rebuild()

    def _rebuild(self) -> None:
        view = health.build_view(
            self._result,
            checking=self._checking,
            checked_at=self._checked_at,
            tray_found=self._tray_found,
        )
        if view != self._view:
            self._view = view
            self.viewChanged.emit()
