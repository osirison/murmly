## Context

See `proposal.md` — Why. What shapes the approach is that the port is not evenly
distributed: the daemon's core is already platform-neutral, and the coupling sits
in six subsystems that each detect their own environment.

Three facts constrain every decision below.

**The seams already exist.** `FocusObserver` is a Protocol with a null
implementation and a `create_focus_observer` factory (`src/murmly/focus.py:38-62,
211-224`). `OverlayLifecycle` is a Protocol with `NullOverlayController`, and
`MurmlyDaemon._create_overlay` already falls back to it whenever
`detect_overlay_backend()` returns `None` (`src/murmly/overlay.py:64-77`,
`src/murmly/daemon.py:2090-2103`). Every subprocess-calling class takes its command
runner and binary name as a constructor parameter — `UserService(run_command=,
systemctl=)`, `PlasmaShortcuts(run_command=, busctl=)`,
`select_paste_injection(env=, which=, run=)`. `peer_identity_supported()` already
asks whether the platform can do something rather than assuming it
(`src/murmly/daemon.py:278-296`). The port populates these seams; it does not cut
new ones.

**What is missing is the thing above them.** There is no `Platform` value. Six
functions detect independently — `is_wayland_session`, `is_plasma_desktop`,
`detect_overlay_backend`, `detect_desktop_session`, `create_focus_observer`, and
the injector probe — and each has its own notion of unsupported. That is why
`murmly doctor` can only report `session` as `wayland` or `x11`
(`src/murmly/cli.py:463`): there is nothing else for it to ask.

**The dependency graph is already cross-platform; the desktop surface is not
packaged at all.** Read from `uv.lock` at the pinned versions:

| Runtime | Linux glibc | Linux musl | Win x64 | Win ARM64 | macOS arm64 | macOS x86_64 |
| --- | --- | --- | --- | --- | --- | --- |
| `ctranslate2` 4.8.1 | yes | **none** | yes | **none** | yes | yes |
| `onnxruntime` 1.28.0 | yes | **none** | yes | yes | yes (14.0+) | **none** |
| `espeakng-loader` 0.2.4 | yes | **none** | yes | yes | yes | yes |
| `nvidia-*-cu12` (the `cuda` extra) | yes | none | yes | none | **none** | **none** |
| `sounddevice` 0.5.5 | pure Python, needs system PortAudio | same | bundled | bundled | bundled | bundled |

Two entries in that table decide more than they look like they do.
`faster-whisper` depends on `onnxruntime` for voice activity detection, so the
missing Intel-macOS `onnxruntime` wheel is not a speech-output gap — it is the
whole product. `onnxruntime` shipped `macosx_13_0_x86_64` at 1.23.1 and stopped at
1.24.1; from 1.24 onward the only macOS wheel is `macosx_14_0_arm64`. That sets
macOS support at **Apple Silicon, macOS 14 or newer**, and it is a decision by
absence rather than one this change gets to make.

Meanwhile GTK4, PyGObject, cairo, gtk4-layer-shell, libX11, libXext, espeak-ng,
`busctl`, `systemctl`, `xdotool`, `wtype`, `ydotool`, `xclip` and `wl-clipboard`
are not pip dependencies at all. They are `dnf` packages installed by `setup.sh`
and reached through `ctypes`, `subprocess` and `shutil.which`, which is why the
overlay renderer runs under a hardcoded `/usr/bin/python3`
(`src/murmly/overlay.py:27`).

## Goals / Non-Goals

**Goals:**

- One platform resolution that every platform-dependent decision is made from, and
  that a test can supply rather than discover.
- Per-concern backends that plug into the Protocol and injection seams already
  present, so the verified Linux paths are not rewritten to gain the others.
- A support matrix that is stated, reported by `murmly doctor`, and true — with
  the gaps named as gaps.
- Landing order that keeps `main` working: the abstraction and distro-agnostic
  Linux first, then Windows, then macOS, each independently shippable.

**Non-Goals:**

- No unified `Platform` abstract base class with one implementation per operating
  system. The axes do not line up: a Linux/GNOME/Wayland machine mixes a GNOME
  hotkey backend, a Wayland clipboard, no focus observer, and a GTK4 overlay, and
  a class per OS would fan back out into exactly these per-concern choices while
  making a mixed configuration inexpressible.
