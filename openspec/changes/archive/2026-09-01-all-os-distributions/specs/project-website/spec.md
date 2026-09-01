## MODIFIED Requirements

### Requirement: The page discloses what Murmly requires before it asks for an install

Wherever the site presents instructions for installing Murmly, that same page SHALL
state, without requiring the visitor to follow a link away from it, which operating
systems Murmly runs on, that Python 3.12 or newer is required, that installation is
run from a terminal, which permissions the visitor's platform will ask them to
grant, and which parts of Murmly are not automatic on their platform — for each
platform, whether the hotkey registers itself and whether the overlay is presented.
Presenting Murmly as broadly consumer-ready MUST NOT come at the cost of omitting
any of these on any page that invites an install.

Where a capability has been verified end to end on some platforms and not others,
the page MUST say which, and MUST NOT let a verified platform's status stand for an
unverified one. A visitor MUST be able to determine, from that page alone and
without installing anything, what will and will not work on the machine they are
reading it on.

#### Scenario: A visitor reads the install section

- **WHEN** install instructions are read on any page of the site
- **THEN** the supported operating systems, the Python version floor, the terminal
  requirement, the permissions each platform asks for, and the per-platform hotkey
  and overlay status are each stated on that same page

#### Scenario: A visitor on an unsupported desktop

- **WHEN** someone running a desktop on which Murmly cannot register a hotkey reads
  any page that invites them to install
- **THEN** they can determine from that page alone that the hotkey will not register
  itself on their desktop
- **AND** that the rest of Murmly still installs and works

#### Scenario: An install command appears on a documentation page

- **WHEN** a documentation page shows an install command
- **THEN** those same disclosures appear on that page as well, not only on the
  landing page

#### Scenario: A visitor checks whether their platform is verified

- **WHEN** someone reads a page that invites an install on a platform where a
  capability has not been verified end to end
- **THEN** the page states which platforms that capability is verified on
- **AND** does not present the unverified platform as verified

### Requirement: README.md is a short entry point rather than a reference manual

`README.md` SHALL state in plain language what Murmly is, state the requirements a
reader must meet before installing — including which operating systems Murmly runs
on, which machines it cannot run on and why, and which platforms each capability is
verified end to end on — give the command that installs it, show what using it
looks like, and link to the published site as the place all further documentation
lives. The link to the site SHALL appear near the top and SHALL address the
published site rather than a file path in the repository.

`README.md` MUST NOT carry the configuration reference, the speech-session protocol,
the command socket's permission rules, troubleshooting steps, or any other reference
material the documentation pages hold, because a second copy drifts from the first.
It SHALL nonetheless remain complete enough that a reader who never leaves the
repository can install Murmly and produce one transcript.

#### Scenario: README.md is read from top to bottom

- **WHEN** `README.md` is read from top to bottom
- **THEN** it states what Murmly is, states the requirements, gives the install
  command, shows a first use, and links to the published site

#### Scenario: README.md is searched for reference material

- **WHEN** `README.md` is searched for a configuration option, the speech-session
  protocol, or a troubleshooting step
- **THEN** it links to the site's documentation instead of reproducing it

#### Scenario: A reader follows README.md without opening the site

- **GIVEN** a reader who follows `README.md` without opening the site
- **WHEN** they follow it to the end
- **THEN** they have installed Murmly and produced one transcript

#### Scenario: README.md is measured after the move

- **WHEN** `README.md` is measured after the move
- **THEN** it is a small fraction of the 966 lines it held before
- **AND** no section of reference material that moved to the site remains duplicated
  in it

#### Scenario: The link to the site is followed

- **WHEN** the link to the site in `README.md` is followed
- **THEN** it resolves to `https://osirison.github.io/murmly/`
