# Command Interface Specification

## Purpose

Defines how a caller reaches Murmly and is answered: who may use the command socket, which accepted connections must receive a response and which may not, how a failure is identified by a stable code, and the rule that no Murmly command terminates without telling its caller why.

## Requirements

### Requirement: An accepted connection is answered

Murmly SHALL send exactly one response to every connection it accepts, unless that connection has declared itself a speech session. A caller cannot distinguish a connection closed in refusal from a daemon that has died, so a connection closed without a response forces the caller to guess.

A response MUST be sent when Murmly is already serving as many commands as it will serve at once, when a request cannot be read, when a request is well-formed but not understood, when a command fails internally, and when shutdown begins after a request has been read. Two cases are exceptions, because there is nothing left to answer or nothing was asked: a connection the peer has already closed, and a connection on which no request had been read when shutdown began.

A speech session is a third exception, and the only one a caller chooses. A connection that declares itself a speech session exchanges many frames in both directions for as long as it stays open, and Murmly MUST write to it without being asked. Murmly MUST refuse a session declaration it does not support with a single response, so a caller that asks for a session it cannot have is answered exactly as any other unsupported request is. A connection that does not declare itself a speech session MUST be answered with exactly one response, unchanged in every respect from before speech sessions existed.

Murmly MUST NOT let open speech sessions consume the capacity it reserves for one-shot commands. A session lasts as long as an exchange between a person and a sender, and state queries and capture toggles have to keep working throughout.

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

#### Scenario: Connection declares itself a speech session

- **WHEN** a connection declares itself a speech session and Murmly supports it
- **THEN** Murmly exchanges frames with it in both directions until the connection closes
- **AND** the one-response rule does not apply to that connection

#### Scenario: Speech session declaration refused

- **WHEN** a connection declares itself a speech session and Murmly cannot provide one
- **THEN** Murmly sends exactly one response identifying why
- **AND** then closes that connection

#### Scenario: Open sessions do not exhaust command capacity

- **WHEN** speech sessions are open and a state query or capture toggle arrives
- **THEN** it is served
- **AND** its response is unaffected by how many sessions are open

#### Scenario: Shutdown with a session open

- **WHEN** shutdown begins while a speech session is open
- **THEN** speech stops and the session is told that Murmly is shutting down
- **AND** the connection is closed

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

A command that reaches Murmly and receives no response, a command run against an unreadable configuration file, and a daemon that refuses to start MUST each be reported as a message and a non-zero exit. A command MUST NOT wait without bound for a response: a connection Murmly accepts and then holds open without answering is the same outcome for the caller as one it closes, and MUST be reported the same way.

#### Scenario: Murmly closes the connection without responding

- **WHEN** a command is sent and the connection closes before any response arrives
- **THEN** the command reports that Murmly did not respond
- **AND** exits non-zero without an unhandled error

#### Scenario: Murmly accepts the command and never answers

- **WHEN** a command is sent, Murmly accepts it, and no response arrives within the supported wait
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
- **AND** anything the daemon started before it refused is stopped

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

A configured socket path another account could take control of MUST be refused at daemon startup, naming the directory at fault and how to correct it. Control is not limited to the directory holding the socket: renaming a directory replaces everything under it, replacing a symbolic link redirects everything reached through it, and an owner can grant itself write access whenever it likes. So every directory a lookup of the configured path passes through counts, symbolic links followed as they are reached, and one another account can write, or that is owned by neither the account the daemon runs as nor the system's own administrative account, is an exposure wherever it sits on that path. Such a directory allows another account to create or replace the socket node, so the owner's own commands would reach a socket Murmly does not serve.

A directory that other accounts can only read or traverse MUST NOT be refused, because the socket node itself is owner-only. Above the deepest directory on the path that already exists, a shared directory that stops one account removing or renaming another's entries MUST NOT be refused either, or the standard shared temporary directory would disqualify every path beneath it. That exemption MUST NOT extend to the deepest existing directory, because the components below it do not exist yet and such a directory does not stop another account creating them.

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

#### Scenario: Configured socket path is below a directory another account can write

- **WHEN** the directory holding the configured socket path is private but a directory above it is writable by group or other
- **THEN** the daemon refuses to start, naming the directory above it as the one at fault
- **AND** no socket is created at that path

#### Scenario: Configured socket path is under a directory another account owns

- **WHEN** a directory on the configured socket path is owned by an account other than the one the daemon runs as
- **THEN** the daemon refuses to start, naming that directory
- **AND** no socket is created at that path

#### Scenario: Configured socket path is reached through a symbolic link

- **WHEN** the configured socket path passes through a symbolic link that sits in a directory writable by group or other, whatever that link points at
- **THEN** the daemon refuses to start, naming the directory holding the link
- **AND** no socket is created at that path

#### Scenario: Shared ancestor that protects the entries in it

- **WHEN** a directory above the one holding the configured socket path is writable by other accounts but stops them removing or renaming entries they do not own
- **THEN** the daemon serves at that path

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
