## Purpose

Defines how Murmly determines which platform it is running on, how it selects the
mechanism that serves each platform-dependent concern, where it writes its files,
what it does when a concern has no mechanism or a permission has been denied, and
what it reports about all of that, so that the same Murmly behaves identically
everywhere it can and says plainly what it cannot do here.

## ADDED Requirements

### Requirement: Murmly resolves one platform identity and uses it everywhere

Murmly SHALL determine, once per process, the platform it is running on — the
operating system, and where behaviour depends on it the session or desktop within
that operating system — and SHALL make every platform-dependent decision from that
one resolution. Separate subsystems MUST NOT each detect the environment for
themselves, because subsystems that detect independently can disagree, and a
Murmly that has decided it is on one platform for the purpose of the overlay and
another for the purpose of pasting cannot be diagnosed from its own report.

The resolution SHALL be derivable from an environment supplied to it rather than
only from the process's own, so that behaviour on a platform can be exercised
without running on it.

#### Scenario: The platform is resolved once

- **WHEN** the daemon starts
- **THEN** every platform-dependent mechanism it selects is selected from one
  resolved platform identity
- **AND** no two subsystems report a different platform for the same process

#### Scenario: A supplied environment decides the resolution

- **WHEN** the resolution is asked for against a supplied environment rather than
  the process's own
- **THEN** it answers for that environment
- **AND** the answer does not depend on the environment Murmly is actually running in

### Requirement: Murmly supports Linux and Windows, and says exactly why it cannot run where it cannot

Murmly SHALL run on Linux and on Windows. On Linux it MUST NOT require a
particular distribution, package manager, init system, desktop environment, or
display protocol in order to start, capture, transcribe, and put a transcript on
the clipboard. Recognising the distribution MUST NOT be a condition of running on
it.

macOS is deliberately absent from that list rather than missing from it. Every
macOS mechanism this capability describes is built and exercised — the command
channel, the service, the hotkey, the clipboard, injection, focus observation and
synthesis all have a macOS backend that runs against the real system APIs. What
is not established is whether a daemon started by the platform's own service
manager can capture audio at all, which macOS gates behind a permission that
fails silently rather than refusing. Until that is proven, claiming macOS would
be claiming the one thing Murmly is for. A later change adds it to this
requirement; nothing here is written so as to make that harder.

Murmly depends on runtimes that are not built for every combination of operating
system, processor architecture, and C library. Where a runtime Murmly needs has no
build available for the machine it is on, Murmly SHALL report that by naming the
runtime, the machine characteristic that has no build, and whether the capability
that runtime serves is one Murmly can continue without. It MUST NOT surface the
runtime's own failure to load in place of that explanation, because the person
reading it can act on "no build of the synthesis runtime exists for this processor"
and cannot act on a loader error.

Where the missing runtime is the one transcription needs, the daemon SHALL refuse
to start and say so, because transcription is what Murmly is. Where it serves
anything else, the daemon SHALL start and report that capability as unavailable
for that reason.

On an operating system Murmly does not support at all, every command SHALL refuse
immediately with a message naming the platform it found and the platforms it
supports. It MUST NOT start the daemon, create a command channel, register a
hotkey, or write any file, because a partial installation on a platform that
cannot run it is worse than none: it leaves state behind that the uninstaller for
that platform does not exist to remove.

#### Scenario: A distribution Murmly has never been run on

- **WHEN** Murmly starts on a Linux distribution whose package manager, init
  system, and desktop it does not recognise
- **THEN** the daemon starts and serves commands
- **AND** capture, transcription, and copying a transcript to the clipboard work
- **AND** each concern it cannot serve here is reported with a reason

#### Scenario: A machine with no build of the transcription runtime

- **WHEN** the machine's operating system, processor architecture, or C library has
  no available build of the runtime transcription needs
- **THEN** the daemon refuses to start, naming that runtime and the characteristic
  that has no build
- **AND** the refusal is not the runtime's own load error

#### Scenario: A machine with no build of a runtime outside the core

- **WHEN** the machine has no available build of a runtime serving a capability
  other than transcription
- **THEN** the daemon starts and serves capture, transcription, and delivery
- **AND** that capability is reported unavailable, naming the runtime and the
  characteristic that has no build

#### Scenario: An unsupported operating system

- **WHEN** any Murmly command runs on an operating system Murmly does not support
- **THEN** it exits non-zero naming the platform found and the platforms supported
- **AND** no daemon is started, no channel is created, and no file is written

### Requirement: Each platform-dependent concern reports the mechanism it selected

`murmly doctor` SHALL report, for each concern whose implementation depends on the
platform, which mechanism was selected — or that none was available, and why. The
concerns are the command channel, service management, hotkey registration,
clipboard access, paste injection, focus observation, the overlay, and speech
synthesis.

The report MUST name the mechanism specifically enough to act on, and MUST
distinguish a mechanism that does not exist on this platform from one that exists
but could not be used here, because those have different remedies and only one of
them is worth a person's time.

#### Scenario: Every concern names its mechanism

- **WHEN** diagnostics run on a supported platform
- **THEN** the report names the resolved platform
- **AND** names, for each platform-dependent concern, the mechanism selected or the
  reason none was

