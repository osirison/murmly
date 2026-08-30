# What you need before you start

This page lists what has to be true about your computer before you install murmly, and
what murmly needs in order to paste your words into whatever you're typing into.

Before you install murmly, make sure you have:

- **Fedora**.
- **KDE Plasma**, for the hotkey and the recording overlay.
- **Python 3.12 or newer**.
- **A terminal**, because installing murmly means running a command in one.
- **The right session for what's verified.** X11 is verified end to end. Plasma Wayland
  uses the same hotkey-registration path but a different key-grab mechanism underneath,
  and has not been verified end to end. murmly says so during installation and checks
  whether the hotkey actually took effect.

Hotkey registration itself is a KDE Plasma feature. On any other desktop, `murmly
install` still installs the background service, and it prints the command you'd run to
bind the hotkey yourself.

## Clipboard and paste tools for your desktop

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
root setup; see [Installing murmly](install.md) for that setup.

## If murmly says it cannot paste

Run `murmly doctor`. Under the field named `paste_injection`, it reports which of the
tools above it settled on for your session. If none of them are available, it prints
what to install instead of guessing.

## Answering the KDE permission dialog

The first time murmly pastes, KDE raises a permission dialog. Answer it once:

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

---

With all of this in place, move on to [Installing murmly](install.md).
