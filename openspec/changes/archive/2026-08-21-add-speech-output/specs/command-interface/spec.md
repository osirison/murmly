## MODIFIED Requirements

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
