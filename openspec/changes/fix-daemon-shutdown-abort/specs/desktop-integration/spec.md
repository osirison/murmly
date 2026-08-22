## ADDED Requirements

### Requirement: Shutdown releases every audio device Murmly opened

When the daemon is asked to stop, it SHALL close each audio stream it opened —
the microphone stream of a capture that is still running, and the speech output
stream — before the process exits. Murmly MUST NOT leave releasing a device to
interpreter teardown, which runs after the daemon has stopped answering and
whose ordering against the audio server is not Murmly's to control.

A stream that will not close MUST be reported and MUST NOT stop the rest of the
shutdown: the socket, the overlay, and the remaining streams still have to be
released.

#### Scenario: Stopped while capture is running

- **WHEN** the daemon is asked to stop while a recording is in progress
- **THEN** the microphone stream is closed as part of the shutdown
- **AND** the microphone is released before the process exits

#### Scenario: Stopped while idle

- **WHEN** the daemon is asked to stop and no recording is in progress
- **THEN** shutdown completes without error

#### Scenario: A stream will not close

- **WHEN** closing an audio stream fails during shutdown
- **THEN** the failure is reported
- **AND** the socket, the overlay, and the remaining streams are still released

### Requirement: The daemon exits cleanly when the audio server is already gone

The daemon SHALL exit with a success status when it is stopped, including when
the audio server it was using has already terminated. It MUST NOT abort, and it
MUST NOT dump core, so the service is left inactive and startable rather than
failed.

Murmly MUST NOT let the audio library tear down the audio backends at process
exit. That teardown asserts on a call that fails once the audio server is gone,
and it covers backends Murmly never opened a stream on, so its outcome does not
depend on anything Murmly did.

#### Scenario: The audio server stops first

- **WHEN** the daemon is stopped after the audio server it was using has already terminated
- **THEN** the process exits with a success status
- **AND** no core dump is produced
- **AND** the service is left inactive rather than failed

#### Scenario: Startup refused

- **WHEN** the daemon refuses to start and the process unwinds
- **THEN** the refusal is still reported to the caller
- **AND** the process exits without an audio teardown fault

#### Scenario: A short-lived command

- **WHEN** a command other than the daemon runs and exits
- **THEN** its process exits unchanged, with the audio library's own exit behavior intact

## MODIFIED Requirements

### Requirement: Daemon runs for the lifetime of the graphical session

Murmly SHALL install a background service that starts when the user's graphical
desktop session becomes available and stops when that session ends. The service
MUST NOT start before the graphical session environment exists, because the
daemon's clipboard, paste, focus, and overlay behavior all depend on it.

The service SHALL be ordered after the session's audio server, so that Murmly is
stopped before the audio server it captures and plays through rather than racing
it at logout.

#### Scenario: Session start

- **WHEN** the user logs into a graphical desktop session after installation
- **THEN** the daemon is running and its command socket accepts commands
- **AND** the daemon observes the session environment of that session

#### Scenario: Logout

- **WHEN** the graphical session ends
- **THEN** the daemon stops
- **AND** no Murmly process outlives the session it was started for

#### Scenario: Logout leaves the service startable

- **WHEN** the graphical session ends and the daemon stops
- **THEN** the service is left inactive rather than failed
- **AND** the next login starts it without the user clearing a failed state first

#### Scenario: Boot without a graphical login

- **WHEN** the machine boots and no graphical session is started
- **THEN** the daemon is not started
