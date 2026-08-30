## Why

Murmly runs on exactly one configuration: Fedora, KDE Plasma, X11. Everything
else fails, and most of it fails before the daemon finishes starting.

The failures are not a matter of polish. `default_runtime_dir` falls back to
`/run/user/{os.getuid()}` (`src/murmly/config.py:139-144`), and `os.getuid` does
not exist on Windows, so asking for the default socket path raises there before
any feature is reached. `detect_desktop_session` returns `supported=False` with
"Hotkey registration requires KDE Plasma." on every other desktop
(`src/murmly/desktop.py:80-121`). `create_focus_observer` returns a null observer
on any Wayland session (`src/murmly/focus.py:211-224`), so delivery verification
is off for most Linux desktops shipping today. The overlay renderer is spawned
under a hardcoded `/usr/bin/python3` (`src/murmly/overlay.py:27`) because GTK4 and
PyGObject are expected to be distribution packages rather than wheels. Speech
output finds its espeak-ng library by reading `/proc/self/maps`
(`src/murmly/tts.py:135`). The installer writes a systemd unit naming
`pipewire.service` and drives it with `systemctl --user`
(`src/murmly/installer.py:64-84, 335-346`). `setup.sh` installs system packages
only when `dnf` is present (`setup.sh:171-200`), and reads
`/proc/driver/nvidia/version` to decide about the GPU (`setup.sh:121-123`).

So Debian with KDE fails at the package step, Ubuntu with GNOME gets no hotkey,
Alpine gets no memory return from `malloc_trim` (`src/murmly/idle.py:22-44`,
glibc only), and Windows and macOS do not start.

Meanwhile the parts that are hard to port are already portable. `faster-whisper`,
`sounddevice`, `onnxruntime` and `kokoro-onnx` all ship wheels for the three
platforms, and the daemon's own core — capture, silence detection, transcription,
session state, the command protocol — names no operating system anywhere. Seven of
the nine capability specs already contain no Linux, KDE, Wayland, X11 or systemd
reference at all. The coupling is concentrated in the six places where Murmly
touches the desktop, and each of those already has an injection seam the tests use.

## What Changes

**Murmly gains a platform layer, and it is the only thing that knows what it is
running on.** Today six subsystems each detect their environment independently —
`is_wayland_session`, `is_plasma_desktop`, `detect_overlay_backend`,
`detect_desktop_session`, `create_focus_observer` and the injector probe — and each
has its own idea of what "unsupported" means. They are replaced by one resolution
that names the platform, selects a backend for every platform-dependent concern,
and reports both. `murmly doctor` gains a `platform` section naming the resolved
platform and the backend chosen for each concern, so a person on an unfamiliar
system can see what Murmly decided before anything goes wrong.

**The command channel becomes private by the platform's own means.** A UNIX socket
at `0600` under a `0700` directory stays exactly as it is on Linux and macOS. On
Windows there is no `AF_UNIX` in CPython, so the channel is a named pipe whose
security descriptor grants the creating account alone. The requirement does not
change — the channel is reachable only by the account that owns it — but how it is
established and how the peer's identity is read do. `SO_PEERCRED` is Linux-only;
macOS reads the peer through `getpeereid`, Windows through the pipe's own client
token, and the existing "cannot determine the peer" degradation stays for anything
that offers neither.

**Hotkeys are registered by whatever the platform actually offers.** KDE Plasma
keeps the launcher-file mechanism it has. GNOME gains automatic binding through
its custom-keybindings settings. Windows and macOS register the key in the daemon's
own process, so nothing outside Murmly has to be configured. Every other Linux
desktop keeps today's behaviour unchanged: the service installs and the manual
binding instruction is printed, which is the honest outcome when the desktop offers
no programmatic route.

**The daemon is started by the platform's own service manager.** The systemd user
unit is joined by a launchd user agent on macOS and a logon task on Windows.
Install, uninstall, start, and status keep the same meaning on all three; the
`murmly install <hotkey>` and `murmly uninstall` commands do not change shape.

