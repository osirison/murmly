# Changing your hotkey

!!! note
    This page assumes murmly is already installed — on Fedora, under KDE
    Plasma, with Python 3.12 or newer, run from a terminal. X11 is verified
    end to end; Plasma Wayland is not.

## Using a different key

Rebind by installing again with a different key:

```bash
uv run murmly install Meta+Shift+Space
```

That rebinds the focused-window hotkey only; a speech-session hotkey keeps the
key it had.

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
./setup.sh uninstall           # or: uv run murmly uninstall
./setup.sh uninstall --purge   # also the environment, models, and configuration
```

## If you moved the murmly folder

If you move the project directory or rebuild its environment, the recorded
path goes stale. Run `murmly install <hotkey>` again from the new location to
repair it; `murmly doctor` shows the path currently recorded. `./setup.sh
upgrade` does this for you, rebinding the keys it reads back rather than
asking for them again.

See [choosing a hotkey](install.md#choosing-a-hotkey) for what makes a hotkey
valid, and [making murmly speak](making-murmly-speak.md) for what the second
hotkey is for.
