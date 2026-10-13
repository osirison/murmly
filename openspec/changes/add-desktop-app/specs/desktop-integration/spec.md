# Spec Delta

## ADDED Requirements

### Requirement: Installing adds an app shortcut

Installation SHALL add an applications-menu entry that opens the Murmly app, and uninstallation MUST remove it with everything else it removes. The entry MUST be visible in the menu, carry a name, an icon and a category, and run the app by absolute path. Only Linux has such an entry. It is skipped only when the toolkit is missing, which a sync prevents.

#### Scenario: Installing on Linux

- **WHEN** installation runs on Linux after a sync
- **THEN** a visible applications-menu entry that runs the environment's `murmly app` by absolute path is added
- **AND** uninstallation removes it

#### Scenario: Installing before the toolkit is there

- **WHEN** installation runs and PySide6 is not installed
- **THEN** no applications-menu entry is added
- **AND** installing never fails because of it

### Requirement: Syncing always installs the app's toolkit

`murmly sync` SHALL install the desktop app's toolkit on every run on Linux and Windows, whether or not the environment held it before, and MUST NOT offer a way to leave it out. It MUST still reapply the GPU runtime swap afterwards. The one exception is a Linux system whose glibc is older than the toolkit's wheels need, which MUST be told so plainly and synced without it.

#### Scenario: Syncing an environment without the toolkit

- **WHEN** `murmly sync` runs on an environment with no PySide6, and nobody has answered any prompt
- **THEN** the sync installs it

#### Scenario: Syncing a GPU environment

- **WHEN** `murmly sync` runs on an environment with the GPU runtime and the GPU build of ONNX Runtime
- **THEN** the sync carries the GPU extra and the app's toolkit together
- **AND** the GPU build of ONNX Runtime is back afterwards

#### Scenario: A system too old for the toolkit

- **WHEN** `murmly sync` runs on a system whose glibc is older than the oldest toolkit wheel for its architecture
- **THEN** it says that the app needs a newer glibc and is not installed
- **AND** the sync completes without the app

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
