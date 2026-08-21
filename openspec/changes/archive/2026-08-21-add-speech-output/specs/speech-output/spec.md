## Purpose

Defines how Murmly turns text into audible speech: opting in, the session that carries text in and playback events out on one connection, the order and units speech is produced in, what happens when the person interrupts, where an interrupted session's transcript goes, what speech signals may not contain, and what diagnostics report about all of it.

## ADDED Requirements

### Requirement: Speech output is opt-in

Murmly SHALL NOT produce audible speech unless speech output is enabled in configuration, and it MUST default to disabled. An installation that gains a speech capability by upgrade MUST behave exactly as it did before until the user enables it, because a machine that starts talking after an update is producing sound its owner did not ask for.

#### Scenario: Speech output disabled

- **WHEN** speech output is disabled and a caller opens a speech session
- **THEN** Murmly refuses the session with a code identifying speech output as not enabled
- **AND** no audio device is opened

#### Scenario: Speech output enabled

- **WHEN** speech output is enabled and a caller opens a speech session
- **THEN** Murmly accepts the session
- **AND** text sent on it is spoken

#### Scenario: Upgrade does not enable speech

- **WHEN** an installation that predates speech output is upgraded and its configuration is unchanged
- **THEN** Murmly produces no speech
- **AND** every existing command behaves as it did before the upgrade

### Requirement: A speech session carries text in and playback events out on one connection

A caller SHALL be able to declare a connection a speech session, after which Murmly exchanges many frames with it in both directions for as long as it stays open. Text sent earlier MUST be spoken earlier. Murmly MUST send playback events on that connection without being asked for them, because the events a session needs most — that the person interrupted — are caused by someone other than the session.

A session MUST be able to state that it has no more text to send, and Murmly MUST NOT report that everything has been heard until it has. Murmly cannot otherwise distinguish a queue that is empty because the sender is still thinking from one that is empty because the exchange is over.

#### Scenario: Text sent over time is spoken in order

- **WHEN** a session sends several pieces of text one after another while speech is already playing
- **THEN** each is spoken in the order it was sent
- **AND** speech continues without a gap between them beyond the pause the text itself calls for

#### Scenario: Session is told which message started

- **WHEN** playback of a piece of text begins
- **THEN** the session receives an event naming that piece
- **AND** the event arrives without the session having requested it

#### Scenario: Session states it has finished sending

- **WHEN** a session states that it has no more text to send and all queued speech has been heard
- **THEN** the session receives an event reporting that everything queued was heard

#### Scenario: Queue empties while the sender is still sending

- **WHEN** all queued speech has been heard and the session has not stated that it has finished sending
- **THEN** Murmly does not report that everything was heard
- **AND** Murmly speaks the next text the session sends

#### Scenario: One-shot commands are unaffected

- **WHEN** a caller sends a command without declaring a speech session
- **THEN** it receives exactly one response and the connection closes, as it did before speech output existed

### Requirement: Speech stops when its session's connection ends

Murmly SHALL stop speech and discard anything that session queued when the session's connection closes for any reason. A sender that has gone away cannot be told what was heard and cannot be interrupted by the person, so speech that outlives its sender is speech nobody can stop through the interface that produced it.

#### Scenario: Session closes while speech is playing

- **WHEN** a session's connection closes while its text is being spoken
- **THEN** speech stops
- **AND** text that session queued and that has not been spoken is discarded

#### Scenario: Session closes after everything was heard

- **WHEN** a session's connection closes after all its text has been spoken
- **THEN** Murmly returns to idle and no audio device remains open

### Requirement: Speech is produced in sentence units with the pauses between them preserved

Murmly SHALL begin speaking before it has produced audio for all the text it was given, and SHALL do so by producing speech in units no larger than a sentence. Speech produced this way MUST carry the same pauses between sentences that the passage would have if it were produced as a whole, because producing sentences independently otherwise drops the silence between them and the passage runs together.

