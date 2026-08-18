---
title: The Wayland overlay needs LD_PRELOAD=libgtk4-layer-shell.so.0, and KWin cannot run wtype
description: Preconditions for running overlay_renderer.py by hand on Wayland and for injecting a paste on a Plasma Wayland session
trigger: overlay_renderer.py --check --backend wayland, Gtk4LayerShell.is_supported, wtype, ydotool, ydotoold

depends_on: src/murmly/overlay.py, src/murmly/overlay_renderer.py, src/murmly/integrations.py, README.md
recorded: 2026-08-18
verified_on: Plasma 6 Wayland, Fedora 44, gtk4-layer-shell 1.3.0, GTK 4.22.4, ydotool 1.0.4
---

## Running the overlay renderer by hand on Wayland

`gtk4-layer-shell` must be loaded before `libwayland-client`, which PyGObject pulls
in as soon as the renderer imports Gtk. Nothing inside a running interpreter can
reorder that, so the loader has to be told before the process starts:

```bash
# Reports available: false, "started without libgtk4-layer-shell.so.0 preloaded"
/usr/bin/python3 src/murmly/overlay_renderer.py --check --backend wayland

# Reports available: true
LD_PRELOAD=libgtk4-layer-shell.so.0 \
  /usr/bin/python3 src/murmly/overlay_renderer.py --check --backend wayland
```

The bare soname resolves through the standard loader search path, so no
distro-specific absolute path is needed. An `LD_PRELOAD` naming a library that does
not exist prints an `ld.so` warning and the process still runs.

Without the preload, `Gtk4LayerShell.is_supported()` is `False` and
`init_for_window()` **does not raise** — it quietly leaves an ordinary toplevel,
which KWin then centres on screen with the transcript panel stacked over the
indicator. That was the original bug. `murmly.overlay.renderer_environment()` sets
the preload for the Wayland backend, and the renderer now refuses to start when
Layer Shell is unavailable rather than drawing in the wrong place.

Diagnostics have to run the check under `renderer_environment(backend)`. A check
that inherits the caller's environment answers a different question.

## Injecting a paste on a Plasma Wayland session

`wtype` needs `zwp_virtual_keyboard_manager_v1`. KWin does not advertise it:

```bash
wayland-info | grep -o "interface: '[a-z_0-9]*'" | sort -u | grep -i virtual
# org_kde_plasma_virtual_desktop_management only
```

So `ydotool` is the injector on this desktop. Facts about ydotool 1.0.4, read from
the packaged binaries rather than assumed:

- The client resolves its socket as `YDOTOOL_SOCKET`, else
  `$XDG_RUNTIME_DIR/.ydotool_socket`, else `/tmp/.ydotool_socket`.
- Fedora's `ydotool.service` runs `/usr/bin/ydotoold` as root with no arguments.
  Root has no `XDG_RUNTIME_DIR` under systemd, so the daemon creates
  `/tmp/.ydotool_socket` at the default permission `0600` — a path the user's client
  does not read, and a mode it could not open anyway.
- A systemd drop-in for `ydotool.service` fixes both ends at once, by setting
  `ExecStart=` empty and then
  `ExecStart=/usr/bin/ydotoold --socket-path=$XDG_RUNTIME_DIR/.ydotool_socket
  --socket-own=$(id -u):$(id -g)`.

`murmly doctor` prints the exact commands, filled in for the current session, under
`paste_injection.remedy`. Murmly never runs them: they need root.

## Probing an injector without installing it

`ydotool type ""` connects to the daemon before typing anything, so it exits
non-zero naming the socket path it tried when the daemon is unreachable. That is
what Murmly's injector selection uses to skip an installed-but-unusable tool. The
package can be inspected without installing it:

```bash
dnf download ydotool && rpm2cpio ydotool-*.rpm | cpio -idmv
strings usr/bin/ydotoold | grep -A5 Usage
./usr/bin/ydotool type ""    # prints the socket path it resolved
```

Do that in a scratch directory: `cpio -idmv` extracts `usr/` into the working
directory.
