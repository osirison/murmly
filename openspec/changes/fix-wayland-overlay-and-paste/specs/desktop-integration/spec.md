## ADDED Requirements

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
