## MODIFIED Requirements

### Requirement: Diagnostics report residency and configuration

Murmly's diagnostics SHALL report, for the transcription model and for the
synthesis session, whether it is currently resident and what idle period is in
effect. Residency SHALL be reported for the models the running daemon holds, not
for the process producing the report, because those are different processes and
only the daemon's answer describes the system.

When no running daemon can be asked, diagnostics MUST say so rather than report
the models as not resident. "Nothing is holding a model" and "nobody was there to
ask" are different facts, and reporting the second as the first makes the report
wrong exactly when a person is checking whether their daemon is running.

Reporting residency MUST NOT itself load a model that is not loaded. Diagnostics
MAY include other sections that load a model, and any such section MUST state
that it does.

A daemon that cannot answer, or that answers unusably, MUST NOT prevent the rest
of the report. The reason MUST be named alongside the affected fields.

#### Scenario: Residency reported for the daemon's models

- **GIVEN** a daemon is running and holds the transcription model
- **WHEN** the user runs diagnostics
- **THEN** the report says the transcription model is resident
- **AND** it does so whether or not the process producing the report holds anything

#### Scenario: A released model is reported as released

- **GIVEN** a daemon is running and its transcription model has been released after its idle period
- **WHEN** the user runs diagnostics
- **THEN** the report says the transcription model is not resident
- **AND** names the configured idle period

#### Scenario: No daemon to ask is not the same as not resident

- **GIVEN** no daemon is running
- **WHEN** the user runs diagnostics
- **THEN** the report states that residency could not be determined because no daemon answered
- **AND** it does not report either model as resident or as not resident

#### Scenario: Residency reported without loading

- **GIVEN** no model has been loaded since the daemon started
- **WHEN** the user runs diagnostics
- **THEN** the output reports both as not resident and names each configured idle period
- **AND** reporting residency loads neither model

#### Scenario: A section that loads a model says so

- **GIVEN** a diagnostics section measures something that requires a loaded model
- **WHEN** that section runs
- **THEN** the report states that the measurement loaded a model
- **AND** the residency it reports is what was held before that section ran

#### Scenario: A daemon that cannot be asked does not abandon the report

- **GIVEN** a daemon is running but does not answer the residency question
- **WHEN** the user runs diagnostics
- **THEN** the report names why residency could not be determined
- **AND** every other section is still reported
