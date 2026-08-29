## ADDED Requirements

### Requirement: An announcement that is not spoken makes no sound

An announcement SHALL make no sound at all when it is not going to be spoken. Any sound that precedes the words — a signal that an announcement is arriving — MUST NOT be produced unless Murmly has undertaken to speak. Every reason an announcement stays silent is covered by this, whether the reason is that speech output is disabled or unavailable, that another caller holds the session, that the microphone is open, or that the agent marked nothing to be heard.

A sound announcing words that never arrive is worse than silence. It tells a person who is not looking at the terminal to stop and listen, spends their attention, and returns nothing for it. Repeated once per turn it trains them to ignore the signal, which costs the announcements that do work.

The undertaking to speak MUST be established before the signal is produced rather than after, and it MUST reflect what Murmly can do at that moment rather than what it could do earlier.

#### Scenario: Speech output cannot run

- **WHEN** a turn ends and speech output is enabled but cannot run
- **THEN** no sound of any kind is produced for that turn
- **AND** the turn completes as though the announcement were not installed

#### Scenario: The session is refused

- **WHEN** a turn ends and Murmly refuses the announcement a speech session, for any reason
- **THEN** no sound of any kind is produced for that turn

#### Scenario: Nothing was marked to be heard

- **WHEN** a turn ends with an announcement suppressed by the agent
- **THEN** no sound of any kind is produced for that turn

#### Scenario: An announcement that will be spoken

- **WHEN** a turn ends and Murmly has undertaken to speak the announcement
- **THEN** the signal is produced and the announcement follows it

#### Scenario: Speech becomes unavailable between turns

- **GIVEN** a daemon that has been announcing turns aloud
- **WHEN** speech output stops being able to run and a further turn ends
- **THEN** that turn produces no sound of any kind
- **AND** the reason is available in the daemon's diagnostics
