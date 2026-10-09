## Purpose

Defines how Murmly tells voices apart: enrolling people by name, delivering only the owner's speech or labelling everyone's speech by speaker, what happens when recognition cannot run, how voiceprints are stored and deleted, and how the speaker model is held and reported.

## ADDED Requirements

### Requirement: Speaker mode is opt-in

Murmly SHALL provide a configuration option selecting one of three speaker modes — off, mine-only, or label-everyone — and that option MUST default to off. While the mode is off, the daemon MUST NOT read voiceprints or load the speaker model, and MUST deliver exactly the transcript it would deliver without this capability. This restriction SHALL apply to the daemon only: enrolment, the voice commands, and diagnostics work in every mode.

#### Scenario: Configuration does not mention a speaker mode

- **WHEN** capture starts with no speaker mode in configuration
- **THEN** the delivered transcript is the same as it would be without this capability
- **AND** the daemon reads no voiceprint and loads no speaker model

#### Scenario: Enrolling while the mode is off

- **WHEN** the user enrols a voice while the speaker mode is off
- **THEN** the voice is enrolled
- **AND** the daemon's transcripts are unchanged until a speaker mode is selected

#### Scenario: Voices are enrolled but the mode is off

- **GIVEN** one or more voices are enrolled
- **WHEN** the speaker mode is off and several people speak during a recording
- **THEN** everything the microphone heard is delivered, without labels

### Requirement: An unrecognized speaker mode falls back to off

A configured speaker mode that Murmly does not recognize MUST be treated as off and MUST NOT prevent the daemon from starting. Murmly SHALL report the rejected value through diagnostics.

#### Scenario: Unrecognized mode configured

- **WHEN** configuration names a speaker mode Murmly does not recognize
- **THEN** the daemon starts and treats the speaker mode as off
- **AND** diagnostics report the rejected value

### Requirement: The match threshold is configurable and bounded

Murmly SHALL provide a configurable match threshold that sets how closely a voice must match an enrolled voiceprint, or an unknown voice heard earlier, to count as the same person. The threshold MUST be bounded. A value outside the bounds, or one that is not a number, MUST fall back to the default without preventing the daemon from starting, and Murmly SHALL report the rejected value through diagnostics.

#### Scenario: Threshold within the bounds

- **WHEN** a match threshold within the supported bounds is configured
- **THEN** Murmly uses that threshold when attributing speech to speakers

#### Scenario: Threshold outside the bounds

- **WHEN** the configured match threshold is below the supported minimum, above the supported maximum, or not a number
- **THEN** the daemon starts and uses the default threshold
- **AND** diagnostics report the rejected value

### Requirement: A voice is enrolled by name from a recorded sample

Murmly SHALL provide a command that enrols a person under a name: it records a short voice sample from the microphone in the command's own process, derives a voiceprint from it, and stores the voiceprint under that name. The command MUST report where the voiceprint was stored and how to remove it. Enrolling a name that is already enrolled MUST replace that voiceprint and say so.

#### Scenario: Enrolling a new voice

- **WHEN** the user runs the enrolment command with a new name and speaks for the length of the sample
- **THEN** a voiceprint is stored under that name
- **AND** the command reports where it was stored and how to remove it

#### Scenario: Enrolling a name that is already enrolled

- **GIVEN** a voice is already enrolled under a name
- **WHEN** the user enrols that name again and the sample is accepted
- **THEN** the new voiceprint replaces the earlier one
- **AND** the command says that it replaced an existing voiceprint

### Requirement: Enrolment is refused while the daemon is busy

Enrolment MUST be refused, storing nothing, when a running daemon reports that it is listening, transcribing, or speaking, so that a sample never mixes the person's voice with dictation in progress or with Murmly's own speech. When no daemon answers, enrolment SHALL proceed.

#### Scenario: Daemon is listening

- **GIVEN** the daemon is capturing a recording
- **WHEN** the user runs the enrolment command
- **THEN** the command refuses without recording, says why, and exits non-zero
- **AND** no voiceprint is stored or changed

