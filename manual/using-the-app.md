# Using the app

Murmly has a small desktop app. It shows what murmly is doing, lets you change
settings without opening a file, runs the same checks as `murmly doctor`, and can
remove murmly. You never need it: murmly types your words the same way whether the
app is installed, open or closed. Closing the app does not stop murmly.

The app runs on Linux and on Windows.

## Getting it

The app is an optional part of murmly called the `gui` extra. Add it, then open the
app:

```bash
uv sync --extra gui
uv run murmly app
```

If you run `murmly app` without the extra, murmly tells you to add it and stops.

On Linux, installing murmly with the extra present also puts **Murmly** in your
applications menu, so you can open the app from there. If you add the extra after
installing, run `murmly install` again to get the menu entry.

On Windows there is no shortcut yet. Open a terminal and run `murmly app` each time.

## Home

Home is the first page. At the top it says what murmly is doing:

| It says | It means |
| --- | --- |
| Ready when you are | Murmly is waiting for your key |
| Listening | You are recording |
| Writing your words | Murmly is turning the recording into text |
| Your words are safe on the clipboard | Murmly did not type your last words; see below |
| The background service is not running | Murmly cannot hear your key until its service is running |

A change shows within a second. When the service is not running, Home offers a
**Start the service** button.

![The Home page of the murmly app, with the heading "Ready when you are", the two keys, the last words with Copy again and Clear buttons, and a summary of how murmly is set up.](assets/app-home.png)

Below the state, Home shows the keys that are bound: the one that dictates, and the
one that replies to a coding assistant (see [Making murmly
speak](making-murmly-speak.md)). It ends with a summary of how murmly is set up:
accuracy, whether it runs on your graphics card or your processor, how long it keeps
the model in memory, and how a recording ends.

### Last words

The last words are the most recent thing murmly heard, whether it typed them or not.
Home shows them with how long ago they were said and what happened to them. Two
buttons sit beside them:

- **Copy again** puts them on the clipboard again.
- **Clear** makes murmly forget them.

Murmly keeps them in the memory of its background service and nowhere else. They are
never written to a file or to the logs. They are gone when the service stops or
restarts, which includes the restart that follows a change in Settings, and the next
dictation replaces them.

### Words not typed

Sometimes murmly will not type, because another window came to the front before your
words were ready. Rather than type into the wrong place, it copies them to the
clipboard. Home then says **Your last words weren't typed**, shows the reason murmly
gave, word for word, and shows what you said. **Copy again** puts the words back on the
clipboard, and then you press Ctrl+V where you meant them to go. **Done** dismisses the
notice. The reasons are the ones in [Where your words go](where-your-words-go.md).

## Your keys, and first run

The first time you open the app on a computer with no dictation key set, it starts
with a short setup instead of Home.

1. **Your keys.** Press the keys you want to dictate with. Murmly checks them with the
   same rules `murmly install` uses: they need Meta, Ctrl or Alt in them, and a key
   another application already holds is refused, naming that application where the
   desktop can tell. Meta is the Windows key on Windows and the Super key on Linux.
   The box that records your keys listens only while you have clicked into it. You can
   also choose one of the keys it suggests. Below it is a text box where you can try
   dictating before you leave.
2. **How careful.** Choose how accurate the listening should be. A more careful
   listener gets more words right and takes longer. You can change this later in
   Settings.

**Set up later** skips it, and **Back** and **Continue** move between the two steps.
Setup appears again on the next launch while no dictation key is set. To change a key
afterwards, see [Changing your hotkey](changing-your-hotkey.md).

## Settings

Settings groups the options most people change. [All the settings](settings.md) says
what each one does and what its default is; the lines marked **In the app** there name
the control.

Every change is saved as soon as you make it. Murmly then restarts its service to use
the new value, but only once nothing is being recorded, transcribed or spoken. Until
then the page says "will apply when Murmly is idle", and a recording in progress is
never cut off.

The app changes only the one setting you touched. Your comments, the order of the
lines and every setting the app does not show stay as you wrote them. Two cases are
handled with care:

- **A file that does not parse.** If `config.toml` has a mistake in it, the app does not
  write to it. Settings shows the error exactly as the parser reported it and turns every
  control off until you fix the file.
- **A file you edited by hand.** If the file changed while the app was open, the app
  reads it again before saving, tells you it changed, and does not write over your edit.

A value outside its range is refused, and the page says what range is accepted.

## Health

Health runs the same checks as `murmly doctor` and shows each one as a plain sentence,
with the raw setting or value it came from beside it, so you can see the evidence. It
takes a few seconds. **Check again** runs it again.

**Copy the full report** copies the whole `murmly doctor` report, ready to paste into a
question. It names your desktop and the tools murmly uses, and never includes anything
you said. [When something goes wrong](troubleshooting.md) lists the usual causes.

## The tray

If your desktop has a system tray, murmly puts an icon there. Click it to see a small
panel with the state, the last words (with **Copy**), and four switches. Each switch
changes the same setting as its control in Settings and is saved and applied the same
way:

| Switch | What it changes |
| --- | --- |
| Words as you speak | Live transcription: shows the words in the overlay while you talk. See [Seeing your words as you speak](words-as-you-speak.md) |
| Stop when I pause | Auto-transcribe: a pause ends the recording, so you do not press the key again. See [Finishing a recording by pausing](pause-to-finish.md) |
| Speak when my assistant finishes | Assistant announcements. See [Hearing when your coding assistant finishes](announcements.md) |
| Quiet hours | Silences speech output during the hours you set. Switching it on uses the hours you had, or the default |

The panel also has **Open murmly**, **Health** and **Quit**. **Quit** closes the app;
murmly's service keeps running.

**Closing the window** with a tray present leaves the app in the tray, and the icon
opens it again. Without a tray, closing the window ends the app, so nothing is left
running out of sight. Either way, dictation carries on.

GNOME does not show tray icons unless you install an AppIndicator extension. On GNOME
without one, the app runs from its window alone with no icon, and Health says that no
system tray was found.

## Removing murmly

At the bottom of Settings, under **This computer**, **Remove murmly** does what
`murmly uninstall` does, and works even when the settings file cannot be read. It
first lists everything it is about to remove, read from what is installed on your
computer, and nothing happens until you confirm. That includes the applications-menu
entry for the app, so the app closes when it finishes.

Your settings, the models murmly downloaded and your enrolled voices are kept.
Turning on **Also delete my settings and downloaded models** removes those too, as
`murmly uninstall --purge` does. That switch starts off, names the folders it will
delete, and cannot be undone.

A purge leaves one thing: the transcription model is stored in the Hugging Face
cache, which other programs share, so murmly never deletes it. See [Removing
murmly](install.md#removing-murmly) for the rest of what is and is not removed.

## Voices

The app does not manage voices yet. To type only your own voice, use
`murmly enrol` and `murmly speakers` as [Typing only your own
voice](typing-only-your-own-voice.md) describes. Home's summary shows which mode is
set.

## Privacy

- The app never takes the keyboard from you. It does not raise its window, move focus
  or grab a key while you dictate, so your words land in the window you started in even
  when the app is open behind it.
- The last words live only in the memory of murmly's service. Murmly hands them to the
  app over its command socket, which only your own account can open, and they are not
  in any log.
- Recording a key in the app listens only while its box has focus. Murmly installs
  nothing that watches your keyboard across the system.
- Nothing you say leaves your computer.