- No change to the command protocol, the speech session protocol, the
  configuration schema, or the CLI surface. `platform-support` requires them to
  stay identical, and this design does not touch them.
- No rewrite of the GTK4 renderer. It is the verified path and it stays.
- No behaviour change on Fedora + KDE + X11. That configuration must come out of
  this change doing exactly what it does now.

## Decisions

### One resolution, per-concern backends — not one class per operating system

A single `PlatformProfile` is resolved once, from `sys.platform` plus a supplied
environment mapping, and carries: the operating system, the processor
architecture, the C library where it matters, and for Linux the session type and
desktop. Every concern then selects its backend from that one value through its
own small registry.

The registry stays per concern because that is where the variation is. A hotkey
backend is chosen by desktop on Linux and by operating system elsewhere; an overlay
backend by display protocol on Linux and by operating system elsewhere; a focus
observer by display protocol on Linux and by operating system elsewhere; a
transport by operating system alone. Collapsing those onto one axis would require
a class per combination.

The resolution takes its environment as a parameter, exactly as
`is_wayland_session(env=)` and `detect_desktop_session(env=)` already do. That is
what makes Windows and macOS backend selection testable from a Linux CI runner,
and it is the pattern the suite already uses everywhere
(`tests/test_desktop.py:191-221`, `tests/test_overlay.py:215-282`).

*Alternative rejected — `platform.system()` checks at each call site.* That is
what the code does today with `is_wayland_session`, and the reason `doctor` cannot
describe its own environment.

### The command channel: a UNIX socket where there is one, a named pipe with an explicit DACL on Windows

CPython does not expose `socket.AF_UNIX` on Windows. Windows has supported it at
the Winsock level since build 17063, but `python/cpython#77589` is still open and
the current implementation attempt, `python/cpython#137420`, targets 3.16 — so it
is unavailable for every version in scope. Linux and macOS keep the socket exactly
as it is, including the path-privacy analysis in `command-interface`.

Windows uses a named pipe created with a security descriptor whose DACL grants
only the creating user's SID, through `win32pipe.CreateNamedPipe` and
`win32security`.

*Alternative rejected — `multiprocessing.connection`.* Its Windows pipes are
created with a NULL security descriptor, which is the OS default DACL and grants
read access to `Everyone`. Its actual protection is an application-layer HMAC
challenge keyed by `authkey`, not an OS access check. The requirement is that no
other account can reach the channel, and a shared secret in a process's memory is
a different guarantee from one the kernel enforces.

*Alternative rejected — localhost TCP with a token.* It binds a port every account
on the machine can connect to, and moves the whole guarantee into a secret. The
socket starts and stops the microphone; that is not the property to weaken.

Peer identity is read three ways behind the existing `peer_identity_supported()`
guard: `SO_PEERCRED` on Linux, `getpeereid(3)` through `ctypes.CDLL(None)` on
macOS — which returns UID and GID but no PID, so the identity check uses the UID
it already compares — and the pipe's client process token on Windows.

### Four hotkey backends, and the two new ones live inside the daemon

| Platform | Mechanism | Permission |
| --- | --- | --- |
| KDE Plasma | `.desktop` launcher with `X-KDE-Shortcuts`, unchanged | none |
| GNOME | `org.gnome.settings-daemon.plugins.media-keys` `custom-keybindings`, with `name`/`command`/`binding` on the per-binding relocatable schema | none |
| Windows | `RegisterHotKey` on a message-loop thread in the daemon | none |
| macOS | Carbon `RegisterEventHotKey`, via `ctypes` into HIToolbox | none |
| Any other Linux desktop | none — service installs, manual instruction printed | — |

GNOME's mechanism is a live dconf value, applies without a logout, and works under
Wayland, which is what makes it worth having: it covers the largest Linux desktop
Murmly currently declines.

