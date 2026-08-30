## MODIFIED Requirements

### Requirement: Daemon runs for the lifetime of the graphical session

Murmly SHALL install a background service that starts when the user's graphical
desktop session becomes available and stops when that session ends. The service
MUST NOT start before the graphical session environment exists, because the
daemon's clipboard, paste, focus, and overlay behavior all depend on it.

The service SHALL be registered with the platform's own per-user service manager,
and installation MUST NOT require administrative rights, because a background
service for one person's session is not a machine-wide change.

Where the platform's service manager can express an ordering against the session's
audio server, the service SHALL be ordered after it, so that Murmly is stopped
before the audio server it captures and plays through rather than racing it at
logout. Where the platform's service manager cannot express that ordering, Murmly
MUST NOT depend on it: the daemon has to survive the audio server disappearing
underneath it, which it is separately required to do.

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

#### Scenario: Installation needs no administrative rights

- **WHEN** installation runs as an ordinary user account on any supported platform
- **THEN** the service is registered and started
- **AND** nothing outside that account's own files and settings is written

#### Scenario: A service manager that cannot order against the audio server

- **GIVEN** a platform whose per-user service manager expresses no ordering against
  the audio server
- **WHEN** the session ends and the audio server stops before the daemon does
- **THEN** the daemon still stops with a success status
- **AND** the service is left inactive rather than failed

### Requirement: Hotkey takes effect in the running session

Installation SHALL bind each requested hotkey such that it works in the session in
which installation was run, without requiring the user to log out or restart the
desktop. When a binding cannot be confirmed within a bounded time, Murmly MUST
report that plainly rather than reporting success, and MUST name which hotkey it
could not confirm.

Where the platform registers the hotkey inside Murmly's own process rather than in
the desktop's shortcut system, the binding exists only while the daemon is running.
Installation MUST therefore start the daemon before it reports a hotkey as bound,
and Murmly MUST report such a binding as depending on the running daemon rather
than as a registration the desktop holds independently. A hotkey the daemon holds
MUST be released when the daemon stops, so that it does not remain claimed against
another application after Murmly is no longer there to receive it.

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

#### Scenario: A hotkey held by the daemon's own process

- **GIVEN** a platform on which Murmly registers the hotkey in its own process
- **WHEN** installation completes
- **THEN** the daemon is running and the hotkey works
- **AND** the report states that the hotkey is held by the running daemon

#### Scenario: The daemon holding a hotkey stops

- **GIVEN** a platform on which Murmly registers the hotkey in its own process
- **WHEN** the daemon stops
- **THEN** the hotkey is released rather than left claimed
- **AND** diagnostics report that the hotkey is not currently held

### Requirement: A hotkey owned by another application is refused

Murmly SHALL refuse to bind a hotkey owned by another application, and MUST name
the current owner where the platform allows it to be determined.

How that is established depends on the platform. Where the platform arbitrates the
claim — refusing a registration for a key another application already holds —
Murmly SHALL treat that refusal as the collision and report it. Where the platform
does not arbitrate, Murmly MUST determine whether the key is already claimed before
binding it and fail closed, because there a second claimant registers without error
and silently never receives the keypress. Murmly MUST NOT report a hotkey as bound
on the strength of a registration the platform accepted without arbitrating.

#### Scenario: Hotkey already owned by another application

- **WHEN** the requested hotkey is already claimed by another application
- **THEN** installation fails without binding the hotkey
- **AND** the message names the application that currently owns it, where the
  platform can report which one it is
- **AND** no service, launcher, or hotkey registration is left behind

#### Scenario: The platform refuses the registration itself

- **GIVEN** a platform that refuses to register a key another application holds
- **WHEN** installation requests such a key
- **THEN** installation fails naming the hotkey as already claimed
- **AND** no service, launcher, or hotkey registration is left behind

#### Scenario: Hotkey already owned by Murmly

- **WHEN** the requested hotkey is already bound to Murmly
- **THEN** installation succeeds and reports the existing binding
- **AND** this is not treated as a conflict

#### Scenario: Conflict introduced after the check

- **WHEN** verification after binding shows more than one owner for the hotkey
- **THEN** installation reports a failed binding
- **AND** removes the registration it created

### Requirement: A hotkey press recovers when the daemon is not listening

Where the hotkey reaches Murmly by invoking it as a command, and the daemon is not
accepting commands, Murmly SHALL attempt to start the installed service, wait a
bounded time for it, and retry once. A hotkey press MUST NOT surface an unhandled
error, because a hotkey has no visible output channel. A daemon that accepts the
connection and then closes it without responding MUST be treated as not having
answered, not as an error to raise. Starting the service SHALL be done through the
platform's own service manager rather than by a mechanism belonging to one
platform.