#### Scenario: A mechanism that does not exist here

- **WHEN** a concern has no mechanism at all on the resolved platform
- **THEN** the report states that the platform offers none
- **AND** does not name something to install

#### Scenario: A mechanism that exists but could not be used

- **WHEN** a concern has a mechanism on this platform that could not be used
- **THEN** the report states what prevented it
- **AND** names what to install, enable, or grant

### Requirement: A concern with no mechanism degrades without stopping Murmly

Capture, transcription, and placing a transcript on the clipboard SHALL work on
every supported platform. Every other concern, when the platform offers it no
mechanism or the mechanism cannot be used, SHALL be reported unavailable with a
reason while the rest of Murmly continues, exactly as a missing overlay already
does. Murmly MUST NOT refuse to start, and MUST NOT fail a command, because a
concern outside that core is unavailable.

#### Scenario: No overlay, no injector, no focus observation

- **WHEN** the daemon runs where the overlay, paste injection, and focus
  observation all have no usable mechanism
- **THEN** the daemon starts and serves every command
- **AND** a transcript is produced and placed on the clipboard
- **AND** each unavailable concern is reported with its reason

#### Scenario: A concern fails after the daemon has started

- **WHEN** a platform mechanism that was available stops working while the daemon
  runs
- **THEN** the command that needed it reports the failure
- **AND** the daemon continues serving every other command

### Requirement: Files are written where the platform puts them

Murmly SHALL resolve its configuration, data, cache, and runtime locations by the
convention of the resolved platform, and MUST NOT write to a location that belongs
to another platform's convention. Where a platform's convention defines an
environment override, that override SHALL be honoured. `murmly doctor` SHALL report
the path actually in use for each, because a report of the default is worthless on
a machine where the default is not what is in use.

A location Murmly needs and cannot create or write SHALL be reported by naming that
location and what failed, rather than by failing somewhere later that does not
mention it.

#### Scenario: Each location follows the platform

- **WHEN** Murmly resolves its configuration, data, cache, and runtime locations
- **THEN** each is the location that platform's own convention specifies
- **AND** diagnostics report the path in use for each

#### Scenario: The platform's override is honoured

- **WHEN** the environment sets the override that platform's convention defines for
  one of those locations
- **THEN** Murmly uses the overridden location
- **AND** diagnostics report the overridden path rather than the default

#### Scenario: A location cannot be written

- **WHEN** a location Murmly needs cannot be created or written
- **THEN** the failure names that location and what went wrong

### Requirement: The permissions a platform requires are stated before they are needed and reported afterwards

Where a platform gates a capability behind a permission the person must grant,
`murmly install` SHALL state which permissions will be requested and what each is
for, before requesting any of them, and `murmly doctor` SHALL report for each
whether it is granted, denied, or could not be determined.

A denied permission SHALL be reported as denied, naming the capability it gates and
where the person grants it. Murmly MUST NOT report a capability as working on the
strength of the mechanism being present when the permission it needs has been
denied, because a denied permission on some platforms makes the mechanism succeed
silently while nothing happens, and that is indistinguishable from a defect in
Murmly.

Murmly MUST NOT attempt to grant a permission on the person's behalf or change any
system setting to obtain one.

#### Scenario: Installation states what will be asked for

- **WHEN** installation runs on a platform that gates capture, injection, or hotkey
  registration behind a permission
- **THEN** it states which permissions will be requested and what each enables
- **AND** it does so before the first request is made

#### Scenario: A denied permission is reported as denied

- **WHEN** diagnostics run where a required permission has been denied
- **THEN** the report states that it is denied, names the capability it gates, and
  names where to grant it
- **AND** does not report that capability as available

#### Scenario: A permission whose state cannot be read

- **WHEN** the platform offers no way to read whether a permission is granted
- **THEN** the report states that it could not be determined
- **AND** does not claim it is granted

#### Scenario: Murmly does not grant permissions itself

- **WHEN** a required permission is not granted
- **THEN** Murmly changes no system setting to obtain it
- **AND** reports the grant as something for the person to perform

### Requirement: What does not depend on the platform is identical on every platform

The command protocol and its response codes, the speech session protocol, the
configuration file format and every option name and default in it, the transcript
pipeline from capture to delivery, the command-line surface, and the field names in
the diagnostics report SHALL be the same on every supported platform. Only the
values of platform-dependent fields, and the presence of a concern the platform
cannot serve, may differ.

A client written against one platform therefore works against another without
change, and configuration copied between machines means the same thing on both.

#### Scenario: A client written on one platform drives another

- **WHEN** a client that speaks the command protocol connects to a daemon on a
  different platform than it was written against
- **THEN** the same commands are accepted and the same response codes are returned

#### Scenario: Configuration moves between platforms

- **WHEN** a configuration file written on one platform is used on another
- **THEN** every option in it is recognised and means the same thing
- **AND** only values naming a platform-specific location need to differ

#### Scenario: The diagnostics report keeps its shape

- **WHEN** diagnostics run on any supported platform
- **THEN** the report carries the same field names as on every other
- **AND** a field for a concern this platform cannot serve reports it as unavailable
  rather than being absent
