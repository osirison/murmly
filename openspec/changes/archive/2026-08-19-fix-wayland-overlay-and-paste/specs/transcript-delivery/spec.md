## ADDED Requirements

### Requirement: Injection method is chosen by what the session can execute

Murmly SHALL choose a paste injection method that the active session can actually execute, and MUST NOT choose a method the session cannot support merely because the tool that implements it is installed. When more than one installed method exists, Murmly MUST prefer one it has determined the session can execute over one it has not.

#### Scenario: An installed method the session cannot support

- **WHEN** a paste injection tool is installed but the active session cannot execute it
- **THEN** Murmly does not select that method for delivery
- **AND** Murmly selects an installed method the session can execute, if one exists

#### Scenario: A usable method exists

- **WHEN** at least one installed injection method can be executed in the active session
- **THEN** Murmly injects the paste with that method

#### Scenario: No method can be executed

- **WHEN** no installed injection method can be executed in the active session
- **THEN** Murmly reports paste injection as unavailable for the session
- **AND** delivery follows the rules for an unavailable injection method

### Requirement: An undeliverable transcript is never lost to an injector failure

When no usable paste injection method is available, or the selected method fails while delivering, Murmly SHALL treat that delivery as refused under the existing refusal rules rather than as an error. The transcript MUST be on the clipboard, the caller MUST be told it was copied but not pasted, and the previous clipboard contents MUST NOT be restored over it. Murmly MUST be able to copy a transcript in a session where it cannot inject a paste at all.

#### Scenario: No injection method available

- **WHEN** a transcript is produced in a session with no usable injection method
- **THEN** the transcript is on the clipboard
- **AND** the toggle response reports that the transcript was copied but not pasted
- **AND** the response does not report the toggle as failed

#### Scenario: Injection fails during delivery

- **WHEN** the selected injection method fails while delivering a transcript
- **THEN** the transcript is on the clipboard
- **AND** the toggle response reports that the transcript was copied but not pasted

#### Scenario: Continuous session with no injection method

- **WHEN** a segment cannot be injected during a continuous auto-transcribe session because no usable injection method is available
- **THEN** Murmly stops capture and returns to idle as it does for any refused delivery
- **AND** that segment's transcript is on the clipboard

#### Scenario: Overlay is told without transcript content

- **WHEN** delivery is not injected because no usable injection method is available
- **THEN** the overlay shows its existing error state
- **AND** the message sent to the overlay carries no transcript text

### Requirement: Diagnostics report paste injection support

`murmly doctor` SHALL report whether Murmly can inject a paste in the active session and which method it would use. When it cannot inject, the report MUST name what the user has to install or enable to make injection work in this session, and MUST distinguish an injection tool that is absent from one that is installed but unusable here.

#### Scenario: Injection available

- **WHEN** diagnostics run in a session where Murmly can inject a paste
- **THEN** the report names the injection method Murmly would use

#### Scenario: No injection tool installed

- **WHEN** diagnostics run in a session where no injection tool is installed
- **THEN** the report states that transcripts will be copied but not pasted
- **AND** names what to install for this session

#### Scenario: Installed tool is unusable in this session

- **WHEN** diagnostics run in a session where an installed injection tool cannot be executed
- **THEN** the report states that the installed tool cannot be used in this session
- **AND** names what to install or enable instead

### Requirement: An unconfirmable injection method must not overwrite the transcript

Some injection methods report success whether or not the keystroke reached the focused window, so Murmly cannot tell delivery from silent failure. When delivery was attempted with such a method, Murmly SHALL leave the transcript on the clipboard and MUST NOT restore the previous contents over it, whatever the restoration setting says. Restoration remains as configured for a method whose failure Murmly can observe.

#### Scenario: Delivery by a method whose success cannot be observed

- **WHEN** a transcript is injected with a method that cannot confirm the keystroke arrived
- **THEN** the transcript is still the clipboard contents after Murmly returns to idle
- **AND** the contents present before capture are not restored
- **AND** Murmly does not wait out the restoration interval

#### Scenario: Delivery by a method whose failure is observable

- **WHEN** a transcript is injected with a method that reports its failures
- **THEN** clipboard restoration behaves as configured
