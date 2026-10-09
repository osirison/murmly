# Typing only your own voice

Murmly can tell voices apart. It can leave out everyone but you, so a
television or a colleague in the room does not end up in your document. Or it
can type everyone and say who spoke.

This is off by default. Nothing here runs until you pick a mode.

## Enrol your voice

Murmly knows a voice only after you enrol it. Enrolling records a short sample,
turns it into a voiceprint, and stores the voiceprint under a name.

```bash
murmly enrol Milo
```

Replace `Milo` with your own name. Murmly prints a short passage and records
for 20 seconds. Read the passage aloud at a relaxed pace. It is shorter than
the recording, so **keep reading until the recording stops**. If you reach the
end, start again from the top. Murmly needs at least 10 seconds of speech and
refuses a sample with less.

A few things to know:

- **Use the microphone you use every day.** A voice sounds different through
  different microphones. If you change microphones, enrol again. Enrolling a
  name that already exists replaces its voiceprint.
- The murmly service must be idle. If it is listening or transcribing, wait and
  try again.
- A name has at most 32 characters. It cannot hold a colon or a line break, and
  it cannot be `You` or `Speaker` followed by a number, because murmly uses
  those as labels.
- The speaker model must be installed. `./setup.sh` and `bootstrap.ps1`
  download it. If it is missing, `murmly enrol` says where it looked.

You can enrol more than one voice. Enrolling works whatever mode is set.

## Say which voice is yours

Murmly needs to know which enrolled name is you. Set it as the owner in your
configuration file:

```toml
[speakers]
owner = "Milo"
```

See [`speakers.owner`](settings.md#speakers-owner). `murmly enrol` prints this
for you when no owner is set. Restart the service after changing it.

## Pick a mode

Set [`speakers.mode`](settings.md#speakers-mode) to one of three words.

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

## Check that it works

**If you use `mine-only`, run `murmly doctor` after you set it up.** When murmly
cannot run a speaker mode, it does not stop you. It types everything, as if the
mode were `off`, and writes one warning to its log. Nothing appears on the
overlay or in the pasted text. So a setup that does not work looks like one that
does, until someone else's words show up in your document.

Look at the `speaker_recognition` section:

- `available` is `true` when the mode can run. When it is `false`, `detail`
  says why.
- `owner_enrolled` is `true` when the name in `speakers.owner` is enrolled.
- `enrolled_names` lists who murmly knows.
- `model_present` and `model_checksum_matches` are `true` when the model is
  installed and is the expected file.
- `voiceprints_need_reenrolment` is `true` when you replaced the model after
  enrolling. Enrol those voices again.

`murmly doctor` also reports a mode or threshold it could not read, under
`mode_rejected_value` and `match_threshold_rejected_value`. For the threshold
itself, see [`speakers.match_threshold`](settings.md#speakers-match-threshold).

You can list who is enrolled, marking the owner, at any time:

```bash
murmly speakers list
```

## What it cannot do

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
- **One person can get two numbers.** In `label-everyone`, if someone you have
  not enrolled says something very short, murmly may not recognise them and
  start a new `Speaker` number for it. The same person then appears as two
  speakers.
- **It fails open.** See above: when a mode cannot run, murmly types everything.
- **It is not a lock.** `mine-only` does not check who is there. A recording of
  your voice, played near the microphone, passes as you. Do not use it to keep
  anyone out of anything.

## What it costs

Telling voices apart adds a wait between the end of the recording and the
text arriving. Measured on a fast machine, it is about 1.3 seconds for each
minute of speech, so a 10-second dictation waits about 0.2 seconds longer.
Your machine may be slower. With the mode `off`, nothing is added.

The speaker model runs on the CPU. It is loaded when you first record in a
speaker mode and released with the transcription model after the idle period.
It does not use your graphics card.

## The model

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

## Your voiceprints

A voiceprint is a list of numbers. It is not a recording, and murmly never
saves the audio of an enrolment. For where voiceprints are stored, who can read
them, and how to remove them, see
[Where your words go](where-your-words-go.md#voiceprints).

---

To change the owner, mode or threshold, see
[All the settings](settings.md#speakers-whose-voice-murmly-types). If something
is not working, see [When something goes wrong](troubleshooting.md).
