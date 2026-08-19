# Desktop Integration Specification

## Purpose

Defines how Murmly installs itself into a desktop session so that it is running when the user needs it and reachable by a single keypress: the daemon's session lifetime, registration and removal of a global hotkey, refusal to take a hotkey another application owns, and what Murmly reports when any of that cannot be confirmed.

## Requirements

### Requirement: Daemon runs for the lifetime of the graphical session

Murmly SHALL install a background service that starts when the user's graphical
desktop session becomes available and stops when that session ends. The service
MUST NOT start before the graphical session environment exists, because the
daemon's clipboard, paste, focus, and overlay behavior all depend on it.

#### Scenario: Session start

- **WHEN** the user logs into a graphical desktop session after installation
- **THEN** the daemon is running and its command socket accepts commands
- **AND** the daemon observes the session environment of that session

#### Scenario: Logout

- **WHEN** the graphical session ends
- **THEN** the daemon stops
- **AND** no Murmly process outlives the session it was started for

#### Scenario: Boot without a graphical login

- **WHEN** the machine boots and no graphical session is started
- **THEN** the daemon is not started

### Requirement: Installed service invokes Murmly by absolute path

The installed service SHALL invoke the entrypoint of the Murmly installation that
performed the install, identified by an absolute path, so that it does not depend
on the shell environment or search path in effect at session start. When that path
cannot be determined, installation MUST fail rather than install a service that
cannot start.

#### Scenario: Path recorded at install time

- **WHEN** installation completes
- **THEN** the installed service refers to the entrypoint by absolute path
- **AND** starting the service does not require a shell search path

#### Scenario: Installation moved

- **WHEN** the Murmly installation is moved or its environment is rebuilt, and
  installation is run again from the new location
- **THEN** the service is updated to the new absolute path
- **AND** the hotkey invokes the new location

#### Scenario: Entrypoint cannot be resolved

- **WHEN** installation cannot determine an absolute, executable entrypoint path
- **THEN** installation fails with a message naming what could not be resolved
- **AND** no service, launcher, or hotkey is left behind

### Requirement: Hotkey takes effect in the running session

Installation SHALL bind the requested hotkey such that it works in the session in
which installation was run, without requiring the user to log out or restart the
desktop. When the binding cannot be confirmed within a bounded time, Murmly MUST
report that plainly rather than reporting success.

#### Scenario: Hotkey bound and usable immediately

- **WHEN** installation completes successfully in a running desktop session
- **THEN** pressing the bound hotkey toggles Murmly capture
- **AND** the user is not required to log out first

#### Scenario: Binding not confirmed within the bounded wait

- **WHEN** the binding cannot be confirmed within the bounded wait
- **THEN** installation reports that the hotkey is not active in this session
- **AND** states whether the binding will take effect at next login
- **AND** does not report success

### Requirement: A hotkey owned by another application is refused

Murmly SHALL determine whether the requested hotkey is already claimed before
binding it, and MUST refuse to bind a hotkey owned by another application. The
desktop does not arbitrate such a collision: a second claimant registers without
error and silently never receives the keypress. Murmly MUST therefore fail closed
and name the current owner.

#### Scenario: Hotkey already owned by another application

- **WHEN** the requested hotkey is already claimed by another application
- **THEN** installation fails without binding the hotkey
- **AND** the message names the application that currently owns it
- **AND** no service, launcher, or hotkey registration is left behind

#### Scenario: Hotkey already owned by Murmly

- **WHEN** the requested hotkey is already bound to Murmly
- **THEN** installation succeeds and reports the existing binding
- **AND** this is not treated as a conflict

#### Scenario: Conflict introduced after the check

- **WHEN** verification after binding shows more than one owner for the hotkey
- **THEN** installation reports a failed binding
- **AND** removes the registration it created

### Requirement: A binding is verified before installation reports success

After registering a hotkey, Murmly SHALL confirm that the desktop resolved it to
the intended key and that Murmly is its sole owner. A binding that cannot be
confirmed MUST be reported as a failure and the partial state removed. Murmly MUST
NOT claim that a keypress will be delivered, because confirming registration does
not confirm that the desktop granted the key grab.

#### Scenario: Registered key differs from the requested key

- **WHEN** the desktop resolved the registration to a key other than the one
  requested
- **THEN** installation reports a failed binding naming both keys
- **AND** removes the registration it created

#### Scenario: Successful verification is reported accurately

- **WHEN** verification confirms the intended key and sole ownership
- **THEN** installation reports the hotkey as registered
- **AND** invites the user to press it once to confirm delivery

### Requirement: Rebinding replaces the previous hotkey in the running session

When installation is run with a hotkey different from the one currently bound,
Murmly SHALL ensure the previous hotkey stops invoking Murmly and the new one
starts, both within the running session. A rebind MUST NOT leave the previous
hotkey active.

#### Scenario: Hotkey changed

- **WHEN** installation is run with a different hotkey than the one bound
- **THEN** the previous hotkey no longer invokes Murmly in the running session
- **AND** the new hotkey invokes Murmly in the running session

#### Scenario: Rebind cannot be completed

- **WHEN** the previous binding cannot be released
- **THEN** installation reports the failure without binding the new hotkey
- **AND** states which hotkey is currently active

