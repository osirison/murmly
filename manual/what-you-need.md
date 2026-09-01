# What you need before you start

This page lists what has to be true about your computer before you install murmly, and
what murmly needs in order to paste your words into whatever you're typing into.

## Supported platforms

Murmly runs on:

- **Linux**, any distribution, with a glibc-based system — see [machines that
  cannot run murmly](#machines-that-cannot-run-murmly) below for the one gap.
  Which desktop you run decides whether the hotkey and the overlay work
  automatically; see [Linux: your desktop](#linux-your-desktop).
- **Windows**, on the x86_64 architecture — see the same section below for the
  one gap there too.

Both need:

- **Python 3.12 or newer**.
- **A terminal.** Installing murmly means running a command in one, on either
  platform.

**macOS is not supported.** Nothing below describes macOS, and nothing on this
page applies to it.

## Machines that cannot run murmly

Two machines, each otherwise a supported platform, have no build of
`ctranslate2`, the runtime transcription needs — and transcription is what
murmly is:

- **musl-based Linux** (Alpine and similar distributions built on musl instead
  of glibc). No `ctranslate2` build exists for a musl C library.
- **Windows on the ARM64 architecture**. No `ctranslate2` build exists for
  Windows on ARM64 either — only the x86_64 build is published.

On either one, murmly refuses to sync before anything is installed, naming the
runtime and the missing build rather than letting `uv sync`'s own dependency
resolver fail on a package name that means nothing to you.

## Linux: your desktop

Which desktop you run decides two things: whether the hotkey registers itself,
and whether the recording overlay appears at all. Everything else — capture,
transcription, clipboard, and paste — works the same regardless of desktop.

| Desktop | Hotkey | Overlay |
| --- | --- | --- |
| KDE Plasma, X11 | Registers itself. The configuration murmly was built against, and the only one where it can confirm a transcript reached the window you were in. **Verified end to end before the port to Windows; not re-verified on X11 since.** | Appears. Same status: verified before the port, not re-verified on X11 since. |
| KDE Plasma, Wayland | Registers itself, through the same registration path as X11 with a different key-grab mechanism underneath. **Not verified end to end** — murmly says so during installation and checks whether the hotkey actually took effect. | Appears. **Not verified end to end** on Wayland specifically. |
| GNOME | Registers itself, through GNOME's own `custom-keybindings` mechanism. **Has never been run against a live GNOME session** — no automated environment murmly is built or tested in has one, so treat this as unproven until you have confirmed it yourself. `murmly doctor` shows what actually happened. | **Does not appear.** GNOME has no overlay backend today; murmly says so and installs everything else. |
| Anything else (Sway, Hyprland, river, and other desktops) | **Does not register itself.** `murmly install` still installs the background service, and prints the command you'd run to bind the hotkey yourself. | **Does not appear.** No overlay backend exists for these desktops; murmly says so and installs everything else. |

A desktop that cannot register the hotkey or present the overlay is not a
smaller install: recording, transcription, clipboard, and paste all keep
working regardless.

### Clipboard and paste tools for your session

To get a transcript out of murmly and into whatever window you're working in, murmly
needs a clipboard tool and a way to paste. Which ones depend on your session — X11, or
Wayland under one of a few different desktops. murmly works this out for itself: rather
than trust what's merely installed, it runs each candidate tool's own no-op check and
uses whichever one answers.

| Your session | Tools murmly uses | Permission |
| --- | --- | --- |
| X11 | `xclip`, `xdotool` | None needed. |
| KDE Plasma (Wayland) | `wl-clipboard`, `xdotool` | Asked for once. See below. |
| wlroots compositors (Sway, river, Hyprland) | `wl-clipboard`, `wtype` | None needed. |
| Anything offering neither | `wl-clipboard`, `ydotool` | One-time root setup. |

On plain X11, `xdotool` talks to the X server directly, so nothing needs to ask your
permission. Under KDE Plasma's Wayland session, KWin — the part of Plasma that draws and
manages your windows — bridges that same `xdotool` through a compatibility layer (XTEST,
tunnelled through something called libei) into its own input handling, so the X11 tool
can still reach Wayland-native windows. That bridging is what triggers the one-time
permission dialog covered below. wlroots is the project behind several other Wayland
desktops — Sway, river, Hyprland — and on those, murmly instead uses `wtype`, which
speaks to the Wayland virtual keyboard protocol directly and so needs no permission at
all. If your desktop offers neither route, murmly falls back to `ydotool`, which injects
your words by writing to `/dev/uinput` and is the only tool here that needs a one-time
root setup; see [Installing murmly](install.md) for that setup. murmly works this out by
running each tool's own no-op check, never by assuming what a named desktop supports, so
this applies to GNOME exactly as it would to any other desktop that turns out to offer
neither route.

### If murmly says it cannot paste

Run `murmly doctor`. Under the field named `paste_injection`, it reports which of the
tools above it settled on for your session. If none of them are available, it prints
what to install instead of guessing.

### Answering the KDE permission dialog

The first time murmly pastes on KDE Plasma's Wayland session, KDE raises a permission
dialog. Answer it once:

!!! tip "Answer it once, permanently"
    Tick **Always allow**, then click **Allow**. murmly won't ask again.

    You can revoke this later under System Settings → *Legacy X11 App Support*.

Until you answer that dialog, the first transcript of a session reaches only your
clipboard — it does not get pasted in for you. That's expected: the paste attempt queues
while the dialog is open, and by the time you answer it, the attempt has already
finished.

murmly never puts your previous clipboard contents back over a transcript delivered this
way. The reason is that `xdotool`, the tool doing the pasting, exits with status `0` —
success — whether or not the paste actually landed, so murmly can never be told the
paste failed. Rather than risk quietly overwriting a transcript that never actually made
it into your document, murmly leaves it sitting on your clipboard where you can see it
and paste it yourself. A transcript is never lost to a paste that silently didn't
happen.

## Windows

Every capability below has run, as an automated test, on a real Windows machine
— a GitHub Actions Windows runner — rather than only on the Linux machine this
port was written from: the named-pipe command channel and its access-control
list, hotkey registration and its collision handling, installation through Task
Scheduler, the clipboard and paste injection, focus observation, the recording
overlay, and reading the microphone privacy setting. What has not yet been
confirmed is the deeper, human-driven check on a second account and against a
window running elevated — that is still open, tracked as its own verification
step.

- **Hotkey.** Registers itself with no permission needed. If another
  application already holds the combination, Windows refuses the registration
  and that refusal is the collision — but unlike KDE, Windows offers no way to
  ask who holds it, so murmly can report that the key is taken without being
  able to name the application.
- **Overlay.** Appears through a Qt-based renderer, drawn the same way and
  showing the same states as the Linux one. Its toolkit is not part of a plain
  sync — run `uv sync --extra overlay` first, or the overlay is simply
  disabled while everything else keeps working.
- **Microphone permission.** Windows gates microphone access behind two
  settings under **Settings → Privacy & security → Microphone**: the overall
  "Microphone access" toggle, and "Let desktop apps access your microphone"
  underneath it. Both have to be on. `murmly doctor` reports this permission as
  granted, denied, or undetermined — never granted on the strength of a
  microphone merely being present.
- **Paste into an elevated window.** See [where your words
  go](where-your-words-go.md#pasting-into-an-elevated-window-on-windows) —
  this is documented in the same place as the equivalent KDE failure, because
  it is the same class of problem: a paste that reports nothing wrong while
  nothing happens.

---

With all of this in place, move on to [Installing murmly](install.md).