**Clipboard, paste injection, and focus verification gain Windows and macOS
methods.** The probe-then-select machinery in `integrations.py` is already
platform-neutral; what it selects among is not. It gains the native clipboard and
synthetic-keystroke paths for each platform, and the existing rule stands: an
injection method that cannot confirm it delivered must never overwrite the
transcript on the clipboard. Focus verification stops being X11-only — it becomes
available on Windows and macOS, and stays unavailable on Wayland, which offers no
way to ask.

**The overlay is rendered by a backend chosen for the platform.** GTK4 with
layer-shell and EWMH stays what Linux uses. Windows and macOS get a native
always-on-top, click-through, non-focus-stealing surface with the same states,
the same bottom-centred placement, and the same partial-transcript panel. A
platform where the visual runtime is missing still degrades to no overlay rather
than to no Murmly, exactly as it does now.

**Speech output stops finding its library by reading `/proc`.** The espeak-ng
library and its data directory are resolved through a mechanism that exists on all
three platforms. Nothing in the `speech-output` capability changes: its
requirements already speak of "the accelerator" rather than naming one, and already
say that a processor which cannot be used falls back to the CPU with a reason
rather than refusing. That is exactly the behaviour a platform without CUDA needs,
so this is an implementation change and gets no spec delta.

**Every path Murmly writes follows the platform's own convention.** XDG on Linux
stays exactly as documented. macOS uses `~/Library/Application Support` and
`~/Library/Caches`; Windows uses `%LOCALAPPDATA%` and `%APPDATA%`. The
`MURMLY_*` and `XDG_*` overrides that exist today keep working where they apply.

**Linux stops being Fedora.** No code path assumes `dnf`, `rpm`, `/proc`, or glibc.
The installer detects the package manager it is on and names the packages for it,
and prints the list plainly when it recognises none. Where a mechanism needs glibc —
returning freed heap to the system — the platform layer reports that it cannot on
this system rather than silently doing nothing.

**Where a runtime has no build for the machine, Murmly says so in those terms.**
The locked dependencies do not cover every combination of operating system,
processor, and C library. At the pinned versions, `ctranslate2` publishes no musl
or Windows-on-ARM wheel, and `onnxruntime` publishes no musl or Intel-macOS wheel —
which `faster-whisper` also needs, so Intel macOS loses transcription and not just
speech. Three machines therefore cannot run Murmly at all: musl-based Linux,
Windows on ARM, and Intel macOS. macOS support is Apple Silicon on macOS 14 or
newer. So "every Linux distribution" is true of what Murmly assumes and not of what
it can be installed from, and the difference has to be reported rather than hidden.
The installer refuses such a machine before it syncs, naming the runtime and the
characteristic — processor or C library — that has no build for it, rather than
letting a resolver error name a package. A machine missing a runtime outside the
core starts and reports that capability unavailable for the same stated reason.

**Installation runs on all three platforms from one entry point.** `setup.sh` is
bash and calls `dnf`, so it is replaced by a cross-platform installer driven from
`murmly` itself, with thin per-platform wrappers for the one-line bootstrap. It
keeps every property the script exists for: the extras already installed are
carried across a sync, and the ONNX Runtime GPU swap is reapplied afterwards.

**The permissions each platform demands are named before they are needed.** macOS
requires microphone access, and paste injection additionally requires an
Accessibility grant; Windows can block microphone access from its privacy settings.
`murmly install` states what will be asked for, and `murmly doctor` reports whether
each grant is in place, because a denied permission on macOS fails silently and is
otherwise indistinguishable from a bug.

**BREAKING** for anyone who scripts against `murmly doctor`: `session` no longer
reports only `wayland` or `x11`. It becomes a field that can also name a
non-Linux session, and the new `platform` section is where the resolved platform
and its backends are read from.

Left out deliberately: no native installer is built — no MSI, winget manifest,
Homebrew formula, or signed and notarized `.pkg` — because each needs a paid
developer account and a signing pipeline, and none of them is needed to make
Murmly work. No mobile platform, no BSD, no ChromeOS. No new feature: the point of
this change is that what Murmly already does works in more places, not that it does
more.

## Capabilities

### New Capabilities

- `platform-support`: how Murmly determines the platform it is running on, how it
  selects a backend for each platform-dependent concern, where it puts its files on
  each platform, what it does when a concern has no backend or a permission is
  denied, and what it reports about all of that.