macOS uses Carbon rather than a `CGEventTap` specifically to stay permission-free.
A default-mode tap requires Accessibility and a listen-only tap requires Input
Monitoring, whereas `RegisterEventHotKey` requires neither, because the process
only learns that one registered combination fired and never sees other input.
Carbon Event Manager is deprecated and still functional, and is what Electron, VS
Code and Slack use for this. The cost is a real limitation, recorded below.

Windows and macOS register in Murmly's own process, so the binding exists only
while the daemon runs. That is the behaviour difference the `desktop-integration`
delta records, and it removes the recovery path: on Linux a keypress launches
`murmly toggle`, which can start a stopped service, and in-process there is no
press to recover from. Installation therefore starts the daemon before reporting a
hotkey bound, the hotkey is released when the daemon stops, and `doctor` reports a
hotkey as not currently held with the daemon named as why.

*Alternative deferred — the `org.freedesktop.portal.GlobalShortcuts` portal.* One
mechanism for both Linux desktops, and the eventual replacement for both backends
above. Not now: it landed in GNOME only at 48 (March 2025), so a GNOME backend
built on it alone would cover fewer machines than gsettings does, and it raises a
consent dialog where the current KDE path raises none. Recorded as the successor,
not built here.

### Clipboard and paste injection: native APIs, and both new injectors are unconfirmable

Windows clipboard goes through the Win32 API with `CF_UNICODETEXT`, not `clip.exe`.
`clip.exe` reads stdin in the console's OEM/ANSI codepage and mangles anything
outside it, which for a transcription tool is a defect in the product's whole
purpose. macOS uses `NSPasteboard`.

Both new injectors fall under the existing requirement that an unconfirmable
method must not overwrite the transcript, for the same reason `xdotool` does:

- Windows `SendInput` returns success, but UIPI silently discards synthetic input
  aimed at a window belonging to a higher-integrity process. An unelevated Murmly
  pasting into an elevated window gets no error and delivers nothing.
- macOS `CGEventPost` of Cmd+V without an Accessibility grant does not fail. The
  event is dropped, and no dialog appears on its own.

So on both, the transcript stays on the clipboard and the previous contents are not
restored over it. No new rule is needed; the rule written for KDE's input-consent
dialog already covers this exactly.

The Accessibility grant is checked with `AXIsProcessTrusted()`, which reports
without prompting, and requested with `AXIsProcessTrustedWithOptions(prompt: true)`
only from `murmly install`. The daemon never prompts: a permission dialog raised by
a background process the person did not just invoke is one they cannot connect to
anything.

### Focus observation by owning application, never by window title

Windows uses `GetForegroundWindow`, `GetWindowThreadProcessId` and
`QueryFullProcessImageName`, none of which needs a permission for a process the
same user owns. macOS uses `NSWorkspace.sharedWorkspace().frontmostApplication()`,
which returns bundle identifier, PID and localized name as unprotected metadata.

`CGWindowListCopyWindowInfo` is deliberately not used. Since macOS 10.15 it omits
the `kCGWindowName` key — the window title — unless the process holds Screen
Recording permission, while owner PID and owner name remain available. Murmly's
delivery target is the application and process that held focus, which is what the
X11 observer already records (`_NET_WM_PID` and `WM_CLASS`), so nothing here needs
a title and nothing here needs that grant.

Wayland remains unobservable and keeps returning the null observer.

### The overlay: keep GTK4 on Linux, add a Qt renderer for Windows and macOS

The renderer is already a separate process speaking newline-delimited JSON over a
`socketpair` (`src/murmly/overlay.py:366-390`). A second renderer implementation
plugs into that boundary without the daemon knowing. `SYSTEM_PYTHON` stops being a
constant and becomes the interpreter the selected renderer needs — Fedora's
`/usr/bin/python3` for the GTK4 one, because PyGObject and GTK4 are distribution
packages, and the project's own interpreter for the Qt one, because PySide6 is a
wheel.

Qt gives `Qt.WindowTransparentForInput` and `Qt.WindowDoesNotAcceptFocus` as
documented cross-platform flags, which are precisely the two properties the
overlay needs and cannot compromise on. On Windows the native `HWND` additionally
takes `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`
through `SetWindowLongPtr`.

*Alternative rejected — one Qt renderer everywhere.* It would discard a verified
layer-shell and EWMH implementation to gain uniformity, and add a large wheel to
the platform that already has a working system toolkit.