#### Scenario: No daemon is running

- **WHEN** the user runs the enrolment command and no daemon answers
- **THEN** the command records the sample and enrols the voice

### Requirement: An enrolment sample without enough speech is refused

Enrolment MUST be refused, storing nothing, when the recorded sample contains less speech than the supported minimum. This includes a sample from a muted or silent microphone. The refusal MUST say that too little speech was heard and MUST exit non-zero.

#### Scenario: Silent microphone

- **WHEN** the enrolment sample contains no speech
- **THEN** the command reports that too little speech was heard and exits non-zero
- **AND** no voiceprint is stored or changed

#### Scenario: Too little speech

- **WHEN** the enrolment sample contains some speech but less than the supported minimum
- **THEN** the command reports that too little speech was heard and exits non-zero
- **AND** no voiceprint is stored or changed

### Requirement: Enrolment without the speaker model is refused with its remedy

When the speaker model is absent or cannot be loaded, enrolment MUST be refused before anything is recorded, storing nothing. The refusal SHALL name the model file Murmly looked for and say how to obtain it.

#### Scenario: Model file is missing

- **WHEN** the user runs the enrolment command and the speaker model file is not present
- **THEN** the command records nothing, names the file it looked for and how to obtain it, and exits non-zero

### Requirement: Enrolled names are validated

Enrolled names are typed into documents as labels, so Murmly MUST refuse a name that is empty, longer than the supported maximum, or contains a line break, a control character, or a colon. It MUST also refuse a name equal, in any letter case, to the owner label or to an unknown-speaker label. Names SHALL be compared without regard to letter case, so two enrolled voices never share a name.

#### Scenario: Name with a line break

- **WHEN** the user tries to enrol a name containing a line break
- **THEN** the command refuses the name before recording and exits non-zero

#### Scenario: Name that would read as a label

- **WHEN** the user tries to enrol the name "you" or "speaker 2"
- **THEN** the command refuses the name before recording and exits non-zero

#### Scenario: Same name in different letter case

- **GIVEN** a voice is enrolled as "Milo"
- **WHEN** the user enrols "milo"
- **THEN** it is treated as the same name and replaces the voiceprint enrolled as "Milo"

### Requirement: One enrolled voice is the owner

Murmly SHALL provide a configuration option naming the owner, and the enrolled voice with that name is the owner. When the option is absent, or names no enrolled voice, there SHALL be no owner. The owner is the person whose speech mine-only delivers and whom label-everyone labels as You.

#### Scenario: Owner configured and enrolled

- **GIVEN** the owner option names a voice that is enrolled
- **WHEN** a recording is processed in a speaker mode
- **THEN** speech matching that voice is attributed to the owner

#### Scenario: Owner configured but not enrolled

- **GIVEN** the owner option names a voice that is not enrolled
- **WHEN** diagnostics run
- **THEN** the report states that the configured owner is not enrolled

### Requirement: Mine-only delivers only the owner's speech

In mine-only mode Murmly SHALL deliver only the parts of a transcript attributed to the owner, in the order they were spoken and without labels. It MUST drop the parts attributed to any other enrolled person or to an unknown voice, including speech from a television or a loudspeaker. This holds except where this capability delivers a transcript as it would with the speaker mode off.

#### Scenario: Owner dictates with a television on

- **WHEN** the owner and a television are both heard during a recording in mine-only mode
- **THEN** only the owner's words are delivered
- **AND** none of the television's words are delivered

#### Scenario: Another enrolled person speaks

- **WHEN** an enrolled person who is not the owner speaks during a recording in mine-only mode
- **THEN** that person's words are not delivered

#### Scenario: Only the owner speaks

- **WHEN** only the owner speaks during a recording in mine-only mode
- **THEN** the delivered transcript is the same as it would be with the speaker mode off

### Requirement: Label-everyone labels each speaker's part

