# Using murmly

## The loop

1. Press your hotkey. The overlay appears at the bottom of the screen and its
   bars follow your voice.
2. Speak.
3. Press the hotkey again. Murmly transcribes, copies, and pastes into the
   window you started in.

Registration is confirmed at install time, but only a keypress proves the
desktop actually delivers the key — so press it once after installing.

## The window that appears while you speak

While you use murmly, a small panel appears at the bottom of the screen
showing what murmly is doing. It moves through three states as you record.

![The murmly overlay while recording, showing a row of bars rising and falling with the speaker's voice.](assets/overlay-listening.png)

While murmly listens, the bars rise and fall with your voice.

![The murmly overlay with a panel of transcribed text below the level bars, showing the words spoken so far while the person is still talking.](assets/overlay-partial.png)

If live transcription is switched on, a panel of transcribed text appears
below the bars while you are still speaking, showing what murmly has made of
your words so far. This panel only appears when live transcription is on —
see [Seeing your words as you speak](words-as-you-speak.md).

![The murmly overlay showing murmly working on the recording just after the hotkey was pressed a second time, before the transcript appears.](assets/overlay-processing.png)

Press the hotkey a second time and the panel changes to show murmly working on
the recording, between you finishing speaking and the transcript landing.

The overlay works on KDE Plasma, under both X11 and Wayland. Under X11 it is
built from two window-system features called X11 EWMH window state and the X
Shape extension; under Wayland it uses a Wayland feature called Layer Shell.
On both, the overlay is a fixed size — 156 by 48 logical pixels — and it does
not request keyboard focus and does not receive pointer input: it never steals
your typing and never intercepts a click.

If you have more than one display connected, murmly shows the overlay on the
display containing the desktop origin, and keeps it on that display for the
whole recording. With reduced motion turned on, the overlay swaps continuous
animation for stable symbols that show its state, and level feedback that
steps rather than moves smoothly.

## Turning the overlay off

Set `overlay.enabled = false` and restart the part of murmly that keeps
running in the background — the murmly service. This changes nothing about
voice-to-text behavior: the loop above still works exactly the same, murmly
just does not show it to you. See [the setting](settings.md#overlay-enabled).

```bash
systemctl --user restart murmly.service
```

The overlay has a few other settings: how far it sits from the bottom of the
screen ([`overlay.bottom_margin_px`](settings.md#overlay-bottom-margin-px)),
whether it uses [reduced motion](settings.md#overlay-reduced-motion), and the
[size of its text](settings.md#overlay-text-size-px).

Next: see [where your words go](where-your-words-go.md) once murmly has
delivered them, or [change your hotkey](changing-your-hotkey.md) if the one
you picked at install time is not the one you want.
