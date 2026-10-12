# Spec Delta

## ADDED Requirements

### Requirement: Installing adds an app shortcut when the app can run

Installation SHALL add an applications-menu entry that opens the Murmly app when the desktop app can run on the computer, and MUST NOT add one when it cannot. Uninstallation MUST remove that entry with everything else it removes. Only Linux has such an entry.

#### Scenario: The app is available

- **WHEN** installation runs on Linux and the app can run
- **THEN** an applications-menu entry that runs `murmly app` is added
- **AND** uninstallation removes it

#### Scenario: The app is not available

- **WHEN** installation runs and the app cannot run
- **THEN** no applications-menu entry is added

### Requirement: Uninstall can also delete what Murmly keeps

Uninstallation SHALL delete Murmly's settings, downloaded models and enrolled voices only when asked to purge, and MUST name the folders it will delete and ask first. A purge MUST leave the Python environment and any announcement hook script in place.

#### Scenario: Uninstalling without purge

- **WHEN** uninstallation runs without purge
- **THEN** settings, downloaded models and enrolled voices are kept

#### Scenario: Uninstalling with purge

- **WHEN** uninstallation runs with purge and the person confirms
- **THEN** the settings folder and the data folder are deleted, except an announcement hook script

#### Scenario: Purge not confirmed

- **WHEN** uninstallation runs with purge and nobody confirms
- **THEN** everything is removed except settings, models and voices, which are kept