### Modified Capabilities

- `command-interface`: the requirement that the command socket is reachable only by
  the account that owns it currently reasons in POSIX file-permission terms alone.
  It gains the case where the platform has no UNIX socket and the channel's privacy
  is established by other means, and the case where the peer's identity is readable
  by a mechanism other than `SO_PEERCRED`. Diagnostics gain the platform section.
- `desktop-integration`: seven requirements change. The daemon's session lifetime
  gains the platform's own per-user service manager and drops the audio-server
  ordering where no service manager can express it. Hotkeys taking effect, the
  refusal of a hotkey another application owns, and the recovery when the daemon is
  not listening all gain the case where the platform registers the key inside
  Murmly's own process — where the binding lives only while the daemon runs, the
  platform may arbitrate the collision itself, and there is no press to recover
  from. Strict hotkey validation gains each platform's own modifier names.
  "Unsupported desktops are refused rather than silently skipped" gains the
  distinction between a platform that registers no hotkeys and a desktop that
  offers no route. Reporting whether a transcript can be pasted gains an ungranted
  permission as a reason distinct from an absent tool. Verification of a binding,
  rebinding, uninstall, and the remaining requirements are already written without
  naming a platform and are left exactly as they are.
- `transcript-delivery`: injection method is chosen by what the session can execute,
  which today means a fixed list of Linux tools. It gains the platform's own
  methods, and the case where an injection method exists but the platform has not
  granted permission to use it. Delivery target verification stops being tied to
  X11.
- `recording-overlay`: the capability's purpose and its placement requirement both
  name KDE Plasma X11 and Wayland specifically. Placement, stacking, focus and input
  behaviour become requirements about the platform's own presentation, with the
  visual outcome unchanged.
- `model-residency`: releasing must return memory to the system rather than to an
  internal pool. Whether that is possible depends on the platform's allocator, so
  the requirement gains what Murmly does and reports where it cannot ask.
- `project-website`: the page is required to state that Murmly targets Fedora, that
  hotkeys and the overlay need KDE Plasma, and that X11 is verified while Wayland is
  not. That disclosure has to become the true one.

## Impact

| Area | Change |
| --- | --- |
| `src/murmly/platform/` (new) | The resolution that names the platform and selects each backend, plus the per-platform backends themselves |
| `src/murmly/config.py` | Path defaults resolve per platform; `os.getuid` leaves the import path |
| `src/murmly/daemon.py` | Socket creation, directory privacy, and peer identity move behind the transport backend |
| `src/murmly/installer.py` | The systemd unit and KDE launcher become one implementation of a service and hotkey backend among several |
| `src/murmly/desktop.py` | Plasma shortcut queries become one hotkey backend; GNOME, Windows and macOS join it |
| `src/murmly/hotkey.py` | Qt key codes become one encoding of a parsed hotkey, alongside the Windows and macOS ones |
| `src/murmly/integrations.py` | The candidate lists for clipboard and injection become per-platform |
| `src/murmly/focus.py` | `X11FocusObserver` joins Windows and macOS observers behind the existing protocol |
| `src/murmly/overlay.py`, `src/murmly/overlay_renderer.py` | Backend selection widens; the renderer stops assuming a system interpreter |
| `src/murmly/tts.py` | espeak-ng resolution stops reading `/proc`; the CUDA library list becomes per-platform |
| `src/murmly/stt.py` | CUDA library loading becomes per-platform; `.so` names are not the only ones |
| `src/murmly/idle.py` | `malloc_trim` becomes one way of returning memory, reported when unavailable |
| `src/murmly/cli.py` | `doctor` gains `platform`; recovery stops calling `systemctl` directly |
| `hooks/murmly-announce.py` | Detaching stops depending on `os.fork`; the socket path resolves per platform |
| `pyproject.toml` | The `cuda` extra gains a platform marker; per-platform desktop dependencies are declared |
| `setup.sh` | Replaced by a cross-platform installer with per-platform bootstrap wrappers |
| `README.md`, `docs/`, the project page | Requirements, install, and scope stop describing one distribution |
| `tests/` | Platform detection joins environment injection as a thing tests substitute |
