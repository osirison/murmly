"""The Qt side of Home, Health and Settings: timers, worker threads and the `view` QML reads.

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

from murmly.app import health, home, keys, removal, settings
from murmly.config import default_config_path, load_config
from murmly.hotkey_record import HotkeyRecordStore, default_hotkey_record_path
from murmly.settings_apply import RestartWhenIdle

logger = logging.getLogger(__name__)

# How often the timer asks the poller. The poller enforces the 500 ms; this
# only sets how soon after it elapses a request goes out.
TICK_MS = 100

# How often Settings asks whether a restart can happen yet, and (every second
# tick) whether the file was edited by hand. The restart itself holds to 500 ms.
SETTINGS_TICK_MS = 500


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
    # Polling started or stopped: some surface became visible, or none is left.
    pollingChanged = Signal()
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
        self.pollingChanged.emit()

    @property
    def polling(self) -> bool:
        """True while a window or the tray panel is visible, so the view is being kept current."""
        return self._poller.visible

    # -- what a person can do ----------------------------------------------

    @Slot()
    def copyAgain(self) -> None:
        words = self._snapshot.last_words
        if words is not None:
            self._copy_text(words.text)

    @Slot()
    def setupChanged(self) -> None:
        """The keys were changed: read the hotkey record again on the next refresh."""
        self._model.reload()

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


def default_settings_model() -> settings.SettingsModel:
    """The model that reads and writes the configuration file Murmly is using."""
    return settings.SettingsModel()


class SettingsController(QObject):
    """QML's `settings`: the page's `view`, and what a person can change.

    Every read and write of the file, and every restart of the service, runs on
    one worker thread, so a change and the restart it asks for never overlap and
    the window never waits on the disk, the daemon or the service manager. The
    timer only submits work: it asks the restart whether the daemon is idle yet
    while one is wanted, and looks for a hand edit of the file while Settings is
    the page being shown.

    A change made while the daemon is busy waits: any number of them make one
    restart, once nothing is being captured, transcribed or spoken.
    """

    viewChanged = Signal()
    _resultReady = Signal(str, object)

    def __init__(
        self,
        model: settings.SettingsModel,
        restart_factory: Callable[[], RestartWhenIdle] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        # Built on first use, on the worker: it needs the daemon's socket path.
        self._restart_factory = (
            restart_factory
            if restart_factory is not None
            else lambda: RestartWhenIdle(load_config(model.path))
        )
        self._restart: RestartWhenIdle | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="murmly-settings")
        self._closed = False
        self._frozen = False
        self._surfaces: set[str] = set()
        self._waiting = False
        self._background_busy = False
        self._ticks = 0
        self._state: settings.SettingsState | None = None
        self._notice = ""
        self._outcome: settings.SaveOutcome | None = None
        self._restart_text = ""
        self._pending = settings.PendingChanges()
        self._view: dict = settings.build_view(None)
        self._timer = QTimer(self)
        self._timer.setInterval(SETTINGS_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._resultReady.connect(self._on_result)

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @property
    def executor(self) -> ThreadPoolExecutor:
        """The one worker. Binding a key shares it, so a bind (which installs and
        starts the service) and a pending restart never run at the same time."""
        return self._executor

    @Slot(bool)
    def setActive(self, active: bool) -> None:
        """Settings is, or is no longer, the page a person is looking at.

        Opening it reads the file again, and while it is open a hand edit of the
        file is noticed within about a second.
        """
        self.setSurfaceActive("window", active)

    @Slot(str, bool)
    def setSurfaceActive(self, surface: str, active: bool) -> None:
        """A surface that shows these settings ("window" for the page, "tray" for the
        panel's switches) is, or is no longer, on screen.

        Each one showing reads the file again; while any is showing, a hand edit
        is noticed within about a second.
        """
        if self._closed or self._frozen:
            return
        if active == (surface in self._surfaces):
            return
        if active:
            self._surfaces.add(surface)
            self._submit("state", self._model.load)
            self._timer.start()
        else:
            self._surfaces.discard(surface)
            if not self._surfaces and not self._waiting:
                self._timer.stop()

    @property
    def _active(self) -> bool:
        return bool(self._surfaces)

    @Slot(str, "QVariant")
    def change(self, control_id: str, value: object) -> None:
        """A control was changed: write its key. The result comes back by `view`.

        The view shows the asked value at once, and until the save comes back a
        second change is worked out from it, not from the file's older value.
        """
        if self._frozen or self._closed:
            return
        self._pending.ask(control_id, value)
        self._submit("save", lambda: self._save(control_id, value), control_id=control_id)
        self._rebuild()

    @Slot()
    def freeze(self) -> None:
        """Write nothing and restart nothing from here on.

        Called when Murmly is being removed: a change saved, or a restart still
        waiting for the daemon to be idle, would otherwise land after the removal
        and recreate the file or start the service that was just taken away.
        Work already queued behind it on the worker still runs first.
        """
        self._frozen = True
        self._waiting = False
        self._timer.stop()

    @Slot()
    def thaw(self) -> None:
        """Settings may be changed again: a removal that was stopped did not remove them."""
        self._frozen = False
        if self._active:
            self._timer.start()

    def shutdown(self) -> None:
        """Stop the timer and let the worker finish. Safe to call twice."""
        self._closed = True
        self._timer.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)

    # -- worker thread -------------------------------------------------------

    def _save(self, control_id: str, value: object) -> tuple:
        outcome, state = self._model.apply(control_id, value)
        restart_error = ""
        if outcome.saved:
            try:
                if self._restart is None:
                    self._restart = self._restart_factory()
                self._restart.request()
            except Exception as error:  # noqa: BLE001 - shown beside the saved setting
                restart_error = str(error) or type(error).__name__
        return outcome, state, restart_error

    def _tick_restart(self) -> object:
        return self._restart.tick() if self._restart is not None else None

    # -- plumbing ---------------------------------------------------------------

    def _tick(self) -> None:
        if self._closed or self._frozen or self._background_busy:
            return
        self._ticks += 1
        if self._waiting:
            self._background_busy = True
            self._submit("restart", self._tick_restart, background=True)
        elif self._active and self._ticks % 2 == 0:
            self._background_busy = True
            self._submit("poll", self._model.poll, background=True)

    def _submit(
        self,
        kind: str,
        work: Callable[[], object],
        control_id: str = "",
        background: bool = False,
    ) -> None:
        if self._closed:
            return
        try:
            future = self._executor.submit(work)
        except RuntimeError:  # the executor is shut down
            if background:
                self._background_busy = False
            return
        future.add_done_callback(lambda done: self._finished(kind, control_id, done))

    def _finished(self, kind: str, control_id: str, future: Future) -> None:
        # Worker thread: only hand the outcome to the GUI thread.
        try:
            outcome: object = future.result()
        except Exception as error:  # noqa: BLE001 - shown as the page's message
            logger.warning("Settings request failed: %s", type(error).__name__)
            text = str(error) or type(error).__name__
            if kind == "save":
                outcome = (settings.SaveOutcome("failed", message=text), None, "")
            elif kind == "restart":
                outcome = text
            else:
                outcome = None
        if kind == "save":
            outcome = (*outcome, control_id)  # type: ignore[misc]
        if not self._closed:
            self._resultReady.emit(kind, outcome)

    @Slot(str, object)
    def _on_result(self, kind: str, outcome: object) -> None:
        if self._closed:
            return
        if kind == "state":
            self._take_state(outcome)  # type: ignore[arg-type]
        elif kind == "poll":
            self._background_busy = False
            if outcome is None:
                return
            self._take_state(outcome)  # type: ignore[arg-type]
        elif kind == "restart":
            self._background_busy = False
            if not self._take_restart(outcome):
                return  # still waiting: nothing the page shows has changed
        else:
            self._take_save(*outcome)  # type: ignore[misc]
        self._rebuild()

    def _take_state(self, state: settings.SettingsState) -> None:
        self._state = state
        self._notice = settings.NOTICE_FILE_CHANGED if state.changed else self._notice

    def _take_save(
        self, outcome: settings.SaveOutcome, state, restart_error: str, control_id: str
    ) -> None:
        self._pending.settled(control_id)
        self._outcome = outcome
        if state is not None:
            self._state = state
        changed = outcome.file_changed or (state is not None and state.changed)
        self._notice = settings.NOTICE_FILE_CHANGED if changed else ""
        if not outcome.saved:
            return
        if restart_error:
            self._restart_text = restart_error
            return
        self._waiting = True
        self._restart_text = "waiting"
        self._timer.start()

    def _take_restart(self, outcome: object) -> bool:
        """Take the restart attempt's outcome; False when it leaves the page as it was."""
        if isinstance(outcome, str):
            text: str | None = outcome
        elif outcome is None:
            text = None
        else:
            text = settings.restart_status(outcome)  # type: ignore[arg-type]
        if text is None:
            self._waiting = False
        elif text == "waiting":
            return False
        else:
            self._waiting = False
            self._restart_text = text
        if not self._waiting and not self._active:
            self._timer.stop()
        return True

    def _rebuild(self) -> None:
        # Always announced, even when nothing about the view differs: a refused value
        # changes no file value, and the control that was moved must snap back to it.
        self._view = self._pending.overlay(
            settings.build_view(
                self._state,
                notice=self._notice,
                outcome=self._outcome,
                restart=self._restart_text,
            )
        )
        self.viewChanged.emit()


def default_keys_model() -> keys.KeysModel:
    """The model that reads the real hotkey record and binds through the real installer."""
    return keys.KeysModel(HotkeyRecordStore(default_hotkey_record_path()).read)


class KeysController(QObject):
    """QML's `keys`: the key fields' `view`, and the recorder behind them.

    The window reports the focus of a field (`fieldFocusChanged`) and each key
    pressed in it (`keyPressed`); `keys.Recorder` decides whether to read it. A
    combination that passes is bound by the install path on the worker thread,
    shared with Settings' when one is given so the two never overlap, and the
    outcome comes back by signal. Nothing here listens outside the window.
    """

    viewChanged = Signal()
    #: A key was saved: Home reads the keys again.
    keysChanged = Signal()
    _resultReady = Signal(str, object)

    def __init__(
        self,
        model: keys.KeysModel,
        executor: ThreadPoolExecutor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._owns_executor = executor is None
        self._executor = (
            executor if executor is not None else ThreadPoolExecutor(max_workers=1, thread_name_prefix="murmly-keys")
        )
        self._recorder = keys.Recorder(model.profile)
        self._state = keys.KeysState()
        #: Keys saved but not yet confirmed by the desktop: not in the record yet.
        self._unconfirmed: dict[str, str] = {}
        self._closed = False
        self._view: dict = keys.keys_view(self._state, "")
        self._resultReady.connect(self._on_result)
        self.reload()

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @Slot()
    def reload(self) -> None:
        """Read the hotkey record again."""
        self._submit("record", self._model.read)

    @Slot(str)
    def startRecording(self, purpose: str) -> None:
        """The person asked to record `purpose`'s key. Only a click or Enter calls this."""
        if self._closed or self._state.pending:
            return
        self._recorder.start(purpose)
        self._state.hints.pop(purpose, None)
        self._state.note = {}
        self._rebuild()

    @Slot()
    def stopRecording(self) -> None:
        self._recorder.stop()
        self._rebuild()

    @Slot(str, bool)
    def fieldFocusChanged(self, purpose: str, focused: bool) -> None:
        before = self._recorder.recording
        self._recorder.focus_changed(purpose, focused)
        if self._recorder.recording != before:
            self._rebuild()

    @Slot(str, int, int, result=bool)
    def keyPressed(self, purpose: str, key: int, modifiers: int) -> bool:
        """A key was pressed in the field for `purpose`. True when the field used it."""
        if self._closed:
            return False
        press = self._recorder.press(purpose, key, modifiers)
        if press.kind == "refused":
            self._state.hints[purpose] = press.message
            self._rebuild()
        elif press.kind == "cancelled":
            self._state.hints.pop(purpose, None)
            self._rebuild()
        elif press.kind == "combo":
            self._state.hints.pop(purpose, None)
            self._bind(purpose, press.text)
        return press.accepted

    @Slot(str, str)
    def pick(self, purpose: str, text: str) -> None:
        """A suggested combination was chosen: bound through the same path."""
        if self._closed or self._state.pending:
            return
        self._recorder.stop()
        self._bind(purpose, text)

    def shutdown(self) -> None:
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)

    # -- plumbing ---------------------------------------------------------------

    def _bind(self, purpose: str, text: str) -> None:
        self._state.pending[purpose] = text
        self._state.note = {}
        self._rebuild()
        record = dict(self._state.record)
        self._submit("bind", lambda: self._model.bind(purpose, text, record), purpose=purpose)

    def _submit(self, kind: str, work: Callable[[], object], purpose: str = "") -> None:
        if self._closed:
            return
        try:
            future = self._executor.submit(work)
        except RuntimeError:  # the executor is shut down
            return
        future.add_done_callback(lambda done: self._finished(kind, purpose, done))

    def _finished(self, kind: str, purpose: str, future: Future) -> None:
        # Worker thread: only hand the outcome to the GUI thread.
        try:
            outcome: object = future.result()
        except Exception as error:  # noqa: BLE001 - shown as the reason the keys were not set
            logger.warning("Key request failed: %s", type(error).__name__)
            if kind == "bind":
                outcome = keys.BindResult("failed", purpose, "", str(error) or type(error).__name__)
            else:
                outcome = None
        if not self._closed:
            self._resultReady.emit(kind, outcome)

    @Slot(str, object)
    def _on_result(self, kind: str, outcome: object) -> None:
        if self._closed:
            return
        if kind == "record":
            if outcome is not None:
                record = dict(outcome)  # type: ignore[call-overload]
                for purpose, text in self._unconfirmed.items():
                    record.setdefault(purpose, text)
                self._state.record = record
            self._state.loaded = True
        else:
            result: keys.BindResult = outcome  # type: ignore[assignment]
            self._state.pending.pop(result.purpose, None)
            self._state.note = {
                "purpose": result.purpose,
                "status": result.status,
                "tone": result.tone,
                "text": result.message,
                "detail": result.detail,
            }
            if result.saved:
                self._state.record[result.purpose] = result.text
                if result.status == "unconfirmed":
                    self._unconfirmed[result.purpose] = result.text
                else:
                    self._unconfirmed.pop(result.purpose, None)
                self.keysChanged.emit()
                self.reload()
        self._rebuild()

    def _rebuild(self) -> None:
        self._view = keys.keys_view(self._state, self._recorder.recording)
        self.viewChanged.emit()


def default_removal_model() -> removal.RemovalModel:
    """The model that reads what is installed and removes it through `murmly uninstall`'s own code."""
    return removal.RemovalModel()


class RemovalController(QObject):
    """QML's `removal`: the confirmation's `view`, and the clicks that drive it.

    Asking to remove only reads what is installed. The removal runs after the
    person confirms, once, on the worker thread (shared with Settings' and the
    keys', so it never overlaps a restart or a bind), and the window never waits
    on it. When it finishes the dialog reports what was removed and, on the
    person's click, `quit_app` ends the app: its own service and menu entry are
    gone. Quitting touches no file Murmly keeps, so it cannot fail because they
    vanished.
    """

    viewChanged = Signal()
    _resultReady = Signal(str, object)

    def __init__(
        self,
        model: removal.RemovalModel,
        executor: ThreadPoolExecutor,
        quit_app: Callable[[], None],
        before_removal: Callable[[], None] = lambda: None,
        removal_failed: Callable[[], None] = lambda: None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._executor = executor
        self._quit_app = quit_app
        self._before_removal = before_removal
        self._removal_failed = removal_failed
        self._state = removal.RemovalState()
        self._closed = False
        self._view: dict = removal.removal_view(self._state)
        self._resultReady.connect(self._on_result)

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @Slot()
    def request(self) -> None:
        """"Remove murmly…" was clicked: read what is installed. Removes nothing."""
        if self._closed or not self._state.request():
            return
        self._rebuild()
        self._submit("plan", self._model.plan)

    @Slot(bool)
    def setPurge(self, purge: bool) -> None:
        """The person's choice to also delete settings and models. Starts off."""
        if self._closed:
            return
        self._state.set_purge(purge)
        self._rebuild()

    @Slot()
    def cancel(self) -> None:
        if self._closed:
            return
        self._state.cancel()
        self._rebuild()

    @Slot()
    def confirm(self) -> None:
        """The person confirmed what the dialog listed. The only call that removes anything."""
        if self._closed or not self._state.confirm():
            return
        purge = self._state.purge
        self._rebuild()
        try:
            self._before_removal()
        except Exception:  # noqa: BLE001 - freezing Settings must never stop a removal the person confirmed
            logger.warning("Settings could not be paused before removal.", exc_info=True)
        self._submit("remove", lambda: self._model.remove(purge))

    @Slot()
    def finish(self) -> None:
        """Close the app, once it has reported a finished removal."""
        if self._closed or not self._state.finished:
            return
        try:
            self._quit_app()
        except Exception:  # noqa: BLE001 - the app is going away either way
            logger.warning("The app could not quit cleanly.", exc_info=True)

    def shutdown(self) -> None:
        self._closed = True

    # -- plumbing ---------------------------------------------------------------

    def _submit(self, kind: str, work: Callable[[], object]) -> None:
        try:
            future = self._executor.submit(work)
        except RuntimeError:  # the executor is shut down
            return
        future.add_done_callback(lambda done: self._finished(kind, done))

    def _finished(self, kind: str, future: Future) -> None:
        # Worker thread: only hand the outcome to the GUI thread.
        try:
            outcome: tuple = (future.result(), "")
        except Exception as error:  # noqa: BLE001 - shown as the reason removal did not happen
            logger.warning("Removal request failed: %s", type(error).__name__)
            outcome = (None, str(error) or type(error).__name__)
        if not self._closed:
            self._resultReady.emit(kind, outcome)

    @Slot(str, object)
    def _on_result(self, kind: str, outcome: object) -> None:
        if self._closed:
            return
        value, error = outcome  # type: ignore[misc]
        if kind == "plan":
            self._state.planned(value, error)
        elif error:
            self._state.failed(error)
            if self._state.step == removal.FAILED:
                try:
                    self._removal_failed()
                except Exception:  # noqa: BLE001
                    logger.warning("Settings could not be resumed after a failed removal.", exc_info=True)
        else:
            self._state.removed(value)
            if self._state.step == removal.PARTIAL:
                # The app stays open, so Settings must work again.
                try:
                    self._removal_failed()
                except Exception:  # noqa: BLE001
                    logger.warning("Settings could not be resumed after a partial removal.", exc_info=True)
        self._rebuild()

    def _rebuild(self) -> None:
        self._view = removal.removal_view(self._state)
        self.viewChanged.emit()


class FirstRunController(QObject):
    """QML's `firstRun`: whether first run is showing, and which step.

    Decided once, when the app starts, from whether a dictation key is bound. It
    changes only when the person acts, never because of anything the daemon says,
    so it can never raise the window or move the focus.
    """

    viewChanged = Signal()

    def __init__(self, first_run: keys.FirstRun | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._first_run = first_run if first_run is not None else keys.FirstRun()
        self._view = self._first_run.view()

    @Property("QVariantMap", notify=viewChanged)
    def view(self) -> dict:
        return self._view

    @Slot()
    def open(self) -> None:
        self._first_run.open()
        self._rebuild()

    @Slot()
    def next(self) -> None:
        self._first_run.next()
        self._rebuild()

    @Slot()
    def back(self) -> None:
        self._first_run.back()
        self._rebuild()

    @Slot()
    def later(self) -> None:
        self._first_run.later()
        self._rebuild()

    def _rebuild(self) -> None:
        self._view = self._first_run.view()
        self.viewChanged.emit()