In label-everyone mode Murmly SHALL deliver every part of a transcript, each preceded by its speaker's label: You for the owner, the enrolled name for any other enrolled person, and Speaker followed by a number for an unknown voice. Unknown voices MUST be numbered from 1 in the order in which they are first heard. Labels are omitted only where this capability delivers a transcript as it would with the speaker mode off.

#### Scenario: Owner and an enrolled person

- **GIVEN** the owner is enrolled and another person is enrolled as "Milo"
- **WHEN** the owner speaks and then Milo speaks during a recording in label-everyone mode
- **THEN** the owner's words are labelled "You:" and Milo's words are labelled "Milo:"

#### Scenario: Two unknown voices

- **WHEN** two voices that match no enrolled person speak in turn during a recording in label-everyone mode
- **THEN** the first voice heard is labelled "Speaker 1:" and the second "Speaker 2:"

#### Scenario: No owner configured

- **GIVEN** voices are enrolled and there is no owner
- **WHEN** a recording is processed in label-everyone mode
- **THEN** enrolled people are labelled by their enrolled names
- **AND** no part is labelled "You:"

### Requirement: A labelled transcript stays on one line

A labelled transcript MUST contain no line break. Each speaker's part SHALL be written as the label, a colon, a space, and the words, and consecutive parts SHALL be separated by a single space, the separator Murmly already uses between the parts of a transcript.

#### Scenario: Two speakers in one recording

- **WHEN** the owner says "Shall we start?" and an unknown voice replies "Yes, go ahead." in label-everyone mode
- **THEN** the delivered transcript is "You: Shall we start? Speaker 1: Yes, go ahead."
- **AND** it contains no line break

### Requirement: Consecutive parts from one speaker share one label

Within one delivered transcript, consecutive parts attributed to the same speaker MUST be joined under a single label. In label-everyone mode every delivered transcript SHALL begin with a label, including each segment of a continuous session, except a transcript this capability delivers as it would with the speaker mode off.

#### Scenario: Owner speaks twice in a row

- **WHEN** the owner says two sentences and an unknown voice then says one, all in one recording
- **THEN** the owner's two sentences appear after a single "You:" label
- **AND** the unknown voice's sentence follows its own label

#### Scenario: A continuous segment continues the previous speaker

- **WHEN** a continuous session delivers a segment and the next segment begins with the same speaker who ended the previous one
- **THEN** the next delivered segment still begins with that speaker's label

### Requirement: Unknown-speaker numbers hold for a whole capture session

Murmly SHALL keep an unknown voice's number for the whole capture session, so a voice numbered in one segment of a continuous session keeps that number in later segments. Numbering MUST restart from 1 when a new capture session begins, and unknown voices MUST NOT be remembered beyond the session that heard them.

#### Scenario: Same stranger in two segments

- **WHEN** an unknown voice labelled "Speaker 1:" in one segment of a continuous session speaks again in a later segment of that session
- **THEN** it is labelled "Speaker 1:" again

#### Scenario: New capture session

- **WHEN** a capture session ends and a new one begins
- **THEN** the first unknown voice heard in the new session is labelled "Speaker 1:"

### Requirement: A part too short to identify takes a neighbouring speaker

A part of a recording or segment that is too short to identify reliably SHALL be attributed to the speaker of the nearest identifiable part in the same recording or segment, preferring the part before it. When no part of a recording or segment can be identified, Murmly MUST deliver it as it would with the speaker mode off.

#### Scenario: Owner answers briefly after speaking

- **WHEN** in mine-only mode the owner says a sentence and then a single short word in the same recording
- **THEN** the short word is attributed to the owner and delivered with the sentence

#### Scenario: Short word between other speakers

- **WHEN** in mine-only mode a single short word falls between two sentences spoken by an unknown voice in the same recording
- **THEN** the short word is attributed to that voice and is not delivered

#### Scenario: Recording too short to identify

- **WHEN** a recording or segment contains only a single short word such as "yes", in either speaker mode
- **THEN** it is delivered as it would be with the speaker mode off

