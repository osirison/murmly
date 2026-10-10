# Spec Delta

## Purpose

Defines Murmly's desktop app — the window, the tray panel and first-run setup —
through which a person sees what Murmly is doing, changes its settings, checks its
health and removes it, without needing a terminal.

## ADDED Requirements

### Requirement: The app is optional on every supported platform

Murmly SHALL provide the desktop app on every platform it supports. The app MUST be optional: capture, transcription and delivery MUST behave identically whether the app is installed, running, or closed, and closing the app window MUST NOT stop the daemon.

#### Scenario: Dictation without the app installed

- **WHEN** Murmly is installed without the app and the person presses the dictation key
- **THEN** capture, transcription and delivery behave exactly as they do with the app installed

#### Scenario: Closing the window

- **WHEN** the person closes the app window
- **THEN** the daemon keeps running
- **AND** the next press of the dictation key starts capture as usual

### Requirement: The app reflects the daemon's state

The app SHALL show the daemon's current state — ready, listening, writing, needs you, or service not running — and MUST show a change of state within one second of it happening.

#### Scenario: Capture starts from the hotkey

- **WHEN** the person presses the dictation key while the app is open
- **THEN** the app shows that Murmly is listening within one second

#### Scenario: The service is not running

- **WHEN** the app cannot reach the daemon over the command socket
- **THEN** the app says that the background service is not running
- **AND** offers to start it

### Requirement: The app never takes focus from dictation

Murmly MUST NOT raise the app window, give it keyboard focus, or otherwise change which window holds focus as a result of capture, transcription, delivery, or any change of state.

#### Scenario: Dictating with the app open behind another window

- **WHEN** the app is open behind the window the person is dictating into, and capture starts, stops and delivers
- **THEN** the window that held focus when capture started still holds it
- **AND** the transcript is delivered to it as it would be with the app closed

### Requirement: The last transcript is held in memory only

Murmly SHALL keep the most recent transcript, delivered or refused, in daemon memory so the app can show it and copy it again. It MUST NOT be written to disk or to any log, MUST be discarded when the daemon exits, and MUST be clearable from the app.

#### Scenario: Shown after delivery

- **WHEN** a transcript is delivered and the person opens the app
- **THEN** the app shows that transcript as the last words, with when it was produced and the outcome of its delivery

#### Scenario: Gone after the daemon restarts

- **WHEN** the daemon exits and starts again
- **THEN** the app shows no last words until the next transcript is produced

#### Scenario: Cleared by the person

- **WHEN** the person clears the last words in the app
- **THEN** the daemon no longer holds that transcript
- **AND** the app shows no last words

### Requirement: The last transcript is available only to its owner

The daemon SHALL answer a request for the last transcript only over the command socket, and therefore only to the account that owns it. The request MUST NOT be answered by any other channel, and MUST NOT appear in the daemon's logs with the transcript's text.

#### Scenario: Another account asks

- **WHEN** a process running as another account tries to request the last transcript
- **THEN** it cannot connect to the command socket and receives nothing

#### Scenario: The owner asks

- **WHEN** the app, running as the owning account, requests the last transcript
- **THEN** the daemon answers with the transcript text
- **AND** no log entry contains that text

### Requirement: A refused delivery is recoverable from the app

When a delivery is refused, the app SHALL say that the transcript is on the clipboard before anything else, SHALL show the detail string the daemon reported exactly as reported, and SHALL offer to copy the transcript again.

#### Scenario: Focus moved before delivery

- **WHEN** a delivery is refused because another window was in front at delivery time
- **THEN** the app says that the words are on the clipboard
- **AND** it shows the daemon's detail string unaltered
- **AND** choosing to copy again places the transcript on the clipboard

### Requirement: Settings are written without losing what the person wrote

Changing a setting in the app SHALL write only that key to the configuration file Murmly is using, keeping every comment, key order and unrelated value. A value outside its documented range MUST be refused in the app and MUST NOT be written.

#### Scenario: Comments survive a change

- **WHEN** the person changes one setting in a configuration file that contains comments
- **THEN** the file afterwards differs only in that setting's value
- **AND** every comment is still present

#### Scenario: A value outside its range

- **WHEN** the person enters a value outside the setting's documented range
- **THEN** the app refuses it and says what range is accepted
- **AND** the configuration file is unchanged

### Requirement: A configuration file that does not parse is never overwritten

If the configuration file does not parse, or has changed on disk since the app read it, the app MUST NOT write to it. It SHALL show the parse error exactly as reported, or say that the file changed, and leave the file untouched.

