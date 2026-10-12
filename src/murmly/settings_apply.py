from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import time

from murmly.config import MurmlyConfig
from murmly.daemon import COMMAND_STATUS, DaemonNotRespondingError, STATE_IDLE, send_command
from murmly.installer import InstallError, UserService
from murmly.platform import SERVICE_MANAGEMENT, PlatformProfile, resolve_platform

#: The least time between two `status` requests while waiting, matching how often
#: the app's screens poll.
MIN_POLL_INTERVAL_SECONDS = 0.5


class RestartStatus(StrEnum):
    #: Nothing was waiting to be applied.
    NOTHING_PENDING = "nothing-pending"
    #: A restart is wanted and the daemon is busy, or it is too soon to ask again.
    WAITING = "waiting"
    #: The service was restarted.
    RESTARTED = "restarted"
    #: The service could not be restarted; `detail` says why.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RestartOutcome:
    status: RestartStatus
    detail: str = ""


def default_restart(profile: PlatformProfile | None = None) -> Callable[[], bool]:
    """The installer's service restart for this platform: the systemd user unit,
    the launchd agent or the Windows scheduled task."""
    choice = SERVICE_MANAGEMENT.select(profile if profile is not None else resolve_platform())
    service = choice.load()() if choice.load is not None else UserService()
    return service.restart


class RestartWhenIdle:
    """Restarts the service to apply saved settings, but only once nothing is
    being captured, transcribed or spoken.

    The app calls `request` after every save and `tick` from its timer. Saves
    made while the daemon is busy collapse into one pending restart, so one idle
    moment gives one restart. `tick` asks the daemon no more often than every
    `MIN_POLL_INTERVAL_SECONDS`, and never restarts while the daemon says it is
    busy or does not answer.

    Two cases where the daemon does not say `restart_ready`:

    - Nothing is listening on the command channel. Nothing can be interrupted and
      the new settings are wanted, so the service is restarted, which also starts
      it when it is stopped.
    - The daemon is too old to send `restart_ready`. It counts as idle only when
      its `state` is `IDLE`.

    A daemon that is there but does not answer in time is treated as busy, not as
    absent: it took the request, so it may be in the middle of a transcription.

    `status`, `restart`, `clock` and `sleep` are injectable so tests need no
    sockets, services or real time. `status` returns the daemon's `status`
    response and raises what `send_command` raises.
    """

    def __init__(
        self,
        config: MurmlyConfig | None = None,
        *,
        status: Callable[[], dict[str, object]] | None = None,
        restart: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = MIN_POLL_INTERVAL_SECONDS,
    ) -> None:
        if status is None:
            if config is None:
                raise ValueError("A config or a status function is needed to ask the daemon.")
            socket_path = str(config.socket_path)
            status = lambda: send_command(socket_path, COMMAND_STATUS)  # noqa: E731
        self._status = status
        self._restart = restart if restart is not None else default_restart()
        self._clock = clock
        self._sleep = sleep
        self._poll_interval = max(poll_interval, MIN_POLL_INTERVAL_SECONDS)
        self._pending = False
        self._last_asked: float | None = None

    @property
    def pending(self) -> bool:
        return self._pending

    def request(self) -> None:
        """Note that a saved setting needs a restart. Asking again changes nothing."""
        self._pending = True

    def tick(self) -> RestartOutcome:
        """Restart now if one is wanted and the daemon is idle; otherwise say so."""
        if not self._pending:
            return RestartOutcome(RestartStatus.NOTHING_PENDING)
        now = self._clock()
        if self._last_asked is not None and now - self._last_asked < self._poll_interval:
            return RestartOutcome(RestartStatus.WAITING)
        self._last_asked = now
        if not self._may_restart():
            return RestartOutcome(RestartStatus.WAITING)
        self._pending = False
        self._last_asked = None
        try:
            restarted = self._restart()
        except InstallError as error:
            return RestartOutcome(RestartStatus.FAILED, str(error))
        if not restarted:
            return RestartOutcome(RestartStatus.FAILED, "The service did not restart.")
        return RestartOutcome(RestartStatus.RESTARTED)

    def run(self, timeout: float | None = None) -> RestartOutcome:
        """Block, polling, until the pending restart is done or `timeout` passes."""
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            outcome = self.tick()
            if outcome.status is not RestartStatus.WAITING:
                return outcome
            if deadline is not None and self._clock() >= deadline:
                return outcome
            self._sleep(self._poll_interval)

    def _may_restart(self) -> bool:
        try:
            response = self._status()
        except DaemonNotRespondingError:
            return False
        except (FileNotFoundError, ConnectionRefusedError):
            return True
        except OSError:
            # Neither absent nor refused (a reset, a timeout): not known to be
            # idle, so not restarted.
            return False
        ready = response.get("restart_ready")
        if isinstance(ready, bool):
            return ready
        return response.get("state") == STATE_IDLE
