## MODIFIED Requirements

### Requirement: The command socket is reachable only by the account that owns it

Murmly SHALL restrict its command socket to the user account the daemon runs as. The socket starts and stops the microphone, so any account that can reach it can record.

What the command socket is made of is the platform's to decide. Where the platform provides a socket that is an object in the filesystem, Murmly SHALL use it, and the directories Murmly creates for it and the socket itself MUST be created accessible only to that account. Where the platform provides no such socket, Murmly SHALL use the platform's own local channel and MUST create it so that only the account the daemon runs as can connect, by whatever access control that platform's channel carries. Murmly MUST NOT substitute a channel any account on the machine can reach, and MUST NOT reach the network, whatever the platform offers.

A peer whose reported identity differs from the daemon's MUST be refused. Reading that identity is the platform's own mechanism and MUST NOT be assumed to be one particular mechanism. When the platform cannot report a peer's identity, Murmly MUST report that it cannot and continue serving on the channel's own access control alone, rather than treating an unknown identity as permitted.

A configured socket path another account could take control of MUST be refused at daemon startup, naming the directory at fault and how to correct it. Control is not limited to the directory holding the socket: renaming a directory replaces everything under it, replacing a symbolic link redirects everything reached through it, and an owner can grant itself write access whenever it likes. So every directory a lookup of the configured path passes through counts, symbolic links followed as they are reached, and one another account can write, or that is owned by neither the account the daemon runs as nor the system's own administrative account, is an exposure wherever it sits on that path. Such a directory allows another account to create or replace the socket node, so the owner's own commands would reach a socket Murmly does not serve.

A directory that other accounts can only read or traverse MUST NOT be refused, because the socket node itself is owner-only. Above the deepest directory on the path that already exists, a shared directory that stops one account removing or renaming another's entries MUST NOT be refused either, or the standard shared temporary directory would disqualify every path beneath it. That exemption MUST NOT extend to the deepest existing directory, because the components below it do not exist yet and such a directory does not stop another account creating them.

That whole analysis is about a channel that is a name in the filesystem, and applies only where the platform's channel is one. Where it is not, Murmly SHALL instead refuse at startup any configured channel name it cannot create privately on that platform, naming the reason and how to correct it. The rule being enforced is the same in both cases and MUST be stated as such in the refusal: no other account may reach the channel, and Murmly refuses to serve rather than serve one that can be reached.

#### Scenario: Permissions at creation

- **WHEN** the daemon creates its command socket
- **THEN** the socket and any directory Murmly created for it are accessible only to the account the daemon runs as

#### Scenario: A platform whose channel is not a filesystem object

- **GIVEN** a platform that provides no socket that is an object in the filesystem
- **WHEN** the daemon creates its command channel
- **THEN** the channel is created so that only the account the daemon runs as can connect
- **AND** the daemon does not refuse to start over the privacy of a directory path
- **AND** a connection from another account is refused

#### Scenario: Peer identity differs from the daemon's

- **WHEN** a connection is accepted whose reported peer identity differs from the account the daemon runs as
- **THEN** Murmly refuses the command with a code identifying the caller as not permitted
- **AND** no command is executed
- **AND** the refusal does not consume the capacity available to permitted callers

#### Scenario: Peer identity read by the platform's own mechanism

- **GIVEN** a platform that reports a peer's identity by a mechanism other than the one another platform uses
- **WHEN** a connection is accepted
- **THEN** Murmly reads the peer's identity by that platform's mechanism
- **AND** applies the same rule to the result

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

#### Scenario: A configured channel name that cannot be made private

- **GIVEN** a platform whose channel is not an object in the filesystem
- **WHEN** the configured channel name is one Murmly cannot create so that only its own account can connect
- **THEN** the daemon refuses to start, naming the reason and how to correct it
- **AND** no channel is created under that name

#### Scenario: Other commands still run when the daemon would refuse

- **WHEN** the configured socket path would cause the daemon to refuse to start
- **THEN** diagnostics still run and report that the configured socket path is not private
- **AND** commands that do not start the daemon still load that configuration

#### Scenario: Default socket path is served

- **WHEN** no socket path is configured
- **THEN** the daemon serves at its default per-user runtime location
