## MODIFIED Requirements

### Requirement: The page discloses what Murmly requires before it asks for an install

The page SHALL state, in a place a visitor reaches before or alongside the install instructions and without following a link off the page, which operating systems Murmly runs on, that Python 3.12 or newer is required, that installation is run from a terminal, which permissions the visitor's platform will ask them to grant, and which parts of Murmly are not automatic on their platform — for each platform, whether the hotkey registers itself and whether the overlay is presented. Presenting Murmly as broadly consumer-ready MUST NOT come at the cost of omitting any of these.

Where a capability has been verified end to end on some platforms and not others, the page MUST say which, and MUST NOT let a verified platform's status stand for an unverified one. A visitor MUST be able to determine, from the page alone and without installing anything, what will and will not work on the machine they are reading it on.

#### Scenario: A visitor reads the install section

- **WHEN** the install instructions are read on the page
- **THEN** the supported operating systems, the Python version floor, the terminal requirement, the permissions each platform asks for, and the per-platform hotkey and overlay status are each stated on the same page

#### Scenario: A visitor on an unsupported desktop

- **WHEN** someone running a desktop on which Murmly cannot register a hotkey reads the page
- **THEN** they can determine from the page alone that the hotkey will not register itself on their desktop
- **AND** that the rest of Murmly still installs and works

#### Scenario: A visitor checks whether their platform is verified

- **WHEN** someone reads the page on a platform where a capability has not been verified end to end
- **THEN** the page states which platforms that capability is verified on
- **AND** does not present the unverified platform as verified