The delay between receiving text and the first audible sound MUST NOT grow with the length of that text.

#### Scenario: Long text begins speaking promptly

- **WHEN** a session sends a passage of many sentences
- **THEN** speech begins after approximately the same delay as it would for a single sentence

#### Scenario: Sentence boundaries keep their pauses

- **WHEN** a passage of several sentences is spoken
- **THEN** the silence between its sentences matches what the same passage carries when produced as a whole

### Requirement: A hotkey press stops speech before capture begins

When a hotkey that starts capture is pressed while speech is playing, Murmly SHALL stop the speech before opening the microphone, and MUST NOT have both running at once. A microphone open while Murmly is speaking records Murmly's own voice, which would be transcribed as though the person had said it.

Both capture hotkeys MUST behave this way. They differ only in where the resulting transcript is delivered.

#### Scenario: Capture hotkey pressed during speech

- **WHEN** a capture hotkey is pressed while speech is playing
- **THEN** speech stops before the microphone opens
- **AND** the recording contains none of Murmly's own speech

#### Scenario: Capture hotkey pressed while silent

- **WHEN** a capture hotkey is pressed and no speech is playing
- **THEN** capture begins as it does today

#### Scenario: Speech is held while capture is running

- **WHEN** a session sends text while capture is running
- **THEN** Murmly holds that text rather than speaking over the person
- **AND** speaks it once capture ends

### Requirement: An interrupted session is told what was not heard

When speech is stopped before everything queued has been spoken, Murmly SHALL send that session an event naming the piece of text that was playing and every piece that had not started. The event MUST report what was played rather than what was produced, and MUST be sent before any transcript that follows the interruption. A sender that is not told it was cut off keeps producing text for a person who has stopped listening.

The position Murmly reports MUST be no finer than the piece of text it was given, because audio already handed to the output device is heard after Murmly stops sending it and no finer position is honest.

#### Scenario: Person interrupts mid-passage

- **WHEN** a capture hotkey is pressed while the second of four queued pieces of text is being spoken
- **THEN** the session is told that the second piece was interrupted
- **AND** is told that the third and fourth were never started

#### Scenario: Interruption arrives before any transcript

- **WHEN** a capture hotkey is pressed during speech and a transcript is later produced for that session
- **THEN** the session receives the interruption event before it receives the transcript

#### Scenario: Position reflects what was heard

- **WHEN** speech is stopped while Murmly has produced audio further ahead than the person has heard
- **THEN** the interruption names the piece the person was hearing, not the piece Murmly had produced

#### Scenario: Nothing was left unheard

- **WHEN** speech is stopped at the moment the last queued piece finishes
- **THEN** the session is told nothing remained unheard

### Requirement: A transcript produced inside a speech session is delivered to that session

When capture is started by the hotkey designated for the open speech session, Murmly SHALL deliver the resulting transcript to that session and MUST NOT paste it or record a window as its target. The window holding focus during a voice exchange is not the intended recipient, and pasting a spoken reply into it puts the person's words somewhere they did not choose.

When such a transcript cannot reach a session — because none was open when capture started, or because the session closed before the transcript was produced — Murmly MUST place it on the clipboard and report that it was not delivered. It MUST NOT paste it. The person still said the words, so losing them is not an acceptable outcome, but the destination they chose no longer exists and Murmly MUST NOT substitute one for it.

When capture is started by the hotkey designated for the focused window, the transcript MUST be delivered exactly as it is delivered today, whether or not a speech session is open.

#### Scenario: Session hotkey during an open session

- **WHEN** the session hotkey starts capture while a speech session is open and a transcript is produced
- **THEN** the transcript is delivered to that session
- **AND** nothing is pasted and the clipboard is unchanged

#### Scenario: Window hotkey during an open session

- **WHEN** the window hotkey starts capture while a speech session is open and a transcript is produced
- **THEN** the transcript is delivered to the focused window as it is today
- **AND** the session is not sent that transcript

