---
title: Load gtk4-layer-shell before gi imports, and know that KWin cannot run wtype
description: Preconditions for running overlay_renderer.py by hand on Wayland and for injecting a paste on a Plasma Wayland session
trigger: overlay_renderer.py --check --backend wayland, Gtk4LayerShell.is_supported, RTLD_GLOBAL, wtype, ydotool, ydotoold

depends_on: src/murmly/overlay.py, src/murmly/overlay_renderer.py, src/murmly/integrations.py, README.md
recorded: 2026-08-18
verified_on: Plasma 6 Wayland, Fedora 44, gtk4-layer-shell 1.3.0, GTK 4.22.4, ydotool 1.0.4
---

## Loading gtk4-layer-shell from PyGObject

`gtk4-layer-shell` interposes on `libwayland-client`, so it has to reach the global
symbol scope before libwayland does. PyGObject pulls libwayland in the moment it
imports Gtk. `Gtk4LayerShell.is_supported()` on this machine:

| how it is loaded | supported |
| --- | --- |
| not loaded | `False` |
| `LD_PRELOAD=libgtk4-layer-shell.so.0` | `True` |
| `ctypes.CDLL("libgtk4-layer-shell.so.0", mode=ctypes.RTLD_GLOBAL)` before any gi import | `True` |
| the same call after a bare `import gi` | `True` |
| the same call after `from gi.repository import Gtk` | `False` |
| upstream's form, default `CDLL` mode, before any gi import | `True` |

This is upstream's documented approach for language bindings — its own
`examples/simple-example.py` loads the library this way before importing gi, and
`linking.md` presents `LD_PRELOAD` as the workaround for programs you cannot modify.

Murmly uses the `ctypes` form, in `load_layer_shell()`, called at the top of
`OverlayApplication.__init__` and of `check_visual_runtime` before either imports
`gi`. It needs no environment variable, and a library that is not installed raises
`OSError` naming it instead of printing an `ld.so` line to stderr. It depends on
`overlay_renderer.py` importing `gi` only inside functions — a module-scope
`import gi` would break it silently, so a test parses the file for one.

The failure this exists to prevent: without the library loaded first,
`init_for_window()` **does not raise**. It quietly leaves an ordinary toplevel, which
KWin then centres with the transcript panel stacked over the indicator. The renderer
checks `is_supported()` before building any window and refuses to start rather than
drawing in the wrong place.

Diagnostics have to run the check the way the renderer runs, under
`renderer_environment(backend)`. A check that inherits the caller's environment
answers a different question.


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