### Requirement: Speaker mode applies to every delivered transcript

The speaker mode SHALL apply to every transcript Murmly delivers, whether capture is bound for the focused window or for a speech session, and whether the recording ends by a toggle, by stop mode, or as a segment of a continuous session. Where a toggle response reports transcript text, that text SHALL be the transcript as delivered, after filtering and labelling.

#### Scenario: Reply to a speech session in mine-only mode

- **WHEN** capture bound for a speech session hears the owner and another voice in mine-only mode
- **THEN** the speech session receives only the owner's words

#### Scenario: Toggle response in label-everyone mode

- **WHEN** a window-bound recording in label-everyone mode is delivered and its toggle response reports transcript text
- **THEN** that text is the labelled transcript that was delivered

### Requirement: A transcript left empty by mine-only is treated as no transcript

When mine-only drops every part of a recording or segment, Murmly SHALL treat it as a transcription that yielded no text: nothing is delivered, the clipboard is left as it was, and the outcome MUST NOT be reported as a refused delivery. In a continuous session the session MUST keep listening, and the dropped segment MUST NOT count as a segment of the session.

#### Scenario: Recording with only other voices

- **WHEN** a recording in mine-only mode contains speech from other people and none from the owner
- **THEN** nothing is delivered and the clipboard is left exactly as it was before capture started
- **AND** no delivery failure is signalled

#### Scenario: Continuous segment with only other voices

- **WHEN** a segment of a continuous session in mine-only mode contains no speech from the owner
- **THEN** nothing is delivered for that segment
- **AND** capture continues for the next segment
- **AND** the toggle that ends the session reports neither the dropped segment's text nor a segment for it

### Requirement: Speaker recognition that cannot run is disabled for that capture

When the selected speaker mode cannot run, Murmly SHALL deliver the transcript as it would with the mode off, MUST log a warning that names the reason and carries no transcript text, and diagnostics SHALL report the reason. Capture, transcription, and delivery MUST continue. Mine-only cannot run without an owner; neither mode can run without a usable speaker model.

#### Scenario: Speaker model missing

- **WHEN** capture starts in either speaker mode and the speaker model file is not present
- **THEN** the transcript is delivered as it would be with the speaker mode off
- **AND** the daemon logs a warning naming the missing model
- **AND** diagnostics report that speaker recognition cannot run and why

#### Scenario: Mine-only without an owner

- **WHEN** capture starts in mine-only mode and there is no owner
- **THEN** the transcript is delivered as it would be with the speaker mode off
- **AND** diagnostics report that mine-only needs an enrolled owner

#### Scenario: Label-everyone with no voices enrolled

- **WHEN** a recording is processed in label-everyone mode with a usable speaker model and no voices enrolled
- **THEN** every voice is labelled as an unknown speaker by number

### Requirement: A speaker failure never loses a transcript

When attributing speakers fails while a recording or segment is being processed, Murmly MUST deliver that transcript as it would with the speaker mode off. The failure MUST NOT fail the recording, end a continuous session, or discard the text, and Murmly MUST log it without transcript text.

#### Scenario: Attribution raises an error

- **WHEN** attributing speakers raises an error while a recording is being processed
- **THEN** the transcript is delivered as it would be with the speaker mode off
- **AND** the daemon logs the failure without any transcript text

### Requirement: Voiceprints are used only with the model that made them

Each stored voiceprint SHALL record which speaker model produced it. Murmly MUST NOT compare a voiceprint with the output of a different model. Voiceprints made with a different model MUST be treated as not enrolled, and diagnostics SHALL say that those voices need to be enrolled again.

#### Scenario: Speaker model replaced

- **GIVEN** voices were enrolled with one speaker model and a different speaker model is now installed
- **WHEN** diagnostics run
- **THEN** the report states that those voices must be enrolled again
- **AND** recordings are processed as if those voices were not enrolled

### Requirement: Speaker attribution never runs on partial results

