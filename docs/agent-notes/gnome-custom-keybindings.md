---
title: Bind GNOME global shortcuts via org.gnome.settings-daemon.plugins.media-keys custom-keybindings
description: gsettings CLI shape, GVariant read/modify/write gotchas, and the accelerator string format for a GNOME custom keybinding written from a CLI installer
trigger: gsettings custom-keybindings, org.gnome.settings-daemon.plugins.media-keys, GTK accelerator, gtk_accelerator_parse

depends_on: src/murmly/hotkey.py, src/murmly/desktop.py
recorded: 2026-08-31
verified_on: not verified against a live GNOME session -- documented convention only, see Scope below
---

## Symptom

You want to bind a hotkey to a command from an installer on GNOME, live without
a logout, the way `docs/agent-notes/plasma-global-shortcut-binding.md` does for
KDE. GNOME has no equivalent to KDE's `.desktop` launcher + `X-KDE-Shortcuts`
mechanism; the whole thing is one relocatable GSettings schema.

## The mechanism

Two schemas work together:

* `org.gnome.settings-daemon.plugins.media-keys`, key `custom-keybindings`: an
  array of dconf paths (`as`), one per custom binding a user or an app has
  created.
* `org.gnome.settings-daemon.plugins.media-keys.custom-keybinding`, a
  *relocatable* schema addressed at each of those paths, with three keys:
  `name` (label), `command` (what runs), `binding` (the accelerator).

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/murmly-window/']"

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/murmly-window/ \
  name 'murmly'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/murmly-window/ \
  command '/abs/path/.venv/bin/murmly toggle'
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/murmly-window/ \
  binding '<Super>x'
```

The relocatable schema is addressed as `SCHEMA:PATH` -- one argument, colon
joined -- not `SCHEMA PATH` as two. This is a live dconf value: the settings
daemon watches it and the binding takes effect without a logout, which is the
whole reason GNOME is worth having a backend for (see `design.md`).

## The list is shared -- read, modify, write exactly one entry

`custom-keybindings` is one array shared with every other custom shortcut the
user has created, through the *same* schema. A write that does not start from
a fresh read of the current list will clobber someone else's binding:

```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
# ['/org/.../custom0/', '/org/.../custom1/']
```

Append your path to what you read; on removal, filter out exactly your path
and write the rest back. Never construct the list from scratch.

**The empty-list gotcha**: an empty `as` prints with a type annotation because
otherwise the value is ambiguous:

```
@as []
```

Strip a leading `@as ` before parsing with `ast.literal_eval` (the rest is
valid Python list-of-strings syntax for any path this code writes, since paths
never contain a quote or backslash). Writing an empty list back must also use
`@as []`, not `[]` -- `gsettings set` on a bare `[]` is a documented ambiguous
case.

## The accelerator string

GTK accelerator text, as `gtk_accelerator_parse` reads it: zero or more
`<Token>` modifiers immediately followed by the key name, no separators.
Modifier tokens: `<Control>` (also accepted on read as `<Primary>`, GTK's
platform-portable alias -- Murmly's own writes always emit `<Control>`),
`<Alt>`, `<Shift>`, `<Super>` (the physical Windows/Cmd-position key -- what
Murmly calls "Meta"), `<Hyper>`. Order does not matter to GTK's parser; Murmly
writes them in a fixed order (Meta, Ctrl, Alt, Shift, Hyper) for its own
read-back comparisons.

Key names are GDK/X11 keysym names, not Qt's. Confirmed different from Qt's
own spelling for: `BackSpace` (not "Backspace"), `ISO_Left_Tab` (Shift+Tab is a
distinct X11 keysym, not a modified Tab), `KP_Enter` (numpad Enter; bare
`Return` is the main key), `Page_Up`/`Page_Down`, lowercase `space`, and the
multimedia keys as `XF86AudioLowerVolume` / `XF86AudioMute` /
`XF86AudioRaiseVolume` / `XF86AudioPlay` / `XF86AudioStop` / `XF86AudioPrev` /
`XF86AudioNext` / `XF86AudioMicMute`. Letters and digits are lowercase single
characters (`x`, `7`); function keys are `F1`-`F35`, matching Qt's own
spelling there.

## Conflict detection has no full-coverage answer

GNOME does not arbitrate a duplicate binding any more than KDE's
`kglobalaccel` does -- a second `custom-keybindings` entry claiming the same
accelerator registers without error, and nothing says which one the settings
daemon actually fires (unverified: this is inferred from the mechanism being
"just another value in a schema", not observed on a live session). Murmly must
therefore determine a conflict itself before writing, exactly as it does for
KDE.

What is scanned: every path in `custom-keybindings` -- covers other
applications' and the user's own custom shortcuts, which live in the same
schema -- and, in addition, every key in GNOME's fixed keybinding schemas:
`org.gnome.desktop.wm.keybindings`, `org.gnome.shell.keybindings`, and
`org.gnome.settings-daemon.plugins.media-keys` itself (the *base* schema,
carrying GNOME's own built-in volume/brightness/screenshot bindings directly
-- not the *relocatable* `.custom-keybinding` schema each `custom-keybindings`
entry is addressed through).

`gsettings list-recursively SCHEMA` prints one line per key as `SCHEMA KEY
VALUE`; a fixed schema's accelerator keys are typed `as` (an action can carry
more than one accelerator), so each line's value is parsed as either a string
list or a bare string, and every candidate string is run through the same
`parse_gnome_accelerator` the custom-keybindings scan uses. Most keys in a
fixed schema are not accelerators at all -- ints, bools, enums, and (on the
media-keys base schema) `custom-keybindings` itself, which is a list of dconf
paths, not accelerator text -- but `parse_gnome_accelerator` returns `None` for
anything that does not parse as one, so a non-accelerator value is silently
skipped rather than mistaken for a match. A binding a person has cleared in
Settings is conventionally written back as `@as []`, which parses to no
candidates, not a false match.

**Fail-closed, deliberately**: `list-recursively` failing on any one of the
three fixed schemas -- a broken dconf, a desktop that does not ship
`gnome-shell` at all -- raises rather than being read as "this schema holds
nothing". A query the code could not answer is not evidence a key is free;
answering "clear" from an unanswered query would be exactly the silent
double-bind this scan exists to prevent. This applies uniformly across all
three schemas, including one this session's desktop may simply not have
installed: an unreadable schema aborts the scan rather than being skipped as
inapplicable, because a `gsettings` failure carries no reliable way to tell
"schema absent" apart from "schema present but the query failed". A read-only
caller (`status()`) already guards every `owners_of` call with a broad
exception handler, so this raise degrades a diagnostic rather than crashing
it; a registration caller (`Installer.install()`) does not catch it, so the
same failure refuses the install rather than binding a key GNOME's own
mechanism cannot be asked about.

## Scope

Nothing above has been run against a real `gnome-settings-daemon`. Every
command shape and key name is the documented, widely-used convention (the same
one desktop-environment configuration scripts and GNOME's own Settings UI
produce), not something verified end-to-end the way the Plasma note is. Treat
this the way Murmly treats Plasma-on-Wayland: register, then read back and
report what actually happened, rather than trusting the write.
