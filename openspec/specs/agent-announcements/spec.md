# Agent Announcements Specification

## Purpose
Defines what a coding agent's finished turn is announced as: how an agent marks the
passage it wrote to be heard, what is spoken when one is present and when none is,
how that convention reaches an agent without editing the person's own instruction
files, and the rule that none of it may fail or delay the turn it is announcing.

## Requirements

### Requirement: An agent marks the passage written to be heard

An agent SHALL be able to mark one passage of its final message as the passage to be
spoken, by enclosing it in a `<voice-note>` element. When such a passage is present,
Murmly SHALL announce it and MUST NOT announce any other part of that message.

The rest of the message stays on the screen. It is written for someone reading it,
and the reason for marking a passage at all is that the marked one was not.

#### Scenario: A marked passage is what is announced

- **WHEN** an agent's final message contains a `<voice-note>` passage surrounded by
  ordinary prose, headings, and code
- **THEN** the announcement is that passage
- **AND** none of the surrounding message is spoken

#### Scenario: The screen keeps the whole message

- **WHEN** a message containing a marked passage is announced
- **THEN** nothing about what the agent displayed is altered by the announcement

#### Scenario: Several marked passages

- **WHEN** a message contains more than one `<voice-note>` passage
- **THEN** they are announced in the order they appear in the message
- **AND** they are announced as one continuous passage rather than as separate
  announcements

### Requirement: A marked passage is announced as written

Murmly SHALL announce a marked passage as the agent wrote it, and MUST NOT reduce it
to its opening sentences. The limits that exist to bound an extract MUST NOT be
applied to it, because those limits exist to stop where an extract is no longer
informative, and a passage that was authored to be heard has no such point.

A marked passage SHALL still be subject to an upper bound of its own, materially
larger than the bound applied to an extract, so that a passage of unbounded length
cannot hold the speech session against the person. When a passage exceeds that bound,
Murmly SHALL announce it up to a sentence boundary rather than stopping mid-word.

Markup inside a marked passage SHALL be removed before it is spoken, on the same
terms as everywhere else. An agent is asked for prose, and removal is what makes a
path or an identifier it slipped in anyway audible rather than punctuation read aloud.

#### Scenario: A passage longer than an extract is announced in full

- **WHEN** a marked passage is longer than the limit that bounds an extract
- **AND** it is within its own upper bound
- **THEN** all of it is announced

#### Scenario: A passage beyond its own bound stops at a sentence

- **WHEN** a marked passage exceeds its upper bound
- **THEN** the announcement ends at a sentence boundary at or before that bound
- **AND** no word is cut in half

#### Scenario: Markup inside a marked passage

- **WHEN** a marked passage contains inline code, a link, or emphasis
- **THEN** the text of those is announced without their markers

### Requirement: A message with no marked passage is announced as it is today

When an agent's final message contains no `<voice-note>` passage, Murmly SHALL
extract what it announces from that message exactly as it did before marked passages
existed. An installation that is upgraded and whose agent has not been told the
convention MUST hear the same extract, taken the same way, at the same moment, and
MUST hear nothing on the same occasions — subject only to that message being the
finished turn's, which the requirement below governs and which an upgrade corrects.

#### Scenario: No marked passage

- **WHEN** an agent's final message contains no `<voice-note>` passage
- **THEN** the announcement is the same extract of that message that Murmly announced
  before marked passages existed

#### Scenario: Upgrade does not change an uninstructed agent

- **GIVEN** an agent that has not been told the convention
- **WHEN** it finishes a turn after Murmly is upgraded
- **THEN** what is announced is the same extract, of that turn's message, that Murmly
  would have produced before the upgrade
- **AND** the upgrade changes which turn's message that is, and nothing else

#### Scenario: A passage that was never closed

- **WHEN** a message opens a `<voice-note>` element and never closes it
- **THEN** Murmly announces the message as it would one with no marked passage

### Requirement: The announcement is of the turn that just finished

Murmly SHALL announce the agent's message from the turn whose ending caused the
announcement, and MUST NOT announce a message from any earlier turn. Where the agent
hands Murmly that message along with the announcement, Murmly SHALL take it from
there in preference to any record written alongside the conversation, because such a
record is not guaranteed to contain the finished turn's message at the moment that
turn ends.

Where no message is handed over, Murmly SHALL fall back to that record. The fallback
is what keeps an agent that supplies no message announced at all, and it is the only
circumstance in which an announcement may be of whatever the record last holds.

#### Scenario: The record lags behind the turn

- **WHEN** the announcement is made and the conversation's record does not yet contain
  the finished turn's message