Speaker attribution SHALL apply only to the transcript that is delivered. Murmly MUST NOT attribute speakers for partial results, so the overlay shows partial text from every voice the microphone hears, in every speaker mode.

#### Scenario: Live transcription in mine-only mode

- **WHEN** live transcription is enabled in mine-only mode and another voice speaks while Murmly is listening
- **THEN** the overlay's partial text may include that voice's words
- **AND** the delivered transcript does not include them

### Requirement: Only the voiceprint is kept from enrolment

Enrolment SHALL keep only the voiceprint derived from the sample. Murmly MUST NOT write the enrolment audio to any file or log entry, and MUST NOT send the sample or the voiceprint off the machine.

#### Scenario: After a successful enrolment

- **WHEN** an enrolment completes
- **THEN** the voiceprint is the only thing stored from it
- **AND** no audio from the sample exists in any file Murmly wrote

### Requirement: Voiceprints are private to the account

Murmly SHALL store voiceprints in its data location for the resolved platform, in a file readable and writable only by the account that runs Murmly, and MUST NOT write them anywhere else. Diagnostics SHALL report the path in use.

#### Scenario: Voiceprint file created

- **WHEN** the first voice is enrolled
- **THEN** the voiceprint file is created in Murmly's data location
- **AND** no other account can read or write it

#### Scenario: Path reported

- **WHEN** diagnostics run
- **THEN** the report names the voiceprint file path in use

### Requirement: Enrolled voices can be listed and removed

Murmly SHALL provide commands that list the enrolled names, marking the owner, and that remove one enrolled voice or all of them. Removing a voice MUST delete its voiceprint from storage, and removing all voices MUST leave no voiceprint behind. Removing a name that is not enrolled MUST be reported and exit non-zero.

#### Scenario: Remove one voice

- **GIVEN** voices are enrolled as "Milo" and "Ana"
- **WHEN** the user removes "Milo"
- **THEN** Milo's voiceprint no longer exists in storage
- **AND** Ana's voiceprint is unchanged

#### Scenario: Remove all voices

- **WHEN** the user removes all enrolled voices
- **THEN** no voiceprint remains in storage

#### Scenario: Remove a name that is not enrolled

- **WHEN** the user removes a name that is not enrolled
- **THEN** the command says that name is not enrolled and exits non-zero

### Requirement: The daemon uses the voiceprints stored when capture begins

A running daemon SHALL use the voiceprints as stored at the moment each capture session begins, so that enrolling or removing a voice takes effect from the next capture without restarting the daemon. A removed voiceprint MUST NOT remain in the daemon's memory after the next capture session begins.

#### Scenario: Voice enrolled while the daemon runs

- **GIVEN** the daemon is running and idle
- **WHEN** a voice is enrolled and the user then starts a capture
- **THEN** that capture recognises the newly enrolled voice

#### Scenario: Voice removed while the daemon runs

- **GIVEN** the daemon is running and a voice is enrolled
- **WHEN** that voice is removed and the user then starts a capture
- **THEN** that capture does not recognise the removed voice

### Requirement: Speaker names never accompany transcript text in signals or logs

Signals Murmly emits about delivery outcomes MUST carry no speaker name. Murmly MUST NOT write a log entry that carries a speaker name together with transcript text. A toggle response is not such a signal: it reports the delivered transcript, labels included.

#### Scenario: Delivery outcome signalled to the overlay

- **WHEN** a labelled transcript is delivered or refused and the overlay is active
- **THEN** the message sent to the overlay about that outcome carries no speaker name and no transcript text

#### Scenario: Speech dropped in mine-only mode

- **WHEN** mine-only drops speech and Murmly writes a log entry about it
- **THEN** the entry carries neither the dropped words nor the transcript text alongside any speaker name

### Requirement: The speaker model is loaded only when a speaker mode needs it

The daemon MUST NOT load the speaker model when it starts or while the speaker mode is off. With a speaker mode selected, the daemon SHALL begin loading the model when capture begins, and loading MUST NOT delay the start of capture. The enrolment command loads the model in its own process, in every mode.

