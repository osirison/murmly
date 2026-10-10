# Typing only your own voice

Murmly can tell voices apart. It can leave out everyone but you, so a
television or a colleague in the room does not end up in your document. Or it
can type everyone and say who spoke.

This is off by default. Nothing here runs until you set it up. This page takes
you from nothing to working, in order:

1. [Get the speaker model](#get-the-model)
2. [Enrol your voice](#enrol)
3. [Choose a mode](#configure)
4. [Restart murmly](#restart)
5. [Check that it can run](#check)
6. [Use it](#use)

After that: [add other people](#other-people), [turn it off](#turn-off), and
[fix a problem](#problems).

Commands below start with `murmly`. If your terminal does not find it, run
`uv run murmly` from the murmly folder instead.

## 1. Get the speaker model { #get-the-model }

Telling voices apart needs one extra file, the speaker model. Setup downloads
it for you, whether or not you use speaker recognition. It is fetched when you
run `install` or `upgrade`.

On Linux, from the murmly folder:

```bash
./setup.sh install Meta+X
```

or, if murmly is already installed:

```bash
./setup.sh upgrade
```

On Windows, in PowerShell, from the murmly folder:

```powershell
.\bootstrap.ps1 install Meta+X
```

or, if murmly is already installed:

```powershell
.\bootstrap.ps1 upgrade
```

These are the commands from [installing murmly](install.md). Use your own
hotkey in place of `Meta+X`. Setup
checks the file against a known checksum and tells you if it does not match.
On Windows, `upgrade` does only this: it downloads the model, or confirms the
one you have, and reports a failure if the file is still missing or wrong
afterwards. It changes nothing else.

Check that the file is there:

```bash
murmly doctor
```

In the `speaker_recognition` section, `model_present` and
`model_checksum_matches` must both be `true`. The `model_path` line shows where
the file is.

### Download the model by hand { #manual-model }

Do this only if setup could not fetch the file. The file is called
`voxceleb_resnet34_LM.onnx` and is about 26 MB. Download it from this address:

```text
https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx
```

Put it in murmly's data folder:

| Platform | Put the file in |
| --- | --- |
| Linux | `~/.local/share/murmly/` (or `$XDG_DATA_HOME/murmly/` when you set that variable) |
| Windows | `%LOCALAPPDATA%\murmly\` |

Then check that it is the right file. Its SHA-256 must be
`7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068`.

```bash
sha256sum ~/.local/share/murmly/voxceleb_resnet34_LM.onnx
```

```powershell
Get-FileHash "$env:LOCALAPPDATA\murmly\voxceleb_resnet34_LM.onnx" -Algorithm SHA256
```

If the two do not match, delete the file and download it again.

## 2. Enrol your voice { #enrol }

Murmly knows a voice only after you enrol it. Enrolling records a short sample,
turns it into a voiceprint, and stores the voiceprint under a name.

Before you start:

- Use **the microphone you dictate with every day**. A voice sounds different
  through different microphones.
- Be in a quiet room, with nothing playing.
- Make sure murmly is idle. It refuses to enrol while it is listening,
  transcribing or speaking, so wait until any spoken reply has finished.

Then run this, with your own name:

```bash
murmly enrol Milo
```

Murmly prints a short passage and says `Recording now.` It records for 20
seconds. Read the passage aloud at a relaxed pace. It is shorter than the
recording, so **keep reading until the recording stops**. If you reach the end,
start again from the top. When murmly prints `Recording finished`, stop talking.
Murmly needs at least 10 seconds of speech in the sample.

When it works, you see `Enrolled "Milo".`, the place the voiceprint is stored,
and the command that removes it. If no owner is set yet, murmly also prints the
two lines to add to your settings, which is the next step.

About the name:

- It has at most 32 characters.
- It cannot contain a colon or a line break.
- It cannot be `You`, or `Speaker` followed by a number. Murmly uses those as
  labels.

Enrolling a name that already exists replaces its voiceprint. If you change
microphones, enrol again.

## 3. Choose a mode { #configure }

Open your settings file. [All the settings](settings.md#where-the-file-lives)
says where it is, and `murmly doctor` reports the path in use. Add a
`[speakers]` block. To type only your words:

```toml
[speakers]
mode = "mine-only"
owner = "Milo"
```

To type everyone and label who spoke:

```toml
[speakers]
mode = "label-everyone"
owner = "Milo"
```

`owner` is the name you enrolled, spelled the same way. These are the settings:

| Setting | What it does |
| --- | --- |
| [`mode`](settings.md#speakers-mode) | `off`, `mine-only` or `label-everyone` |
| [`owner`](settings.md#speakers-owner) | The enrolled name that is you |
| [`match_threshold`](settings.md#speakers-match-threshold) | How alike a voice must sound to count as an enrolled one. Default `30`, the lowest allowed, up to `90` |

You can leave `match_threshold` out. The modes:

| Mode | What murmly types |
| --- | --- |
| `off` | Every voice, as it always has. |
| `mine-only` | Only the owner's words, with no labels. Other enrolled people, and voices murmly does not know, are left out. |
| `label-everyone` | Every voice, each part labelled. |

In `label-everyone`, the owner is labelled `You:`. Another enrolled person is
labelled with their name. A voice murmly does not know is labelled
`Speaker 1:`, `Speaker 2:` and so on, in the order they first speak.

The result is one line. Each label is followed by a colon and a space, and
parts are separated by a single space. If you say "Shall we start?" and an
unknown voice replies "Yes, go ahead.", murmly types:

```text
You: Shall we start? Speaker 1: Yes, go ahead.
```

Consecutive parts from the same voice share one label. Numbers stay the same
for the whole recording, and through a whole
[continuous session](pause-to-finish.md).

Murmly reads these settings **when it starts**. Saving the file changes nothing
until you restart it.

## 4. Restart murmly { #restart }

On Linux:

```bash
systemctl --user restart murmly.service
```

On Windows, murmly runs as a Task Scheduler task named `MurmlyDaemon`. Stop it
and start it again from a terminal:

```powershell
schtasks /end /tn MurmlyDaemon
schtasks /run /tn MurmlyDaemon
```

Signing out of Windows and back in also starts it again.

## 5. Check that it can run { #check }

**Do this every time you change the setup.** When murmly cannot run a speaker
mode, it does not stop you. It types everything, as if the mode were `off`, and
writes one warning to its log. Nothing appears on the overlay or in the pasted
text. So a setup that does not work looks like one that does, until someone
else's words show up in your document.

```bash
murmly doctor
```

Look at the `speaker_recognition` section. A working `mine-only` setup looks
like this (some lines are left out):

```json
"speaker_recognition": {
  "mode": "mine-only",
  "match_threshold": 30,
  "owner": "Milo",
  "owner_enrolled": true,
  "enrolled_count": 1,
  "enrolled_names": ["Milo"],
  "model_present": true,
  "model_checksum_matches": true,
  "available": true,
  "detail": null,
  "resident": false
}
```

What the keys tell you:

| Key | Meaning |
| --- | --- |
| `available` | `true` when the mode can run. When it is `false`, `detail` says why. |
| `detail` | The reason the mode cannot run, or a warning when it can. `null` when all is well. With `mode = "off"` it says the speaker mode is off. |
| `owner_enrolled` | `true` when the name in `speakers.owner` is enrolled. |
| `enrolled_names` | Who murmly knows. |
| `model_present`, `model_checksum_matches` | `true` when the model is installed and is the expected file. |
| `voiceprints_need_reenrolment` | `true` when you replaced the model after enrolling. Enrol those voices again. |
| `resident` | Whether the speaker model is loaded in memory right now. `false` is normal. It loads when you first record in a speaker mode and is released after the idle period. |

`murmly doctor` also reports a mode or threshold it could not read, under
`mode_rejected_value` and `match_threshold_rejected_value`.

## 6. Use it { #use }

Dictate as you always do. Nothing changes in how you start or stop.

**With the hotkey.** Press the hotkey, speak, press it again. Murmly types the
result of the whole recording: in `mine-only` only your words, in
`label-everyone` the labelled line. Telling voices apart adds a short wait
before the text arrives, [about 1.3 to 1.7 seconds for each minute of
speech](#what-it-costs). If `mine-only` finds none of your voice in a
recording, nothing is typed.

**With continuous listening.** Set
[`auto_transcribe = "continuous"`](settings.md#stt-auto-transcribe) under
`[stt]` and restart murmly, as in step 4. Murmly then closes a segment at each
pause and types it while it keeps listening. Each segment is checked on its
own. In `mine-only`, a segment that held only other voices types nothing, and
the session keeps listening for your next sentence. In `label-everyone`, the
`Speaker` numbers stay the same until you press the hotkey to end the session.
See [finishing a recording by pausing](pause-to-finish.md).

## Add other people { #other-people }

To label other people by name in `label-everyone`, enrol them in the same way.
**Ask first.** A voiceprint identifies a person. Enrol someone else only if they
agree, and remove it if they ask.

Have them sit at the microphone you use, then run:

```bash
murmly enrol Sam
```

Enrolling does not need a restart. Murmly sees the new voice at the start of
the next recording.

List who is enrolled. The owner is marked:

```bash
murmly speakers list
```

Remove one voice, or all of them:

```bash
murmly speakers remove Sam
murmly speakers remove --all
```

## Turn it off { #turn-off }

Set the mode to `off`:

```toml
[speakers]
mode = "off"
```

Then restart murmly, as in [step 4](#restart). Your voiceprints stay where they
are. To delete them as well, run `murmly speakers remove --all`.

## When something does not work { #problems }

Messages you may see when you run `murmly enrol`:

| You see | What it means | What to do |
| --- | --- | --- |
| `Too little speech was heard: 3.2 seconds, and at least 10 are needed. Nothing was stored.` | The sample held less than 10 seconds of speech. | Check that the microphone is not muted, then try again and keep talking for the whole recording. |
| `The Murmly daemon is listening, so a sample would mix your voice with it. Try again when it is idle.` (or `transcribing`, or `speaking`) | Murmly is busy. | Wait until it is idle, including until spoken replies finish, and try again. |
| `The speaker model is missing: ...` | The model file is not in the data folder. | Run the `upgrade` command from [step 1](#get-the-model), or place the file there by hand. |
| `Cannot enrol that name: ...` | The name breaks one of the rules in [step 2](#enrol). | Pick another name. |
| `The earlier voiceprints were made with a different speaker model and have been discarded. Enrol those voices again.` | You replaced the model after enrolling. Only the voice you just enrolled works. | Enrol the other voices again. |
| `The earlier voiceprint file could not be read and has been replaced.` | The voiceprint file was damaged. Murmly started a new one. | Enrol the other voices again. |

What `murmly doctor` and the murmly log can show. In the log, the line starts
with `Speaker recognition disabled for this capture:`. While it is disabled,
murmly types everything.

| Reason given | What it means | What to do |
| --- | --- | --- |
| `the speaker model is missing: ...` | The model file is gone. | Run `upgrade` from [step 1](#get-the-model). |
| `the speaker model does not match the expected checksum: ... Download it again with the setup script.` | The file is damaged or is not the expected model. | Run `upgrade` from [step 1](#get-the-model). Setup replaces a file that does not match. |
| `mine-only needs an enrolled owner, and no owner is set` (the log says `no owner is enrolled`) | `owner` is empty. | Set `owner` as in [step 3](#configure) and restart. |
| `mine-only needs an enrolled owner, and "Milo" is not enrolled` | `owner` does not match an enrolled name. | Run `murmly speakers list`. Fix the spelling in `owner`, or enrol that name. Restart after changing `owner`. |
| `the enrolled voices were made with a different speaker model and must be enrolled again` | The model changed since you enrolled. | Enrol again, starting with the owner. |
| `the voiceprint file cannot be used: ...` | The voiceprint file is damaged. | Enrol again. Murmly replaces the file. |
| `the speaker mode is off` | `mode` is `off`, or was not a mode murmly knows. | Check `mode_rejected_value`. Set a mode and restart. |

**My own words are being dropped.** In `mine-only`, some of your own words are
left out.

1. Enrol again, with the microphone you dictate with every day, in a quiet room.
2. Run `murmly doctor` and check that `available` is `true` and `owner_enrolled`
   is `true`.
3. Check `match_threshold`. It is already at `30`, the lowest murmly allows. If
   you raised it, lower it again and restart.
4. Very short words are weak evidence. See [what it cannot
   do](#what-it-cannot-do).

**Other voices are being typed.** Raise
[`match_threshold`](settings.md#speakers-match-threshold), for example to `50`,
and restart murmly. The higher it goes, the more of your own quieter or shorter
words may be dropped. Also check that `mode` is not `label-everyone`, which
types everyone.

For anything else, see [when something goes wrong](troubleshooting.md).

## What it cannot do { #what-it-cannot-do }

- **Quick back-and-forth.** Murmly splits what you said into parts at breaks
  in the speech, then decides who spoke each one. A pause of about two seconds
  or more between speakers separates their words. If two people speak with a
  shorter pause between them, the whole part can go to one of them. In
  `mine-only` it is kept or dropped whole. In
  [continuous mode](pause-to-finish.md), the silence setting also splits what
  you said.
- **Overlapping speech.** When two people talk at once, murmly does not
  separate them.
- **The overlay shows everyone.** With [live transcription](words-as-you-speak.md)
  on, the partial text on the overlay includes every voice the microphone
  hears, in every mode. Only the text that is finally typed is filtered or
  labelled.
- **Short parts are weak evidence.** A part shorter than one second is not
  checked. It goes to the speaker of the nearest longer part. Voices are also
  harder to match over a second or two, so in `mine-only` some of your own words
  may be left out. The default threshold is already the lowest allowed.
- **Very short replies in a conversation.** A reply of a word or two, such as
  "yes" or "three", is too short to check, so it takes the speaker of the part
  next to it. If someone else spoke just before, your "yes" counts as theirs and
  `mine-only` leaves it out. Said on its own, the same word is kept.
- **Quieter voices.** `label-everyone` picks up a quieter voice, such as
  someone further from the microphone, that earlier versions could miss.
  `mine-only` works as before.
- **One person can get two numbers.** In `label-everyone`, if someone you have
  not enrolled says something very short, murmly may not recognise them and
  start a new `Speaker` number for it. The same person then appears as two
  speakers.
- **It fails open.** When a mode cannot run, murmly types everything. See
  [step 5](#check).
- **It is not a lock.** `mine-only` does not check who is there. A recording of
  your voice, played near the microphone, passes as you. Do not use it to keep
  anyone out of anything.

## What it costs { #what-it-costs }

Telling voices apart adds a wait between the end of the recording and the
text arriving. Measured on a fast machine, `mine-only` adds about 1.3 seconds
for each minute of speech, so a 10-second dictation waits about 0.2 seconds
longer. `label-everyone` adds about 1.7 seconds for each minute, so a 10-second
dictation waits about 0.3 seconds longer. Your machine may be slower. With the
mode `off`, nothing is added.

`label-everyone` does not skip the pauses before it transcribes, which is how it
hears quieter voices, and it then discards any part with no speech in it. That
costs the extra time, and the text of a recording can occasionally start without
capital letters or full stops. `mine-only` is not affected.

The speaker model runs on the CPU. It is loaded when you first record in a
speaker mode and released with the transcription model after the idle period.
It does not use your graphics card.

## The model { #the-model }

Murmly uses the WeSpeaker ResNet34-LM model, published by the
[WeSpeaker project](https://github.com/wenet-e2e/wespeaker) under the Creative
Commons Attribution 4.0 licence (CC BY 4.0). Murmly does not carry the file.
Setup downloads it from the publisher onto your machine.

The model was trained on VoxCeleb2, a collection of speech from public
videos. The terms published for VoxCeleb are not consistent: the pages that
distribute it say it is available for research purposes under CC BY 4.0, and
the copyright stays with the owners of the videos. If that matters to you, read
the terms and the licence file before you turn a speaker mode on. The licence
file is
[`licenses/wespeaker-resnet34-lm-license.txt`](https://github.com/osirison/murmly/blob/main/licenses/wespeaker-resnet34-lm-license.txt).

## Your voiceprints { #your-voiceprints }

A voiceprint is a list of numbers. It is not a recording, and murmly never
saves the audio of an enrolment. For where voiceprints are stored, who can read
them, and how to remove them, see
[Where your words go](where-your-words-go.md#voiceprints).

---

To change the owner, mode or threshold, see
[All the settings](settings.md#speakers-whose-voice-murmly-types). If something
is not working, see [When something goes wrong](troubleshooting.md).
