## MODIFIED Requirements

### Requirement: Diagnostics report paste injection support

`murmly doctor` SHALL report whether Murmly can inject a paste in the active session and which method it would use. When it cannot inject, the report MUST name what the user has to install, enable, or grant to make injection work in this session, and MUST distinguish an injection tool that is absent from one that is installed but unusable here, and both from one that is installed and usable but whose required permission has not been granted.

Those three states have three different remedies, and only the first is fixed by installing something. A method the platform gates behind a permission MUST NOT be reported as available while that permission is ungranted, because on some platforms such a method reports success while nothing reaches the focused window — which is precisely the failure a person would otherwise spend their time attributing to Murmly.

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

#### Scenario: Method present but its permission is not granted

- **WHEN** diagnostics run where an injection method exists and could be executed
  but the permission the platform requires for it has not been granted
- **THEN** the report states that the permission is what is missing
- **AND** names where the person grants it
- **AND** does not report paste injection as available

#### Scenario: The permission state cannot be read

- **WHEN** diagnostics run where the platform offers no way to read whether that
  permission is granted
- **THEN** the report states that it could not be determined
- **AND** does not report paste injection as available on the strength of the
  method being present