*Alternative rejected — tkinter.* It is available: `python-build-standalone`, which
is what `uv` installs, ships Tk on all three platforms. The problem is what it can
express. `-transparentcolor` and `-toolwindow` are Windows-only `wm` attributes,
`-alpha` is whole-window opacity with no per-pixel control, and macOS tkinter offers
no colour-key or click-through equivalent at all — so the one property the overlay
must not compromise on cannot be built with it on macOS. Its Linux build also
statically links libX11/libxcb, which collides with a separately loaded system
libX11 in the same process.

*Alternative rejected — GTK4 on Windows and macOS.* Windows is workable through
MSYS2; macOS needs `gtk-osx` and `gtk-mac-bundler` with no wheel path at all.

The macOS presentation carries a risk recorded below.

### Service management: the platform's own per-user manager, and no administrative rights

| Platform | Mechanism | Lifecycle verbs |
| --- | --- | --- |
| Linux | systemd user unit, unchanged | `systemctl --user` |
| macOS | `~/Library/LaunchAgents/<label>.plist` with `Label`, `ProgramArguments`, `RunAtLoad`, `KeepAlive` | `launchctl bootstrap gui/$UID`, `print`, `kickstart -k`, `bootout` |
| Windows | Task Scheduler logon trigger | `schtasks /create /sc onlogon`, `/query`, `/run`, `/end`, `/change` |

macOS uses `launchctl bootstrap`, not `launchctl load`. `load` has been deprecated
since the 10.10 launchd rewrite and its failure mode is the disqualifying one: on a
malformed plist it exits 0 and does nothing, which would make installation report
success for a service that will never start — exactly what "a binding is verified
before installation reports success" exists to prevent.

Windows uses Task Scheduler rather than the Startup folder or `HKCU\...\Run`
because it is the only per-user autostart of the three with CLI verbs for start,
stop, status, enable and disable. `UserService.is_active()` and `status()` have to
answer, and the other two mechanisms can only report whether a file or registry
value exists.

Neither needs administrative rights, which the `desktop-integration` delta now
requires.

Neither expresses an ordering against the audio server the way the systemd unit's
`After=pipewire.service wireplumber.service` does. Murmly already has to survive
the audio server going away first — that is what
`disable_portaudio_exit_teardown()` is for (`src/murmly/audio.py:28-66`) — so the
ordering is an optimisation on Linux rather than a dependency, which is what the
spec delta now says.

### Paths: three branches in `config.py`, not a dependency

`default_config_path`, `default_tts_model_dir` and `default_runtime_dir` gain a
branch each:

| | Linux | macOS | Windows |
| --- | --- | --- | --- |
| config | `$XDG_CONFIG_HOME/murmly` or `~/.config/murmly` | `~/Library/Application Support/murmly` | `%APPDATA%\murmly` |
| data | `$XDG_DATA_HOME/murmly` or `~/.local/share/murmly` | `~/Library/Application Support/murmly` | `%LOCALAPPDATA%\murmly` |
| runtime | `$XDG_RUNTIME_DIR` or `/run/user/<uid>` | `~/Library/Caches/murmly` | the pipe namespace, no filesystem path |

*Alternative rejected — `platformdirs`.* It is small and canonical, and it is the
obvious choice for a project starting today. Here it would resolve the Linux paths
by its own rules rather than by the three lines in `config.py:139-166` that are
already installed on people's machines, and a silently moved config file is the one
regression this change must not produce. Three branches that keep the existing
Linux answers byte-identical by construction cost less than proving an equivalence.

`os.getuid()` moves out of the Linux branch, which is what stops `config.py` from
raising on import under Windows.

The transcription model cache is deliberately left where it is. `faster-whisper`
resolves it through `huggingface_hub`, whose default is `~/.cache/huggingface/hub`
on every operating system — not `~/Library/Caches` and not `%LOCALAPPDATA%`.
`WhisperModel(download_root=)` could move it, and moving it would strand the 1.6 GB
already cached on every existing Linux install and re-download it. It is the
Hub's cache rather than one of Murmly's own locations, so `doctor` reports the path
it resolved to and nothing writes a new one.