#### Scenario: Session hotkey with no session open

- **WHEN** the session hotkey starts capture and no speech session is open
- **THEN** Murmly reports that there is no session to deliver to
- **AND** the transcript is placed on the clipboard and not pasted anywhere

#### Scenario: Session closes before its transcript is ready

- **WHEN** a session's connection closes after capture ends but before its transcript is produced
- **THEN** Murmly reports that the transcript could not be delivered
- **AND** the transcript is placed on the clipboard rather than pasted into whatever window then holds focus

### Requirement: Voice and speech settings are configurable and bounded

Murmly SHALL let the voice, the speaking rate, and the output device be configured, MUST state a default for each, and MUST fall back to that default when a configured value is unrecognized or outside the supported range rather than refusing to start. Speech settings are cosmetic, and a misconfigured voice is not a reason to leave the person without a working daemon.

#### Scenario: Unrecognized voice

- **WHEN** the configured voice is not one Murmly can produce
- **THEN** Murmly uses the default voice
- **AND** diagnostics report the configured value and the one in use

#### Scenario: Rate outside the supported bounds

- **WHEN** the configured speaking rate is outside the supported range
- **THEN** Murmly uses the default rate

#### Scenario: Configured output device unavailable

- **WHEN** the configured output device cannot be opened
- **THEN** Murmly reports which device it could not open and what it used instead

### Requirement: Speech output unavailable is reported rather than fatal

When speech output is enabled but cannot run — its runtime is absent, its model files are missing, or no output device can be opened — Murmly SHALL start, refuse speech sessions with a reason, and continue serving transcription unchanged. A missing synthesis dependency MUST NOT prevent capture, delivery, or any existing command from working.

#### Scenario: Synthesis runtime absent

- **WHEN** speech output is enabled and its runtime is not installed
- **THEN** Murmly starts and reports speech output as unavailable, naming what to install
- **AND** capture, transcription, and delivery work unchanged

#### Scenario: No output device

- **WHEN** speech output is enabled and no output device can be opened
- **THEN** Murmly refuses speech sessions with a reason naming the device problem
- **AND** continues serving every other command

### Requirement: Speech signals exclude the text being spoken

Events, log entries, and command responses concerning speech SHALL NOT carry the text being spoken, and MUST NOT carry a transcript produced inside a session. They identify text by the name the session gave it. One exception is stated explicitly: the transcript delivered to the session that asked for it, which is the whole point of delivering it.

#### Scenario: Playback events name text without quoting it

- **WHEN** a playback or interruption event is sent
- **THEN** it identifies the affected text by name
- **AND** carries none of that text

#### Scenario: Logs exclude spoken text

- **WHEN** Murmly logs a speech failure
- **THEN** the entry names the failure and the affected text by name
- **AND** contains neither the spoken text nor any transcript

### Requirement: Diagnostics report speech output configuration and availability

`murmly doctor` SHALL report whether speech output is enabled, whether it can run, the voice and rate in use alongside any configured values that were not honoured, and the output device it would use, alongside its existing sections. When speech output cannot run, the report MUST name the remedy.

#### Scenario: Speech output enabled and working

- **WHEN** diagnostics run with speech output enabled and able to run
- **THEN** the report states that speech output is available and names the voice, rate, and output device in use

#### Scenario: Speech output disabled

- **WHEN** diagnostics run with speech output disabled
- **THEN** the report states that speech output is disabled

#### Scenario: Speech output enabled but unable to run

- **WHEN** diagnostics run with speech output enabled and its runtime or model files absent
- **THEN** the report states that speech output is unavailable
- **AND** names what to install or place to make it available

#### Scenario: One probe failing does not abandon the report

- **WHEN** the speech output probe fails unexpectedly
- **THEN** the report states that the speech section could not be determined
- **AND** every other section is still reported
