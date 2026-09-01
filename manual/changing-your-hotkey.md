# Changing your hotkey

!!! note
    This page assumes murmly is already installed on Linux or Windows, with
    Python 3.12 or newer, from a terminal. On Linux, whether the hotkey
    registers itself at all depends on your desktop: KDE Plasma X11 is
    verified end to end before the port to Windows and not re-verified on X11
    since, KDE Plasma Wayland is not verified, GNOME's backend has never
    been run against a live GNOME session, and any other desktop has no
    automatic registration — `murmly install` prints the command to bind the
    key yourself instead. Windows always registers itself, with no
    permission needed. See [What you need before you
    start](what-you-need.md) for the full detail.

## Using a different key

Rebind by installing again with a different key — the same command on Linux and
Windows:

```bash
uv run murmly install Meta+Shift+Space
```

That rebinds the focused-window hotkey only; a speech-session hotkey keeps the
key it had.

On a desktop where the hotkey does not register itself, this still updates
murmly's own record of which key is bound and prints the new bind-it-yourself
command; run that command again to actually move the binding your desktop
fires.

## Moving both keys at once

Name both keys to move both, and to swap them for each other:

```bash
uv run murmly install Meta+Shift+Space Meta+A
```

Asking for a key that murmly's *other* binding currently holds is refused
rather than silently unbinding it — name both keys in one command instead.

## Removing it

Remove everything:

```bash
./setup.sh uninstall           # Linux; or: uv run murmly uninstall
.\bootstrap.ps1 uninstall       # Windows; or: uv run murmly uninstall
./setup.sh uninstall --purge   # Linux only: also the environment, models, and configuration
```

## If you moved the murmly folder

If you move the project directory or rebuild its environment, the recorded
path goes stale. Run `murmly install <hotkey>` again from the new location to
repair it; `murmly doctor` shows the path currently recorded. On Linux,
`./setup.sh upgrade` does this for you, rebinding the keys it reads back
rather than asking for them again; Windows has no equivalent upgrade command
yet, so repeat the install command by hand.

See [choosing a hotkey](install.md#choosing-a-hotkey) for what makes a hotkey
valid, and [making murmly speak](making-murmly-speak.md) for what the second
hotkey is for.
