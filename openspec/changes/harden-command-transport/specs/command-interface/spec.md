## Purpose

Defines how a caller reaches Murmly and is answered: who may use the command socket, which accepted connections must receive a response and which may not, how a failure is identified by a stable code, and the rule that no Murmly command terminates without telling its caller why.

## ADDED Requirements

### Requirement: An accepted connection is answered

Murmly SHALL send exactly one response to every connection it accepts. A caller cannot distinguish a connection closed in refusal from a daemon that has died, so a connection closed without a response forces the caller to guess.

A response MUST be sent when Murmly is already serving as many commands as it will serve at once, when a request cannot be read, when a request is well-formed but not understood, when a command fails internally, and when shutdown begins after a request has been read. Two cases are exceptions, because there is nothing left to answer or nothing was asked: a connection the peer has already closed, and a connection on which no request had been read when shutdown began.

#### Scenario: Murmly is at connection capacity

- **WHEN** a connection is accepted while Murmly is already serving as many commands as it will serve at once
- **THEN** Murmly sends a response reporting that it is over capacity
- **AND** then closes that connection

#### Scenario: Request cannot be read

- **WHEN** a request is not valid JSON, is not valid text, exceeds the supported request size, or does not arrive within the supported wait
- **THEN** Murmly sends an unsuccessful response naming what it could not read
- **AND** Murmly continues accepting further commands

#### Scenario: Command fails unexpectedly

- **WHEN** handling a command fails in a way Murmly does not anticipate
- **THEN** the caller receives an unsuccessful response rather than an unanswered connection
- **AND** Murmly continues accepting further commands

#### Scenario: Shutdown after a request was read

- **WHEN** shutdown begins while a command whose request has already been read is still being handled
- **THEN** the caller receives a response rather than an empty read

#### Scenario: Shutdown before a request was read

- **WHEN** shutdown begins on a connection from which no request has been read
- **THEN** Murmly closes that connection without a response

#### Scenario: Peer closed the connection first

- **WHEN** the peer closes the connection before Murmly can write the response
- **THEN** Murmly discards the response and continues accepting further commands

#### Scenario: Peer never reads its response

- **WHEN** a peer connects, is answered, and never reads what Murmly wrote
- **THEN** Murmly stops waiting on that peer within a bounded interval
- **AND** continues accepting further commands

#### Scenario: Successful commands are unchanged

- **WHEN** the state query succeeds
- **THEN** its response reports success and the daemon state, and carries no other field

### Requirement: A request of an unexpected shape is answered, not fatal

A request whose payload is valid JSON but is not an object, and a request whose command name is not text, MUST each be answered with an unsuccessful response. Murmly MUST NOT allow a request of an unexpected shape to prevent it from answering that request or any later one.

Fields a request carries beyond its command name MUST NOT cause the request to be refused.

#### Scenario: Payload is valid JSON but not an object

- **WHEN** a request payload is a JSON array, string, number, or boolean
- **THEN** Murmly sends an unsuccessful response reporting that the request is not an object
- **AND** Murmly continues accepting further commands

#### Scenario: Command name is not text

- **WHEN** a request object carries a command name that is not text
- **THEN** Murmly sends an unsuccessful response reporting an unsupported command
- **AND** no command is executed

#### Scenario: Request carries additional fields

- **WHEN** a request object carries fields beyond its command name
- **THEN** Murmly executes the command as it would without them

### Requirement: Unsuccessful responses identify the failure by a stable code

Every unsuccessful response SHALL carry a machine-readable code identifying the category of failure, alongside the human-readable message it already carries. A caller that must decide what to do next cannot do so by matching on prose, because prose is free to be reworded.

Codes MUST distinguish at least these categories: Murmly is busy with another command, the command is not supported, the request could not be read, Murmly is over capacity, the caller is not permitted, Murmly is shutting down, and the command itself failed. An unsuccessful response MUST keep its current wording and every other field it carries today, so an existing reader of that response continues to work.

#### Scenario: Command refused while Murmly is busy

- **WHEN** a command arrives while Murmly is handling another one
- **THEN** the response carries a code identifying Murmly as busy
- **AND** the response carries the same message and the same other fields it carried before

#### Scenario: Distinct categories carry distinct codes

- **WHEN** two responses report failures in different categories
- **THEN** their codes differ