### CUDA loading, and what has no build where

`load_cuda_libraries` loads `.so` files by relative wheel path with
`RTLD_GLOBAL` (`src/murmly/stt.py:22-26, 92-104`). All six `nvidia-*-cu12` packages
the `cuda` extra pins publish `win_amd64` wheels, so CUDA transcription is
available on Windows — but not by the same mechanism. `RTLD_GLOBAL` is POSIX symbol
visibility and has no Windows counterpart; what Windows needs is
`os.add_dll_directory` over each `nvidia/*/bin` directory before the runtime is
imported, because since Python 3.8 Windows does not search `PATH` for an extension
module's DLL dependencies. So this is a second implementation, not a translated
one.

None of the six publishes a macOS wheel of any architecture, so resolving the extra
on macOS fails outright rather than falling back. The extra gains
`sys_platform != 'darwin'` on every requirement — the same marker `kokoro-onnx`
uses on its own GPU extra.

On macOS, CTranslate2 has no GPU backend at all: its acceleration there is CPU
dispatch across Apple Accelerate, oneDNN and Ruy. `onnxruntime`'s stock macOS wheel
does carry the CoreML execution provider, so synthesis has an accelerator on macOS
even though transcription does not — which is exactly why `[tts] device` and
`[stt] device` are already separate settings, and why `speech-output` needs no
delta: "the accelerator" is whatever this platform's is.

Where a runtime has no build for the machine, the `platform-support` delta requires
Murmly to say so in those terms. From the table in Context, three machines have no
build of something they need, and two of the three cannot run Murmly at all:

| Machine | Missing | Outcome |
| --- | --- | --- |
| musl Linux (Alpine and the like) | `ctranslate2`, `onnxruntime`, `espeakng-loader` | Not supported. Transcription has no runtime. |
| Windows on ARM | `ctranslate2` | Not supported. Transcription has no runtime. |
| Intel macOS | `onnxruntime`, which `faster-whisper` also needs | Not supported. Not a speech-output gap — the transcription stack will not install. |

These fail at install rather than at run: `uv sync` cannot resolve them, and its
resolver error names a package rather than the situation. So the installer checks
the machine against this table before it syncs and refuses with the reason, and
`doctor` reports the same thing on a machine where an environment was carried over
from elsewhere. The runtime check the spec requires is the second line of defence,
not the first.

### espeak-ng: keep preferring the platform's, and spike the bundled wheel

`resolve_espeak()` finds the system library with `ctypes.util.find_library`, its
real path by reading `/proc/self/maps`, and its data directory by parsing
`espeak-ng --version` (`src/murmly/tts.py:104-159`). It deliberately avoids the
bundled `espeakng-loader` wheel because that wheel's library has its data directory
compiled in as the path on the machine that built it, ignores
`EspeakWrapper.set_data_path()`, and fails by printing to stderr and returning no
audio — recorded in `docs/agent-notes/espeakng-loader-data-path.md`.

`/proc/self/maps` is Linux-only and has to go regardless; `dlinfo`, already in the
lock as a transitive dependency, answers the same question portably.

The larger question is whether the bundled wheel can be made to work through
espeak-ng's `ESPEAK_DATA_PATH` environment variable, which the library reads when
its caller passes no path. If it can, speech output needs no system package on any
platform and the whole class of "install espeak-ng from your distribution" remedies
disappears. That is a spike, not an assumption. Until it resolves, the fallback is
what Linux does today, per platform: the distribution package, Homebrew on macOS,
and the espeak-ng installer or winget package on Windows — reported by `doctor`
through the existing "runtime absent, here is what to install" path, which already
handles it.

### Installation: `murmly install` grows, `setup.sh` shrinks to a bootstrap

`setup.sh` is 814 lines of bash that gates on `dnf`, reads
`/proc/driver/nvidia/version`, and drives `systemctl`. What it exists to get right
— carrying the already-installed extras across a `uv sync`, and reapplying the ONNX
Runtime GPU swap that every sync undoes — is not shell work and does not vary by
platform. It moves into `murmly` itself, where it is testable.

