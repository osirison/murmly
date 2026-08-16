---
title: Selection-request counts cannot tell you a paste was consumed
description: Why xclip -loops and wl-copy --paste-once are unusable as clipboard consumption signals on a desktop with a clipboard manager
trigger: xclip -loops, xclip -l, wl-copy --paste-once, wl-copy -o

depends_on: src/murmly/integrations.py, openspec/specs/transcript-delivery/spec.md
recorded: 2026-08-16
---

## Symptom

You want to know when the application you pasted into has actually taken the text
off the clipboard, so you can restore the previous clipboard contents afterwards
without racing it. Both clipboard tools appear to offer exactly that:

```text
xclip:   -l, -loops       number of selection requests to wait for before exiting
wl-copy: -o, --paste-once Only serve one paste request and then exit.
```

They do not work for this. The owning process exits almost immediately, before any
application has pasted, and what the clipboard holds afterwards is no longer yours
to control: with no owner left, the observed value is whatever the clipboard manager
decides to put there. It has been seen both empty and repopulated with a stale
history entry. Either way the content you meant to protect is not reliably on the
clipboard.

## Reproduce

Run this with nothing pasting and no reader involved:

```bash
printf 'PAYLOAD' | timeout 5 xclip -quiet -selection clipboard -loops 1 &
sleep 1.2
kill -0 $! 2>/dev/null && echo "still alive" || echo "already exited"
xclip -o -selection clipboard   # not your payload
```

On Plasma X11 this reports `already exited` for `-loops 1`, `-loops 2` **and**
`-loops 3`. Raising the count does not buy you a real signal; it only changes how
many requests get eaten before the owner quits.

The `already exited` line is the load-bearing part of this check. Do not read
anything into what the final `xclip -o` prints — that is the manager's choice once
the owner is gone, and it varies.

## Cause

A desktop clipboard manager is a second, always-present consumer. Klipper (part of
plasmashell) requests the selection immediately on every ownership change so it can
keep clipboard history. Every request your counter sees is Klipper's, not the target
application's — the count carries no information about the application at all.

Once the loop count is spent, the tool releases the selection and no owner remains,
which is why the follow-up read returns empty.

Check whether a manager is present with:

```bash
busctl --user list | grep -i klipper
```

## Fix

Do not build consumption detection on a request count. The options that actually
work:

- **Bounded delay.** Wait a configured interval before restoring, and document it as
  a margin rather than a guarantee. This is what `ClipboardPaster` does; see
  `restore_delay_ms` in the README.
- **Own the selection in-process** and compare each `SelectionRequest`'s `requestor`
  against the window you expect. This is the only way to distinguish the manager from
  the target, but it needs TARGETS and INCR handling and an X11 event loop, and there
  is no Wayland equivalent.

Use an ordinary persistent copy (no `-loops`, no `--paste-once`) whenever the content
must survive on the clipboard — for example when a paste was deliberately not
performed.

## Why it was not obvious

The flags are documented in terms of *selection requests*, which sounds like a proxy
for "someone pasted". Nothing in either tool's help mentions that a clipboard manager
makes that count meaningless, and on a desktop without a manager the mechanism does
appear to work, so it fails only on the systems most users actually run.
