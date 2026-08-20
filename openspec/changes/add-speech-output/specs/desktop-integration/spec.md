## ADDED Requirements

### Requirement: Murmly binds more than one hotkey

Murmly SHALL install a hotkey that starts capture for delivery to the focused
window and a second hotkey that starts capture for delivery to an open speech
session. Every requirement in this capability governing how a hotkey is claimed,
bound, verified, rebound, released, and reported SHALL apply to each bound hotkey
independently, and a failure affecting one MUST NOT be reported as a failure of
another.

Murmly MUST refuse an installation that requests the same key for both, naming the
collision. Two Murmly bindings on one key cannot be told apart by the desktop, so
one of them would silently never receive the keypress.

#### Scenario: Both hotkeys installed

- **WHEN** installation runs with a hotkey for each purpose
- **THEN** both are bound and verified independently
- **AND** each is reported with the purpose it serves

#### Scenario: One hotkey is owned by another application

- **WHEN** one requested hotkey is claimed by another application and the other is free
- **THEN** installation fails naming which hotkey collided and which application owns it
- **AND** no service, launcher, or hotkey registration is left behind

#### Scenario: The same key requested for both purposes

- **WHEN** installation requests the same key for both hotkeys
- **THEN** installation fails naming the collision
- **AND** binds neither

## MODIFIED Requirements

### Requirement: Hotkey takes effect in the running session

Installation SHALL bind each requested hotkey such that it works in the session in
which installation was run, without requiring the user to log out or restart the
desktop. When a binding cannot be confirmed within a bounded time, Murmly MUST
report that plainly rather than reporting success, and MUST name which hotkey it
could not confirm.

#### Scenario: Hotkey bound and usable immediately

- **WHEN** installation completes successfully in a running desktop session
- **THEN** pressing the hotkey bound for the focused window toggles Murmly capture
  for delivery to the focused window
- **AND** pressing the hotkey bound for a speech session toggles Murmly capture for
  delivery to that session
- **AND** the user is not required to log out first

#### Scenario: Binding not confirmed within the bounded wait

- **WHEN** a binding cannot be confirmed within the bounded wait
- **THEN** installation reports which hotkey is not active in this session
- **AND** states whether the binding will take effect at next login
- **AND** does not report success

### Requirement: Uninstall removes everything Murmly installed

Uninstallation SHALL stop the service, remove the installed service and launcher,
and release every hotkey Murmly bound, so that no Murmly-owned desktop state
remains. It MUST succeed when some or all of that state is already absent, and MUST
NOT depend on any particular piece of it existing.

#### Scenario: Full removal

- **WHEN** uninstallation runs on an installed system
- **THEN** the daemon is stopped and its service removed
- **AND** every hotkey Murmly bound is released and reported as available again
- **AND** the launcher entry is removed

#### Scenario: Nothing installed

- **WHEN** uninstallation runs and no Murmly desktop state is present
- **THEN** it succeeds and reports that there was nothing to remove

#### Scenario: Partially installed

- **WHEN** uninstallation runs and only some of the installed state is present
- **THEN** it removes what is present and succeeds

#### Scenario: Only one hotkey still bound

- **WHEN** uninstallation runs and only one of Murmly's hotkeys is still bound
- **THEN** it releases that one and succeeds
- **AND** does not report the absent binding as a failure

### Requirement: Diagnostics report installation state

`murmly doctor` SHALL report whether Murmly is installed, whether the service is
active, which hotkeys are bound and what each is for, and whether each binding is
currently held by Murmly, alongside its existing sections.

#### Scenario: Installed and healthy

- **WHEN** diagnostics run on an installed system with every hotkey held by Murmly
- **THEN** the report names each bound hotkey with its purpose and states that the
  service is active

#### Scenario: Not installed

- **WHEN** diagnostics run and no service is installed
- **THEN** the report states that Murmly is not installed

#### Scenario: Hotkey lost to another application

- **WHEN** diagnostics run and a bound hotkey is claimed by another application
- **THEN** the report states which hotkey is not held by Murmly
- **AND** names the application that holds it
