## ADDED Requirements

### Requirement: The daemon exits with the status it determined

The daemon process SHALL terminate with the exit status it determined for its run, and
MUST NOT be terminated by a signal after determining it. A run that stopped cleanly MUST
be observable as a clean stop by the service manager that started it, whether or not the
audio server outlived the daemon.

Murmly deliberately leaves PortAudio's exit-time teardown unregistered, because that
teardown aborts the process when the audio server has already stopped. That teardown is
also what stopped PortAudio's own threads, so those threads outlive the work the daemon
was doing. The daemon MUST therefore release what it owns and leave without running
interpreter finalization, rather than letting finalization unload libraries while those
threads are still executing in them.

Because finalization does not run, the daemon MUST explicitly flush or close anything
whose loss would be observable — buffered output and log handlers among them — before it
leaves. Anything the operating system reclaims on process exit MAY be left to it.

#### Scenario: A clean stop is recorded as a clean stop

- **GIVEN** the daemon is running and the audio server is running
- **WHEN** the daemon is asked to stop
- **THEN** the process exits zero
- **AND** it is not terminated by a signal
- **AND** the service manager records a clean stop rather than a failure

#### Scenario: A stop that outlives the audio server is also clean

- **GIVEN** the daemon is running
- **WHEN** the audio server stops first and the daemon is then asked to stop
- **THEN** the process exits zero
- **AND** it is not terminated by a signal

#### Scenario: A startup refusal keeps its exit status

- **WHEN** the daemon refuses to start
- **THEN** the process exits non-zero with the status that refusal determined
- **AND** the reason has already been reported on its output

#### Scenario: Output is not lost by leaving early

- **WHEN** the daemon reports something on its output or its log and then exits
- **THEN** that output is complete in the destination
- **AND** nothing written before the exit is truncated

#### Scenario: A command that is not the daemon is unaffected

- **WHEN** any command other than the daemon runs
- **THEN** it returns its exit status through the ordinary path
- **AND** interpreter finalization runs as it does today