- **THEN** Murmly announces the finished turn's message
- **AND** Murmly does not announce the previous turn's message

#### Scenario: A marked passage in the finished turn

- **WHEN** the finished turn's message contains a marked passage and the previous
  turn's message contains a different one
- **THEN** the passage from the finished turn is announced

#### Scenario: The first turn of a session

- **WHEN** the announcement is made for the first turn of a session, before the record
  holds any agent message at all
- **THEN** that turn's message is announced
- **AND** the announcement is not skipped for want of anything to say

#### Scenario: No message is handed over

- **WHEN** the agent supplies no message alongside the announcement
- **THEN** Murmly falls back to the conversation's record
- **AND** announces the last agent message that record holds

### Requirement: An empty marked passage suppresses the announcement

When an agent marks a passage and leaves it empty, Murmly SHALL announce nothing for
that turn and MUST NOT fall back to announcing an extract. An agent that marked a
passage knows the convention, so an empty one is a decision that this turn is not
worth interrupting the person for, not an absence of one.

#### Scenario: Empty marked passage

- **WHEN** a message contains a `<voice-note>` passage with no text in it
- **THEN** nothing is announced
- **AND** no extract of that message is announced in its place

#### Scenario: Suppression is recorded

- **WHEN** an announcement is suppressed by an empty marked passage
- **THEN** the diagnostic record states that it was suppressed rather than that there
  was nothing to say

### Requirement: The convention reaches the agent without editing the person's files

Registering the announcement SHALL also install, for each agent that can be told
things automatically, whatever tells that agent to write a marked passage. That
installation MUST NOT write into any file that holds the person's own instructions to
their agent. Unregistering SHALL remove it, and registering more than once SHALL
leave exactly one of it, on the same terms as the announcement itself.

An agent that offers no way to be told automatically SHALL still be announced. Only
the instruction is left to the person, and the documentation MUST give them the exact
text to place and say where it goes.

#### Scenario: Registering installs the instruction

- **WHEN** the announcement is registered for an agent that can be told automatically
- **THEN** that agent is told the convention from its next session onward
- **AND** no file holding the person's own instructions is written to

#### Scenario: Registering twice

- **WHEN** the announcement is registered twice for the same agent
- **THEN** exactly one instruction registration remains

#### Scenario: Unregistering removes the instruction

- **WHEN** the announcement is unregistered
- **THEN** the instruction registration is removed alongside it
- **AND** configuration belonging to anything other than Murmly is left as it was

#### Scenario: An agent that cannot be told automatically

- **GIVEN** an agent with no facility for being given instructions by an installer
- **WHEN** the announcement is registered for it
- **THEN** its turns are still announced
- **AND** it announces a marked passage once the person has placed the documented
  instruction themselves

### Requirement: Nothing installed for announcements may fail, and only the instruction may delay anything

Every hook Murmly registers with an agent SHALL exit successfully whatever it
encounters. This holds for reading a message it cannot parse, for a marked passage
that is malformed, and for every reason the announcement stays silent.

The announcement SHALL NOT hold up the turn it announces.

Whatever tells the agent the convention SHALL run before the session it instructs
begins, because there is no later point at which it can be in that session's
context. It MUST therefore cost no more than emitting a fixed piece of text: it
opens no connection, reads no configuration, and starts no subprocess. That is the
whole of what may delay anything, and it is bounded by construction rather than by
hope.

#### Scenario: A message that cannot be read

- **WHEN** the announcement runs against a message it cannot parse
- **THEN** it exits successfully
- **AND** the agent's turn completes as though the announcement were not installed

#### Scenario: The instruction cannot be delivered

- **WHEN** whatever tells the agent the convention fails for any reason
- **THEN** the agent's session starts normally
- **AND** the failure is not reported as an error to the person

#### Scenario: The turn is not held up

- **WHEN** an announcement is made for a turn
- **THEN** the agent is free to continue or exit without waiting for it to be spoken

#### Scenario: The instruction does no work

- **WHEN** whatever tells the agent the convention runs
- **THEN** it performs no socket, network, configuration, or subprocess work
- **AND** the session begins without a wait the person notices

### Requirement: The diagnostic record says which announcement was made

When a diagnostic record of announcements is being kept, it SHALL state whether what
was announced was a passage the agent marked or an extract taken from the message.
Without that, an agent that is silently not writing marked passages is indistinguishable
from one that is.

#### Scenario: A marked passage was announced

- **WHEN** an announcement is made from a marked passage
- **THEN** the record states that the passage was the agent's own

#### Scenario: An extract was announced

- **WHEN** an announcement is made with no marked passage present
- **THEN** the record states that it was an extract