What stays per platform is only the bootstrap: install `uv`, then hand off. That is
a few lines of `sh` and a few of PowerShell.

Linux system packages stop assuming `dnf`. The package manager is detected — `dnf`,
`apt`, `pacman`, `zypper`, `apk` — and the package names for that manager are named
in the command it prints. Where none is recognised, the list is printed plainly,
which is what the script already does when `dnf` is absent (`setup.sh:171-200`).

### Testing: the platform becomes an injected value

The suite has never distinguished operating systems because it never had to — there
is no `sys.platform` or `platform.system()` check anywhere in `src/` or `tests/`.
It already isolates itself from desktop specifics through environment-dict
injection and injected command runners, and that is the same seam.

So `PlatformProfile` is a value a test constructs, and every backend registry is
exercised for every platform from any machine. What cannot be faked keeps the
existing pattern: a runtime `self.skipTest(...)` inside the test body when the live
session is absent, as `X11RuntimeIntegrationTests` does (`tests/test_focus.py:160-190`),
now also skipping on the wrong operating system. CI runs the suite on Linux,
Windows and macOS runners; the session-dependent tests skip on all three exactly as
they skip today.

## Risks / Trade-offs

**The macOS overlay may not be buildable on top of Qt.** `NSPanel` with
`NSWindowStyleMaskNonactivatingPanel`, `setLevel_`, `setIgnoresMouseEvents_` and an
all-Spaces `collectionBehavior` are documented AppKit APIs, but applying them to a
Qt widget's underlying `NSWindow` through handle reflection is a community
technique with no citable confirmation. → Spike it before the macOS overlay task,
against Qt's own flags first. If Qt's flags alone do not give a non-activating,
click-through, all-Spaces panel, the fallback is a PyObjC renderer built directly
on `NSPanel`, which is more code but uses the documented APIs directly. The
renderer is a separate process behind a JSON protocol, so this choice does not
reach the daemon either way. The spec already requires no overlay rather than one
that takes input, so the worst outcome is a reported absence.

**macOS hotkeys can be swallowed by the foreground application.**
`RegisterEventHotKey` only fires when the frontmost app does not consume the
combination itself, and cannot express modifier-only chords. → Accept it, and make
it legible: `doctor` reports the mechanism and its limitation, and the hotkey
verification that already refuses to claim a keypress will be delivered covers the
case. Moving to a `CGEventTap` would fix it at the cost of an Accessibility or
Input Monitoring grant for the hotkey itself, which is a much larger permission ask
than the paste injection grant, and is the trade to revisit only if this proves to
bite in practice.

**Windows paste into an elevated window silently does nothing.** UIPI drops the
synthetic input, and running Murmly elevated to fix it would be worse. → It is
already covered: `SendInput` is registered as an unconfirmable method, so the
transcript stays on the clipboard and the person can paste it themselves. Document
it in the same place the KDE consent dialog is documented.

**A macOS daemon may get no microphone at all, silently.** This is the largest risk
in the change, because it defeats the core rather than a peripheral. macOS gates
microphone access through TCC, which attributes a request to a signed bundle
carrying an `NSMicrophoneUsageDescription`. A bare Python process started by a
launchd agent has no bundle and no such string: reports converge on the failure
being silent — no dialog, no exception, an open stream delivering zeroes. A process
run from a terminal usually works only because it inherits the terminal
application's own grant, which is why this would not show up in development. →
Establish it first, before any other macOS work, with a spike that runs the daemon
under launchd and checks whether audio arrives. Two documented routes if it does
not: set `AssociatedBundleIdentifiers` in the launchd plist so TCC attributes the
agent to a bundle that holds the grant, or ship a minimal `.app` wrapper with the
usage-description string. Until one is proven, macOS capture is not claimed.
Whatever the answer, `doctor` must distinguish "microphone denied" from "no audio
device", because the two look identical from inside the process.

