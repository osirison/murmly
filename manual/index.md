# The murmly manual

Murmly types what you say. Press a key, speak, press it again, and your words
appear in whatever you were working in. Everything happens on your own computer.

This manual is everything there is to know about it. You do not need to read it
in order, and most people never need most of it.

## Start here

**Never used murmly?** Read [what you need before you start](what-you-need.md),
then [installing murmly](install.md), then [using murmly](using-murmly.md). That
is about ten minutes and it is the whole of getting going.

**Already installed it?** The page you want is probably one of these:

- [Changing your hotkey](changing-your-hotkey.md) — pick a different key, or take
  the key away.
- [Where your words go](where-your-words-go.md) — why a transcript sometimes
  lands on your clipboard instead of in the window, and what happens to whatever
  was on the clipboard before.
- [When something goes wrong](troubleshooting.md) — start here when murmly is
  not doing what you expect.

**Something is not working?** Run this, and read what it tells you:

```bash
uv run murmly doctor
```

It is the first step of every answer in [when something goes
wrong](troubleshooting.md).

## Everything else

| Page | What it covers |
| --- | --- |
| [What you need before you start](what-you-need.md) | The computer and the software murmly expects, and what it needs to paste into your windows |
| [Installing murmly](install.md) | One command, what it writes, and the ways to do it by hand |
| [Using murmly](using-murmly.md) | The three-step loop, and the little window that appears while you speak |
| [Changing your hotkey](changing-your-hotkey.md) | Rebinding, moving both keys at once, and removing them |
| [Where your words go](where-your-words-go.md) | Pasting, the clipboard, and why murmly sometimes refuses to paste |
| [Seeing your words as you speak](words-as-you-speak.md) | Showing the transcript while you are still talking |
| [Finishing a recording by pausing](pause-to-finish.md) | Letting a silence end the recording instead of pressing the key again |
| [Making murmly speak](making-murmly-speak.md) | Turning on speech output and the second hotkey that goes with it |
| [Hearing when your coding assistant finishes](announcements.md) | Being told out loud when an agent has finished its turn |
| [All the settings](settings.md) | Every option, its default, and what it does |
| [Speed, memory, and your graphics card](speed-and-memory.md) | What murmly holds in memory, how fast it is, and what a GPU is worth |
| [When something goes wrong](troubleshooting.md) | Diagnostics first, then answers by symptom |
| [For developers](for-developers.md) | The speech-session protocol, and the rules the command socket enforces |

## Where things moved

All of this used to live in one very long `README.md` in the source repository.
If you followed a link to a heading there and landed at the top of a much
shorter file, this is where that heading went.

| It used to be under | It is now on |
| --- | --- |
| Overview | The [murmly home page](https://osirison.github.io/murmly/), and the top of `README.md` |
| Requirements | [What you need before you start](what-you-need.md) |
| Pasting with ydotool | [Installing murmly](install.md#if-murmly-cannot-paste-on-your-desktop) |
| Installing by hand | [Installing murmly](install.md#installing-by-hand) |
| Choosing a hotkey | [Installing murmly](install.md#choosing-a-hotkey) |
| What installation writes | [Installing murmly](install.md#what-installing-writes) |
| Change or remove the hotkey | [Changing your hotkey](changing-your-hotkey.md) |
| Speech output | [Making murmly speak](making-murmly-speak.md) |
| Turning it on | [Making murmly speak](making-murmly-speak.md) |
| Where synthesis runs | [Speed, memory, and your graphics card](speed-and-memory.md) |
| Announcing a finished agent turn | [Hearing when your coding assistant finishes](announcements.md) |
| Asking the agent for a voice note | [Hearing when your coding assistant finishes](announcements.md) |
| The two hotkeys | [Making murmly speak](making-murmly-speak.md) |
| The session protocol | [For developers](for-developers.md) |
| What speech output does not do | [Making murmly speak](making-murmly-speak.md) |
| Scope and limitations | Split: the desktop and session parts are on [what you need](what-you-need.md), the memory part on [speed and memory](speed-and-memory.md), and the privacy of a live transcript on [seeing your words as you speak](words-as-you-speak.md) |
| Configuration | [All the settings](settings.md) |
| The command socket | Split: the permission rules are on [for developers](for-developers.md), the model profiles on [speed and memory](speed-and-memory.md) |
| Live transcription | [Seeing your words as you speak](words-as-you-speak.md) |
| Auto-transcribe | [Finishing a recording by pausing](pause-to-finish.md) |
| Releasing idle model memory | [Speed, memory, and your graphics card](speed-and-memory.md) |
| Transcript delivery | [Where your words go](where-your-words-go.md) |
| What each session gets | [Where your words go](where-your-words-go.md#what-each-session-gets) |
| Restoring your previous clipboard | [Where your words go](where-your-words-go.md#restoring-your-previous-clipboard) |
| The recording overlay | [Using murmly](using-murmly.md#the-window-that-appears-while-you-speak) |
| Troubleshooting | [When something goes wrong](troubleshooting.md) |

`README.md` still carries what it takes to install murmly and produce one
transcript, and the commands a contributor runs against the source. Everything
else is here.