#### Scenario: Daemon starts with a speaker mode selected

- **WHEN** the daemon starts with a speaker mode selected
- **THEN** the speaker model is not loaded until a capture begins

#### Scenario: Capture begins while the model loads

- **WHEN** capture begins in a speaker mode and the speaker model is not loaded
- **THEN** capture starts without waiting for the model to load

### Requirement: The speaker model runs on the CPU

The speaker model SHALL run on the CPU and MUST NOT hold accelerator memory, whatever processor the transcription model or the synthesis session is configured to use.

#### Scenario: Transcription on an accelerator

- **WHEN** the transcription model runs on an accelerator and the speaker model is loaded
- **THEN** the speaker model holds no accelerator memory

### Requirement: The speaker model is released with the transcription model

Murmly SHALL release the speaker model when the transcription model's idle period elapses, and SHALL ask the platform to return the freed memory where the platform allows it. Murmly MUST NOT release the speaker model while it is attributing speakers. A released speaker model SHALL be loaded again at the next capture without user action.

#### Scenario: Idle period elapses

- **GIVEN** the speaker model and the transcription model are both resident
- **WHEN** no capture has been active for longer than the transcription idle period
- **THEN** Murmly releases both

#### Scenario: Release disabled for transcription

- **GIVEN** the transcription idle period is configured as zero
- **WHEN** Murmly is left idle indefinitely after the speaker model was loaded
- **THEN** the speaker model stays resident

#### Scenario: Capture after a release

- **GIVEN** the speaker model has been released
- **WHEN** the user records in a speaker mode
- **THEN** Murmly loads the speaker model again and the transcript is filtered or labelled as before the release

### Requirement: Diagnostics report speaker recognition configuration and availability

`murmly doctor` SHALL report a speaker recognition section with the speaker mode and match threshold in effect, any rejected value for either, the configured owner and whether that owner is enrolled, the number and names of enrolled voices, the voiceprint and model file paths, and whether speaker recognition can run, with the reason when it cannot.

#### Scenario: Defaults in effect

- **WHEN** diagnostics run with no speaker settings in configuration
- **THEN** the report states that the speaker mode is off
- **AND** the section is still present with the threshold in effect and the number of enrolled voices

#### Scenario: Speaker mode selected without a usable model

- **WHEN** diagnostics run with a speaker mode selected and the speaker model file missing
- **THEN** the report states that speaker recognition cannot run and names the missing file

### Requirement: Diagnostics never reveal a voiceprint

Diagnostics and every command's output MUST NOT include the contents of a voiceprint. What Murmly reports about enrolled voices SHALL be limited to their names, which one is the owner, and how many there are.

#### Scenario: Diagnostics with voices enrolled

- **WHEN** diagnostics run with voices enrolled
- **THEN** the report lists their names and count
- **AND** contains no voiceprint values

### Requirement: Diagnostics report the speaker model's residency without loading it

Diagnostics SHALL report whether the running daemon holds the speaker model and MUST NOT load the model to answer. When no daemon can be asked, the report MUST state that residency could not be determined rather than report the model as not resident.

#### Scenario: Daemon holds the speaker model

- **GIVEN** a daemon is running and holds the speaker model
- **WHEN** diagnostics run
- **THEN** the report says the speaker model is resident

#### Scenario: No daemon to ask

- **GIVEN** no daemon is running
- **WHEN** diagnostics run
- **THEN** the report states that the speaker model's residency could not be determined because no daemon answered

### Requirement: The speaker recognition section keeps its shape

The speaker recognition section of the diagnostics report SHALL carry the same field names on every supported platform and in every speaker mode. A field that does not apply MUST be reported as empty rather than left out.

#### Scenario: Speaker mode off

- **WHEN** diagnostics run with the speaker mode off
- **THEN** the speaker recognition section carries the same field names as when a speaker mode is selected
