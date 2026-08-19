## MODIFIED Requirements

### Requirement: A hotkey press recovers when the daemon is not listening

When a hotkey press reaches Murmly and the daemon is not accepting commands,
Murmly SHALL attempt to start the installed service, wait a bounded time for it,
and retry once. A hotkey press MUST NOT surface an unhandled error, because a
hotkey has no visible output channel. A daemon that accepts the connection and
then closes it without responding MUST be treated as not having answered, not as
an error to raise.

#### Scenario: Daemon not running but installed

- **WHEN** the hotkey is pressed, the service is installed, and the daemon is not
  accepting commands
- **THEN** Murmly starts the service, waits for it, and retries the command once
- **AND** capture begins as it would have if the daemon were already running

#### Scenario: Murmly not installed

- **WHEN** the hotkey is pressed or the command is run and no service is installed
- **THEN** Murmly exits non-zero with a message naming the command that installs it
- **AND** no unhandled error is raised

#### Scenario: Service fails to start

- **WHEN** recovery is attempted and the daemon does not accept commands within
  the bounded wait
- **THEN** Murmly exits non-zero with a message stating that the daemon could not
  be started
- **AND** does not retry indefinitely

#### Scenario: Daemon accepts the connection but does not respond

- **WHEN** the hotkey is pressed, the connection is accepted, and it closes before
  any response arrives
- **THEN** Murmly exits non-zero with a message stating that the daemon did not
  respond
- **AND** no unhandled error is raised