**Full parity was chosen, and the wheels do not deliver it everywhere.** Windows on
ARM, musl Linux, and Intel macOS cannot run Murmly at all at the pinned versions. →
Not fixable inside this change: they are upstream build matrices, and for Intel
macOS the window has closed rather than not yet opened — `onnxruntime` shipped that
wheel until 1.23.1 and stopped. Refuse each at install time with the reason, record
all three as stated limits in the README and on the project page, and do not
present a parity that does not exist. Pinning `onnxruntime` back to 1.23.1 to keep
Intel macOS is possible and is not proposed: it would hold every platform to a
year-old runtime for one that Apple itself has stopped shipping.

**Windows install has two environment preconditions.** `uv sync` fails on a machine
without long-path support enabled, and reports "the system cannot find the path
specified" rather than naming the length; and `huggingface_hub`'s cache uses
symlinks, which without Developer Mode degrade to full copies, doubling the 1.6 GB
model on disk. → Check both in the Windows bootstrap before syncing, and name each
precisely. Neither is a defect Murmly can fix, and both are ones it can refuse to
be confusing about.

**Windows named-pipe security descriptors need `pywin32`.** A new platform-gated
dependency on the transport, which is the one component that must never be
subtly wrong. → Marker it to `sys_platform == 'win32'` so no other platform
resolves it, and pin it. Test the DACL by connecting as the same account and
asserting the descriptor's contents, since a second account is not available in
CI.

**Two overlay renderers to keep in step.** The GTK4 one and the Qt one have to
present the same states, dimensions and lifecycle, and only one of them is
exercised on any given machine. → The protocol between daemon and renderer is
already the contract. Pin it with tests that drive each renderer's message
handling without a display, and keep the visual states enumerated in one place both
read.

**The change is large enough to stall.** Eight capability deltas and three
platforms. → Phase it so each phase is shippable on its own: the platform layer and
distro-agnostic Linux land with no new platform at all and no behaviour change,
then Windows, then macOS. `main` is never mid-port.

## Migration Plan

1. **Phase 1 — the layer, and Linux stops being Fedora.** `PlatformProfile` and the
   per-concern registries land with exactly the backends that exist today behind
   them, plus the GNOME hotkey backend, package-manager detection, and the removal
   of `/proc`, `dnf`, glibc and `/usr/bin/python3` assumptions. `doctor` gains its
   `platform` section. No new operating system is claimed. A Fedora + KDE + X11
   machine must be indistinguishable before and after — that is the phase's
   acceptance test.
2. **Phase 2 — Windows.** Named-pipe transport, `RegisterHotKey`, Task Scheduler,
   Win32 clipboard and `SendInput`, the Qt renderer, `%APPDATA%` paths, the
   PowerShell bootstrap, and a Windows CI runner.
3. **Phase 3 — macOS.** The TCC microphone spike first, because everything else is
   worthless if a launchd-started daemon cannot record. Then `getpeereid`, Carbon
   hotkeys, launchd, `NSPasteboard` and `CGEventPost` with the Accessibility grant,
   `NSWorkspace` focus, the overlay after its own spike, `~/Library` paths, and a
   macOS CI runner.
4. **Phase 4 — the documentation and the page.** README, the project page, and the
   scope statement stop describing one distribution, and state the support matrix
   including its gaps.

Each phase is a separate reviewable unit that leaves `main` working. Rollback for
phase 1 is reverting the platform layer, since every backend behind it is the code
that is there now. Rollback for phases 2 and 3 is removing that platform's
registry entries: no other platform's path runs through them.

## Open Questions

- **Does the bundled `espeakng-loader` library carry the same compiled-in data-path
  defect on Windows and macOS that it carries on Linux?** The defect is confirmed
  only on Fedora. If the Windows and macOS wheels are sound — or if
  `ESPEAK_DATA_PATH` overrides the compiled-in path where
  `EspeakWrapper.set_data_path()` does not — speech output needs no system package
  on those platforms and possibly on none. If not, each platform names its own
  espeak-ng install: Homebrew on macOS, and on Windows the espeak-ng installer,
  which has no standard package-manager route. Deferrable: the spec requires only
  that an absent runtime is reported with what to install, and every answer
  satisfies it. The spike is a task in phases 2 and 3.

The question about `nvidia-*-cu12` wheels on Windows is resolved rather than open:
all six publish `win_amd64`, none publishes any macOS wheel. It is recorded under
Decisions.
