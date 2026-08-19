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

That does **not** mean ydotool is required. `xdotool` works on this desktop, verified
end to end: a GTK4 window under `GDK_BACKEND=wayland` received the clipboard contents
from `xdotool key --clearmodifiers ctrl+v`. KWin hands XWayland an EIS socket and
XWayland feeds XTEST through it, so the events go into KWin's own input redirection
rather than only to X11 clients:

```bash
tr '\0' '\n' < /proc/$(pgrep -x Xwayland)/environ | grep LIBEI
# LIBEI_SOCKET=/proc/self/fd/122
ldd /usr/bin/Xwayland | grep libei
# libei.so.1 => /lib64/libei.so.1
```

Three behaviours to know before relying on it:

- **The first attempt raises a consent dialog** (`/usr/libexec/kwin_eis_prompter`).
  Until it is answered the events queue, and a one-shot `xdotool` has already exited,
  so that first paste never lands. Ticking "Always allow apps claiming to be X" writes
  `XwaylandEisNoPromptApps` to `kwinrc`; without the tick the grant lasts the session.
  Revocable under System Settings, "Legacy X11 App Support".
- **Exit 0 does not mean delivered.** With consent outstanding, two `xdotool` calls
  returned 0 while the target window received nothing — XWayland falls back to plain
  XTEST when the EIS connection is refused. Murmly marks this method
  `confirms_delivery=False` and never restores the clipboard over a transcript
  delivered by it.
- **Probe without a keystroke.** `xdotool getdisplaygeometry` opens an X connection and
  issues no XTEST request, so probing cannot trip the prompt as a side effect. Checked
  against the journal for `kwin_eis_prompter` after a probe.

Injection latency: 14 ms median over ten runs.

`ydotool` is only needed on a compositor with neither route. Facts about ydotool 1.0.4,
read from the packaged binaries:

- The client resolves its socket as `YDOTOOL_SOCKET`, else
  `$XDG_RUNTIME_DIR/.ydotool_socket`, else `/tmp/.ydotool_socket`.
- Fedora's `ydotool.service` runs `/usr/bin/ydotoold` as root with no arguments and
  `WantedBy=default.target`. Root has no `XDG_RUNTIME_DIR` under systemd, so the daemon
  creates `/tmp/.ydotool_socket` at the default permission `0600` — a path the user's
  client does not read, at a mode it could not open.
- **Do not fix that with a drop-in pointing at `$XDG_RUNTIME_DIR`.** The unit starts at
  boot, `/run/user/$UID` is a tmpfs logind mounts at login, so the daemon fails until
  someone logs in and `Restart=always` spins it meanwhile. Grant `/dev/uinput` to the
  user with a udev `uaccess` rule and run `ydotoold` as a user service instead, where
  it picks up `XDG_RUNTIME_DIR` and needs no flags at all. Preconditions checked here:
  `uaccess` works on this box (`getfacl /dev/dri/card1` shows `user:qp:rw-`),
  `/dev/uinput` is `crw------- root root` with no ACL, and no packaged rule claims it.
  The arrangement itself is **unverified** — no root available in that session.


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
