# Where your words go

## Why nothing was pasted

The most common thing that happens is that you speak, and nothing appears in
the window you were looking at.

Murmly decides where a transcript goes at the moment it presses Ctrl+V, which
is seconds after you stop speaking. To stop transcripts landing in whatever
happened to take focus in the meantime, murmly records the focused window when
capture stops and checks it again just before pasting.

If the focused window changed, murmly **does not paste**: the transcript is
left on your clipboard so you can place it yourself, the previous clipboard is
not restored, and the overlay shows its error symbol. So if nothing appeared,
look at your clipboard — your words are there.

The part of murmly that keeps running in the background — the murmly service —
reports this as `"delivered": false`:

```json
{
  "ok": true,
  "state": "DONE",
  "text": "the words you spoke",
  "delivered": false,
  "detail": "Transcript copied to the clipboard but not pasted."
}
```

The same check applies to `murmly spike --paste`.

This check is controlled by
[`clipboard.verify_target`](settings.md#clipboard-verify-target).

## What each session gets

These are two separate questions: whether murmly can tell that focus moved
away before it pastes, and whether it can tell the paste itself landed.

| Session | Target verification | Clipboard preservation |
| --- | --- | --- |
| X11 with an EWMH window manager | yes | yes |
| X11 without EWMH | no | yes |
| Wayland with `wtype` or `ydotool` | no | yes |
| Wayland with `xdotool`, which is the KDE Plasma path | no | no |
| Windows | yes | no |

On KDE Plasma's Wayland session, murmly cannot check the window and cannot put
your old clipboard back — that is the row above naming `xdotool`, and it is
why both of its columns say "no". Windows can check the window: it reads the
foreground window directly, needing no permission. It still cannot put your
old clipboard back, but for the unrelated reason in the next section —
`SendInput`'s own success cannot be trusted, regardless of whether the window
it was aimed at was still the right one.

`murmly doctor` reports which of these applies to your session, under
`delivery`.

## Pasting into an elevated window on Windows

On Windows, murmly pastes by sending synthetic keystrokes with `SendInput`.
Windows itself — not murmly — silently discards synthetic input aimed at a
window belonging to a process running at a higher privilege level than
murmly's own, such as anything opened with "Run as administrator". This is
User Interface Privilege Isolation (UIPI), a Windows security feature, and it
produces no error: the call that sends the keystrokes reports success whether
or not anything actually arrived.

This is the same class of failure as the KDE Plasma Wayland case above: a
paste that reports nothing wrong while nothing happens. murmly's response is
the same one it gives there. Because `SendInput`'s success cannot be trusted,
murmly treats every paste on Windows as unconfirmed: the transcript is left on
the clipboard rather than assumed delivered, and your previous clipboard
contents are never restored over it — restoring over the only copy of what you
said, on the chance the paste silently failed, would be worse than leaving it
on the clipboard for you to paste yourself.

If you were dictating into an elevated window, that is exactly what happened:
paste from the clipboard by hand.

## Restoring your previous clipboard

Murmly never restores over a transcript it cannot prove was delivered.
`xdotool` on Wayland exits 0 whether or not the keystroke reached the window,
so on that path the previous clipboard is not read and not put back, whatever
`clipboard.restore` says — an undelivered transcript is the only copy of what
you said, and restoring over it would destroy it. `murmly doctor` reports this
as `paste_injection.confirms_delivery`.

Pasting overwrites your clipboard, so murmly puts the previous contents back
afterwards. It waits `restore_delay_ms` first, giving the receiving
application time to read the transcript. This is a margin, not a guarantee:
murmly cannot tell whether the application has read the clipboard, because a
desktop clipboard manager such as Klipper takes a copy of every clipboard
change immediately, so any "someone read it" signal reports the manager
rather than the application.

Raise `restore_delay_ms` if a slow application ever pastes your previous
clipboard instead of the transcript. Values outside 0-5000 fall back to 500.
Set `restore = false` to keep the transcript on the clipboard and never
restore it.

See [`clipboard.restore`](settings.md#clipboard-restore) and
[`clipboard.restore_delay_ms`](settings.md#clipboard-restore-delay-ms).

## Pasting without the check

To paste unconditionally, as murmly did before target verification existed,
set:

```toml
[clipboard]
verify_target = false
```

---

If murmly still is not pasting the way you expect, see
[When something goes wrong](troubleshooting.md). For which paste tools your
desktop needs, see [Installing murmly](install.md).