### Requirement: Uninstall removes everything Murmly installed

Uninstallation SHALL stop the service, remove the installed service and launcher,
and release the hotkey, so that no Murmly-owned desktop state remains. It MUST
succeed when some or all of that state is already absent, and MUST NOT depend on
any particular piece of it existing.

#### Scenario: Full removal

- **WHEN** uninstallation runs on an installed system
- **THEN** the daemon is stopped and its service removed
- **AND** the hotkey is released and reported as available again
- **AND** the launcher entry is removed

#### Scenario: Nothing installed

- **WHEN** uninstallation runs and no Murmly desktop state is present
- **THEN** it succeeds and reports that there was nothing to remove

#### Scenario: Partially installed

- **WHEN** uninstallation runs and only some of the installed state is present
- **THEN** it removes what is present and succeeds

### Requirement: Murmly does not modify desktop configuration it does not own

Installation and uninstallation SHALL confine their writes to files Murmly
creates. Murmly MUST NOT edit the user's global shortcut configuration or any
other application's desktop configuration, and MUST NOT disturb hotkeys belonging
to other applications.

#### Scenario: Shortcut configuration untouched

- **WHEN** installation and uninstallation have both run
- **THEN** the user's global shortcut configuration is unchanged
- **AND** hotkeys belonging to other applications continue to work

#### Scenario: Existing user override respected

- **WHEN** the user has already overridden Murmly's hotkey through the desktop's
  own settings
- **THEN** installation reports that a user override is in effect
- **AND** does not overwrite it

### Requirement: A hotkey press recovers when the daemon is not listening

When a hotkey press reaches Murmly and the daemon is not accepting commands,
Murmly SHALL attempt to start the installed service, wait a bounded time for it,
and retry once. A hotkey press MUST NOT surface an unhandled error, because a
hotkey has no visible output channel.

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

### Requirement: Hotkey specification is validated strictly

Murmly SHALL accept a hotkey only when it can be parsed unambiguously into a known
key with at least one modifier, and MUST reject anything else rather than binding
a key the user did not intend. Rejection MUST identify what was not understood.

#### Scenario: Unrecognized key name

- **WHEN** the requested hotkey names a key Murmly does not recognize
- **THEN** Murmly refuses with a message naming the unrecognized part
- **AND** nothing is installed or bound

#### Scenario: No modifier

- **WHEN** the requested hotkey carries no modifier
- **THEN** Murmly refuses and states that at least one modifier is required

#### Scenario: Accepted modifier aliases

- **WHEN** the requested hotkey uses a common alias for the platform modifier key
- **THEN** Murmly normalizes it and binds the intended key

### Requirement: Diagnostics report installation state

`murmly doctor` SHALL report whether Murmly is installed, whether the service is
active, which hotkey is bound, and whether that binding is currently held by
Murmly, alongside its existing sections.

#### Scenario: Installed and healthy

- **WHEN** diagnostics run on an installed system with the hotkey held by Murmly
- **THEN** the report names the bound hotkey and states that the service is active

#### Scenario: Not installed

- **WHEN** diagnostics run and no service is installed
- **THEN** the report states that Murmly is not installed

#### Scenario: Hotkey lost to another application

- **WHEN** diagnostics run and the bound hotkey is claimed by another application
- **THEN** the report states that the hotkey is not held by Murmly
- **AND** names the application that holds it

### Requirement: Unsupported desktops are refused rather than silently skipped

Hotkey registration SHALL be attempted only on desktop environments Murmly
supports. On an unsupported desktop, Murmly MUST install the service, decline the
hotkey with an explanation, and tell the user how to bind one themselves.

#### Scenario: Unsupported desktop environment

- **WHEN** installation runs on a desktop environment whose hotkey registration
  Murmly does not support
- **THEN** the service is installed and started
- **AND** Murmly reports that it cannot register a hotkey on this desktop
- **AND** reports the exact command to bind manually

#### Scenario: Supported desktop, unverified session type

- **WHEN** installation runs on a supported desktop under a session type Murmly
  has not verified
- **THEN** installation proceeds and verification decides the outcome
- **AND** the report states that the session type is unverified

### Requirement: Installation reports whether a transcript can be pasted

Installation SHALL report whether Murmly can inject a paste in the session it installed into, and when it cannot, MUST name what the user has to install or enable. Because Murmly still copies every transcript to the clipboard, this MUST NOT fail the installation, and Murmly MUST NOT change system state outside the files it owns in order to satisfy it.

#### Scenario: Paste injection available

- **WHEN** installation completes in a session where Murmly can inject a paste
- **THEN** the report states that transcripts will be pasted into the focused window

#### Scenario: Paste injection unavailable

- **WHEN** installation completes in a session where Murmly cannot inject a paste
- **THEN** installation still succeeds and the service and hotkey are installed
- **AND** the report states that transcripts will be copied but not pasted
- **AND** names what the user has to install or enable for this session

#### Scenario: Murmly does not install the injector itself

- **WHEN** installation finds no usable injection method
- **THEN** Murmly installs no package and enables no system service
- **AND** the remedy is reported as commands for the user to run