Where the hotkey is held by the daemon's own process, there is no press to recover
from: a stopped daemon holds no hotkey and receives nothing. On such a platform
Murmly MUST make the state legible instead — diagnostics report the hotkey as not
currently held and name the daemon as the reason — and the service manager's own
restart behaviour is what returns it, rather than a press that starts it.

#### Scenario: Daemon not running but installed

- **WHEN** the hotkey is pressed, the service is installed, the hotkey reaches
  Murmly as a command, and the daemon is not accepting commands
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

#### Scenario: The daemon holding the hotkey is not running

- **GIVEN** a platform on which Murmly holds the hotkey in its own process
- **WHEN** the daemon is not running
- **THEN** diagnostics state that the hotkey is not currently held and that the
  daemon is why
- **AND** name the command that starts the service

### Requirement: Hotkey specification is validated strictly

Murmly SHALL accept a hotkey only when it can be parsed unambiguously into a known
key with at least one modifier, and MUST reject anything else rather than binding
a key the user did not intend. Rejection MUST identify what was not understood.

One hotkey specification SHALL mean the same key on every platform. Murmly SHALL
accept the names each platform's own users write for a modifier and normalise them
to one meaning, so that a specification written for one platform is not silently
read as a different key on another. A specification naming a modifier the resolved
platform does not have MUST be refused, naming it, rather than dropped or
substituted.

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

#### Scenario: The same specification on two platforms

- **WHEN** one hotkey specification is installed on two different supported
  platforms
- **THEN** the same physical key combination is bound on both

#### Scenario: A modifier this platform does not have

- **WHEN** the requested hotkey names a modifier the resolved platform does not
  have
- **THEN** Murmly refuses naming that modifier
- **AND** binds nothing

### Requirement: Unsupported desktops are refused rather than silently skipped

Hotkey registration SHALL be attempted only where Murmly supports it. Where it does
not, Murmly MUST install the service, decline the hotkey with an explanation, and
tell the user how to bind one themselves. The explanation MUST distinguish a
platform on which Murmly registers hotkeys but this desktop offers no route, from
a platform Murmly registers hotkeys on generally, because only one of those is
something a person can act on by changing desktops.

Declining the hotkey MUST NOT decline the installation. A person on such a desktop
gets a working daemon, a working command, and an instruction they can follow, which
is the honest outcome when the desktop offers Murmly no programmatic route.

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

#### Scenario: The platform registers hotkeys but this desktop does not

- **WHEN** installation runs on a platform where Murmly registers hotkeys on some
  desktops and this desktop is not one of them
- **THEN** the report names this desktop as the reason rather than the platform
- **AND** the service is installed and started

#### Scenario: Everything else still installs

- **WHEN** a hotkey is declined for any of these reasons
- **THEN** the service, the command, and every other capability are installed and
  reported as usable
- **AND** installation does not exit non-zero for the declined hotkey alone

### Requirement: Installation reports whether a transcript can be pasted

Installation SHALL report whether Murmly can inject a paste in the session it installed into, and when it cannot, MUST name what the user has to install, enable, or grant. Because Murmly still copies every transcript to the clipboard, this MUST NOT fail the installation, and Murmly MUST NOT change system state outside the files it owns in order to satisfy it.

Where the platform gates injection behind a permission, installation MUST report an ungranted permission as the reason rather than reporting the method as unavailable, and MUST name where the permission is granted. It MUST NOT request the permission as a side effect of installing, because a permission dialog raised by an install the person did not connect to it is one they cannot answer usefully.

#### Scenario: Paste injection available

- **WHEN** installation completes in a session where Murmly can inject a paste
- **THEN** the report states that transcripts will be pasted into the focused window

#### Scenario: Paste injection unavailable

- **WHEN** installation completes in a session where Murmly cannot inject a paste
- **THEN** installation still succeeds and the service and hotkey are installed
- **AND** the report states that transcripts will be copied but not pasted
- **AND** names what the user has to install, enable, or grant for this session

#### Scenario: Injection gated behind an ungranted permission

- **WHEN** installation completes where an injection method exists but the
  permission it needs has not been granted
- **THEN** the report names the ungranted permission and where to grant it
- **AND** does not report the method as absent

#### Scenario: Murmly does not install the injector itself

- **WHEN** installation finds no usable injection method
- **THEN** Murmly installs no package and enables no system service
- **AND** the remedy is reported as commands for the user to run