#### Scenario: Successful responses carry no code

- **WHEN** a command succeeds
- **THEN** its response carries no failure code

### Requirement: No command terminates with an unhandled error

Every Murmly command SHALL report the failures it encounters through its own output and exit status, and MUST NOT terminate with an unhandled error. Commands are invoked from a desktop hotkey and from scripts, neither of which surfaces an unhandled error usefully, so such a failure is indistinguishable from the command doing nothing.

A command that reaches Murmly and receives no response, a command run against an unreadable configuration file, and a daemon that refuses to start MUST each be reported as a message and a non-zero exit.

#### Scenario: Murmly closes the connection without responding

- **WHEN** a command is sent and the connection closes before any response arrives
- **THEN** the command reports that Murmly did not respond
- **AND** exits non-zero without an unhandled error

#### Scenario: Configuration file cannot be read

- **WHEN** any command runs against a configuration file that cannot be parsed
- **THEN** the command reports that the configuration could not be read, naming the file
- **AND** exits non-zero without an unhandled error

#### Scenario: Daemon refuses to start

- **WHEN** the daemon refuses to start for any reason it detects itself
- **THEN** the reason is reported as a message and a non-zero exit
- **AND** no unhandled error is raised

### Requirement: Diagnostics report every section they can determine

`murmly doctor` SHALL report every section it can determine and describe each section it cannot, and MUST NOT abandon the whole report because one section could not be determined. This applies to every diagnostics section, including those specified by other capabilities. A report that stops at the first unavailable probe withholds precisely the information the command exists to provide.

#### Scenario: Transcription runtime is configured but unavailable

- **WHEN** diagnostics run with a transcription device configured that is not available
- **THEN** the report is still produced
- **AND** the transcription section states why the runtime could not be determined
- **AND** the remaining sections report their values

#### Scenario: Every section reports

- **WHEN** diagnostics run and every probe succeeds
- **THEN** each section reports its value in the shape it reported before this requirement existed

### Requirement: The command socket is reachable only by the account that owns it

Murmly SHALL restrict its command socket to the user account the daemon runs as. The socket starts and stops the microphone, so any account that can reach it can record.

Directories Murmly creates for the socket, and the socket itself, MUST be created accessible only to that account. A peer whose reported identity differs from the daemon's MUST be refused. When the platform cannot report a peer's identity, Murmly MUST report that it cannot and continue serving on file permissions alone, rather than treating an unknown identity as permitted.

A configured socket path whose containing directory is writable by another account MUST be refused at daemon startup, reporting the path and how to correct it. Such a directory allows another account to create or replace the socket node, so the owner's own commands would reach a socket Murmly does not serve. A directory that other accounts can only read or traverse MUST NOT be refused, because the socket node itself is owner-only.

#### Scenario: Permissions at creation

- **WHEN** the daemon creates its command socket
- **THEN** the socket and any directory Murmly created for it are accessible only to the account the daemon runs as

#### Scenario: Peer identity differs from the daemon's

- **WHEN** a connection is accepted whose reported peer identity differs from the account the daemon runs as
- **THEN** Murmly refuses the command with a code identifying the caller as not permitted
- **AND** no command is executed
- **AND** the refusal does not consume the capacity available to permitted callers

#### Scenario: Platform cannot report peer identity

- **WHEN** the daemon runs where a peer's identity cannot be determined
- **THEN** Murmly reports that peer identity cannot be verified
- **AND** continues serving commands

#### Scenario: Configured socket path is in a directory another account can write

- **WHEN** the configured socket path lies in a directory writable by group or other
- **THEN** the daemon refuses to start, reporting the configured path and how to correct it
- **AND** no socket is created at that path

#### Scenario: Configured socket path is readable but not writable by others

- **WHEN** the configured socket path lies in a directory other accounts can read or traverse but not write
- **THEN** the daemon serves at that path

#### Scenario: Other commands still run when the daemon would refuse

- **WHEN** the configured socket path would cause the daemon to refuse to start
- **THEN** diagnostics still run and report that the configured socket path is not private
- **AND** commands that do not start the daemon still load that configuration

#### Scenario: Default socket path is served

- **WHEN** no socket path is configured
- **THEN** the daemon serves at its default per-user runtime location