#### Scenario: The file has a syntax error

- **WHEN** the configuration file cannot be parsed and the person changes a setting
- **THEN** the app shows the parse error
- **AND** the file on disk is byte-for-byte unchanged

#### Scenario: The file was edited by hand meanwhile

- **WHEN** the configuration file changes on disk after the app read it and the person then changes a setting
- **THEN** the app re-reads the file before writing, rather than writing over the hand edit

### Requirement: Settings take effect without a manual restart

After a setting that needs a restart is saved, Murmly SHALL restart its service once nothing is being captured, transcribed or spoken, and MUST NOT interrupt a recording or an utterance to do so.

#### Scenario: Saved while idle

- **WHEN** a setting is saved while Murmly is idle
- **THEN** the service restarts and the new value is in effect within ten seconds

#### Scenario: Saved while recording

- **WHEN** a setting is saved while capture is running
- **THEN** the recording continues and is delivered as usual
- **AND** the service restarts only after Murmly returns to idle

### Requirement: Health presents the diagnostics report

The app's Health section SHALL present every section `murmly doctor` reports, each as a plain sentence beside the raw key and value it came from, and SHALL offer the full report for copying. The copied report MUST NOT contain transcript text.

#### Scenario: Everything works

- **WHEN** the person opens Health and every check passes
- **THEN** each check is listed with a plain sentence and its raw key and value

#### Scenario: Something does not

- **WHEN** a section reports a problem, such as speech output being unavailable
- **THEN** that section is listed with the problem in a plain sentence and its raw key and value

#### Scenario: Copying the report

- **WHEN** the person copies the full report after dictating
- **THEN** the copied text contains every section of the report and no transcript text

### Requirement: First run sets up the dictation key

On first run the app SHALL let the person choose the dictation key by pressing it, SHALL check it with the same rules `murmly install` applies, and MUST refuse a key another application already holds, naming that application where the platform can.

#### Scenario: A free key with a modifier

- **WHEN** the person presses a combination that includes Meta, Ctrl or Alt and that no other application holds
- **THEN** the app shows it and binds it as the dictation key

#### Scenario: A key without a modifier

- **WHEN** the person presses a single key with no Meta, Ctrl or Alt
- **THEN** the app refuses it and asks for a modifier

#### Scenario: A key another application holds

- **WHEN** the person presses a combination another application holds
- **THEN** the app refuses it and names that application where the platform reports it

### Requirement: Key recording is confined to the app

Recording a key in the app MUST capture keystrokes only while its key field has focus and the person has asked to record. The app MUST NOT install a system-wide keyboard hook to do so.

#### Scenario: Typing elsewhere while the app is open

- **WHEN** the app is open and the person types into another application
- **THEN** the app records none of those keystrokes

### Requirement: The tray panel mirrors Home at a smaller scale

Where the platform offers a system tray, the app SHALL provide a tray icon whose panel shows the current state, the last words, and quick switches for live transcription, auto-transcribe, assistant announcements and quiet hours. Where no tray is available, the app MUST remain reachable from its window.

#### Scenario: A desktop with a tray

- **WHEN** the person opens the tray panel
- **THEN** it shows the current state, the last words and the quick switches
- **AND** changing a switch saves that setting as Settings would

#### Scenario: A desktop without a tray

- **WHEN** the app starts on a desktop that offers no system tray
- **THEN** the app runs without a tray icon
- **AND** Health says that no tray was found

### Requirement: Murmly can be removed from the app

The app SHALL offer to remove Murmly, performing the same removal as `murmly uninstall`, and MUST first ask for confirmation, naming what will be removed. Deleting settings and downloaded models MUST be a separate, explicit choice.

#### Scenario: Removing Murmly

- **WHEN** the person confirms removal in the app
- **THEN** everything `murmly uninstall` removes is removed, including the app's own shortcut
- **AND** settings and downloaded models are kept

#### Scenario: Removing everything

- **WHEN** the person also chooses to delete settings and downloaded models, and confirms
- **THEN** those are deleted as `murmly uninstall --purge` deletes them

### Requirement: The app follows the system's appearance and is operable by keyboard

The app SHALL follow the system's light or dark preference and its reduced-motion preference, and every control MUST be reachable and operable from the keyboard with a visible focus indicator.

#### Scenario: The system switches to light

- **WHEN** the system's appearance changes from dark to light while the app is open
- **THEN** the app switches to its light theme

#### Scenario: Keyboard only

- **WHEN** the person moves through the app with Tab and operates controls with Space and Enter
- **THEN** every control is reached and operable, and the focused control is visibly marked
