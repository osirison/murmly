## ADDED Requirements

### Requirement: No renderer process is abandoned when Murmly shuts down

When Murmly shuts down, it SHALL terminate any overlay renderer process it
launched, including one whose launch was still under way at the moment shutdown
began. Shutdown MUST NOT wait indefinitely for a launch still in progress before
returning; it SHALL return within its existing bounded time regardless of how long
the renderer takes to start. A renderer whose launch was still in progress when
that bound was reached MUST still be terminated once the launch completes, even
though shutdown itself has already returned.

This requirement covers Murmly shutting down through its own control flow — the
ordinary stop of the daemon and the equivalent cleanup on every other path that
stops serving. It does not cover the daemon process being killed outright.

#### Scenario: Shutdown begins while the renderer is still launching

- **WHEN** Murmly shuts down while the overlay's renderer process is still being
  started
- **THEN** shutdown does not wait for that launch to finish before returning
- **AND** the renderer process is terminated once the launch completes, even though
  shutdown has already returned

#### Scenario: Shutdown after the renderer is already running

- **WHEN** Murmly shuts down after the overlay's renderer process has already
  started
- **THEN** the renderer process is terminated before shutdown returns

#### Scenario: The overlay is closed more than once

- **WHEN** Murmly's overlay is closed a second time after it has already closed
- **THEN** the second close returns without attempting to terminate a process a
  second time in a way that raises an error
