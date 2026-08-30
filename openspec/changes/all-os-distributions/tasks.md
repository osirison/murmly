Phases 1 and 2 land with no new operating system claimed and must leave a
Fedora + KDE + X11 machine indistinguishable from before. Phases 3 and 4 are each
shippable on their own. See `design.md` — Migration Plan.

## 1. The platform layer

- [ ] 1.1 Add `src/murmly/platform/__init__.py` with a `PlatformProfile` value carrying the operating system, processor architecture, C library where it is determinable, and — on Linux — the session type and desktop
- [ ] 1.2 Resolve it from `sys.platform` plus a supplied environment mapping, defaulting to `os.environ`, so a test can construct any platform from any machine, matching the `env=` parameter every existing detector already takes
- [ ] 1.3 Resolve it once per process and pass it, rather than re-resolving at each call site — this is the thing whose absence lets subsystems disagree
- [ ] 1.4 Add a per-concern backend registry for each of: command channel, service management, hotkey registration, clipboard, paste injection, focus observation, overlay, and speech synthesis. Keep them separate registries: a hotkey is chosen by desktop on Linux and by operating system elsewhere, and one shared axis cannot express that
- [ ] 1.5 Re-express `is_wayland_session`, `is_plasma_desktop`, `detect_overlay_backend` and `detect_desktop_session` as readings of the resolved profile rather than as independent detectors, keeping their existing signatures so nothing calling them changes yet
- [ ] 1.6 Register every backend that exists today — Plasma hotkeys, systemd service, X11 focus, GTK4 overlay, the Wayland and X11 clipboard and injector lists — behind the registries, unchanged
- [ ] 1.7 Refuse an unsupported operating system from `cli.main` before any command runs, naming the platform found and the platforms supported, and writing nothing
- [ ] 1.8 Add the machine-capability check: refuse at startup, naming the runtime and the characteristic that has no build, where transcription's runtime is unavailable for this operating system, processor, or C library

## 2. Paths, and the end of `os.getuid` on the import path

- [ ] 2.1 Give `default_config_path`, `default_tts_model_dir` and `default_runtime_dir` a branch per platform, keeping the Linux answers byte-identical to what `config.py:139-166` produces today — an existing install must not see its configuration move
- [ ] 2.2 Move `os.getuid()` inside the Linux branch, so importing `config` on Windows no longer raises
- [ ] 2.3 Honour each platform's own environment override — `XDG_*` on Linux, and the platform equivalents elsewhere — and report the path actually in use rather than the default
- [ ] 2.4 Report a location Murmly needs but cannot create or write by naming that location and what failed, rather than failing later somewhere that does not mention it
- [ ] 2.5 Leave the transcription model cache where `huggingface_hub` puts it, and report the resolved path in diagnostics — moving it would strand the 1.6 GB already cached on every existing install
- [ ] 2.6 Resolve the announce hook's socket path through the same platform resolution instead of its own copy of the `XDG_RUNTIME_DIR` fallback (`hooks/murmly-announce.py:58-60`)

## 3. Linux stops being Fedora

- [ ] 3.1 Replace `_loaded_library_path`'s `/proc/self/maps` read (`src/murmly/tts.py:135`) with `dlinfo`, already in the lock as a transitive dependency, which answers the same question on every platform
- [ ] 3.2 Make `_malloc_trim` (`src/murmly/idle.py:22-44`) report that the platform's allocator cannot be asked to return memory, rather than silently doing nothing, and surface that in the residency diagnostics
- [ ] 3.3 Turn `SYSTEM_PYTHON` (`src/murmly/overlay.py:27`) into the interpreter the selected renderer needs, keeping `/usr/bin/python3` for the GTK4 renderer and its reason with it
- [ ] 3.4 Detect the package manager — `dnf`, `apt`, `pacman`, `zypper`, `apk` — and name the packages for it, printing the list plainly where none is recognised, which is what the script already does without `dnf`
- [ ] 3.5 Name `libportaudio2` or its equivalent among the Linux system packages: `sounddevice` bundles PortAudio on Windows and macOS but not on Linux
- [ ] 3.6 Stop reading `/proc/driver/nvidia/version` to detect a GPU; ask through a mechanism that answers on every platform
- [ ] 3.7 Keep the systemd unit's `After=pipewire.service wireplumber.service` ordering where systemd is the service manager, and stop treating it as something Murmly depends on elsewhere

## 4. GNOME hotkey registration

- [ ] 4.1 Add a GNOME hotkey backend writing to `org.gnome.settings-daemon.plugins.media-keys` `custom-keybindings`, with `name`, `command` and `binding` on the per-binding relocatable schema
- [ ] 4.2 Take the command runner and the `gsettings` binary name as constructor parameters, matching `PlasmaShortcuts(run_command=, busctl=)`, so it is testable without GNOME
- [ ] 4.3 Read back the binding to verify it took effect, and confirm it applies without a logout — it is a live dconf value
- [ ] 4.4 Determine whether another application already claims the key, and fail closed naming the owner where GNOME can report one
- [ ] 4.5 Confine writes to the entries Murmly created: append to `custom-keybindings` and remove exactly what was appended, never rewriting the list wholesale
- [ ] 4.6 Report a desktop Murmly cannot register on as this desktop's limitation rather than the platform's, and install everything else

## 5. Hotkey parsing across platforms

- [ ] 5.1 Split `hotkey.py` into one parse producing a platform-neutral hotkey, and one encoding per platform — Qt key codes stay as the KDE encoding rather than as the representation
- [ ] 5.2 Accept each platform's own modifier names, including `Command` and `Cmd`, and normalise them so one specification means the same physical key everywhere
- [ ] 5.3 Refuse a modifier the resolved platform does not have, naming it, rather than dropping or substituting it
- [ ] 5.4 Persist which keys are bound and what each is for, where the platform registers them in Murmly's own process — the desktop holds no record there, so the daemon has to re-register them at every session start and `doctor` has to read them from somewhere
- [ ] 5.5 Have `murmly install <hotkey>` reach a running daemon to rebind, since an in-process registration cannot be changed by writing a file the desktop reads; a daemon that is not running picks the new keys up from the record at 5.4 when it next starts

## 6. Diagnostics

- [ ] 6.1 Add a `platform` section naming the resolved platform and, for each platform-dependent concern, the mechanism selected or the reason none was
- [ ] 6.2 Distinguish a mechanism that does not exist on this platform from one that exists and could not be used — only the second is worth naming something to install for
- [ ] 6.3 Widen `session` beyond `wayland` and `x11` so a non-Linux session is not misreported as one of them (`src/murmly/cli.py:463`)
- [ ] 6.4 Report, for each permission the platform requires, whether it is granted, denied, or could not be determined, and never report a capability as available on the strength of the mechanism alone
- [ ] 6.5 Keep every existing field name and shape, so a report from one platform has the same keys as a report from another

## 7. Windows: the command channel

- [ ] 7.1 Add a named-pipe transport, since CPython exposes no `AF_UNIX` on Windows and the open implementation targets 3.16
- [ ] 7.2 Create the pipe with a security descriptor whose DACL grants only the creating user's SID — the OS default DACL grants `Everyone` read access, which is not the guarantee the requirement states
- [ ] 7.3 Add `pywin32` to `pyproject.toml` behind `sys_platform == 'win32'`, pinned
- [ ] 7.4 Read the peer's identity from the pipe's client process token, behind the existing `peer_identity_supported()` guard
- [ ] 7.5 Refuse at startup a configured channel name that cannot be created privately, naming the reason and the correction, and skip the filesystem path-privacy analysis, which does not apply to a name that is not in the filesystem
- [ ] 7.6 Keep the `daemon.socket_path` configuration key and its meaning: the channel Murmly serves on

## 8. Windows: desktop integration

- [ ] 8.1 Add a service backend driving Task Scheduler with a logon trigger — the only per-user autostart of the three with CLI verbs for start, stop, status, enable and disable, which `UserService.is_active()` and `status()` have to answer
- [ ] 8.2 Confirm it registers and starts without administrative rights
- [ ] 8.3 Add a hotkey backend calling `RegisterHotKey` on a message-loop thread inside the daemon, pumping `GetMessageW`
- [ ] 8.4 Treat the platform's own refusal to register a key another application holds as the collision, rather than querying first
- [ ] 8.5 Release the hotkey when the daemon stops, so it is not left claimed against another application
- [ ] 8.6 Have installation start the daemon before reporting a hotkey bound, and report the binding as held by the running daemon
- [ ] 8.7 Report a hotkey as not currently held, naming the daemon as why, when the daemon is not running

## 9. Windows: clipboard, injection, focus

- [ ] 9.1 Copy and read through the Win32 clipboard API with `CF_UNICODETEXT`, not `clip.exe`, which encodes through the console codepage and mangles anything outside it
- [ ] 9.2 Inject the paste with `SendInput`
- [ ] 9.3 Register `SendInput` as a method whose success cannot be observed, because UIPI silently discards synthetic input aimed at a higher-integrity window — so the transcript stays on the clipboard and the previous contents are not restored over it
- [ ] 9.4 Add a focus observer using `GetForegroundWindow`, `GetWindowThreadProcessId` and `QueryFullProcessImageName`, behind the existing `FocusObserver` protocol
- [ ] 9.5 Report the microphone privacy setting's state where it can be read, and distinguish a blocked microphone from an absent device — both present as no audio

## 10. Windows: overlay

- [ ] 10.1 Add a Qt renderer speaking the existing newline-delimited JSON protocol over the socketpair, so the daemon does not learn which renderer it started
- [ ] 10.2 Add PySide6 to `pyproject.toml` behind the platform markers for the platforms whose renderer needs it
- [ ] 10.3 Set `Qt.WindowTransparentForInput` and `Qt.WindowDoesNotAcceptFocus`, and apply `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` to the native `HWND` through `SetWindowLongPtr`
- [ ] 10.4 Reproduce every visual state, dimension and lifecycle transition the GTK4 renderer presents, from a single enumeration both read
- [ ] 10.5 Present nothing, and report which property could not be provided, where the platform cannot give a surface that is above ordinary windows, takes no focus, and intercepts no pointer input

## 11. Windows: runtime and packaging

- [ ] 11.1 Load the CUDA libraries with `os.add_dll_directory` over each `nvidia/*/bin` wheel directory before the runtime is imported — Windows has no `RTLD_GLOBAL`, and since Python 3.8 it does not search `PATH` for an extension module's DLL dependencies
- [ ] 11.2 Add `sys_platform != 'darwin'` to every `nvidia-*-cu12` requirement in the `cuda` extra: all six publish `win_amd64` and none publishes any macOS wheel, so resolving the extra on macOS fails outright
- [ ] 11.3 Spike whether the bundled `espeakng-loader` Windows wheel carries the same compiled-in data-path defect confirmed on Linux, and whether `ESPEAK_DATA_PATH` overrides it where `EspeakWrapper.set_data_path()` does not
- [ ] 11.4 If it does carry the defect, resolve a Windows espeak-ng install and name it in the remedy through the existing "runtime absent" reporting path; if it does not, use the bundled library
- [ ] 11.5 Check long-path support before syncing and name it, since `uv sync` otherwise fails with "the system cannot find the path specified" and never mentions path length
- [ ] 11.6 Check Developer Mode and warn that the model cache will be stored as full copies without it, doubling 1.6 GB on disk, because `huggingface_hub`'s cache uses symlinks
- [ ] 11.7 Confirm PortAudio is bundled in the `sounddevice` wheel and that the default host APIs — MME, DirectSound, WDM/KS, WASAPI — are enough, leaving ASIO to its `SD_ENABLE_ASIO` opt-in

## 12. macOS: microphone access, before anything else

- [ ] 12.1 Spike a daemon started by launchd and confirm whether audio actually arrives — a bare Python process has no bundle and no `NSMicrophoneUsageDescription`, and the reported failure is silent: no dialog, no exception, a stream delivering zeroes
- [ ] 12.2 Run the same spike from a terminal to confirm the difference, since a terminal-started process inherits the terminal application's own grant and would hide the problem
- [ ] 12.3 If launchd gets no audio, set `AssociatedBundleIdentifiers` in the plist so TCC attributes the agent to a bundle holding the grant, and verify
- [ ] 12.4 If that does not work either, ship a minimal `.app` wrapper carrying the usage-description string, and verify
- [ ] 12.5 Have diagnostics distinguish a denied microphone from an absent device, which are identical from inside the process
- [ ] 12.6 Do not claim macOS capture until one of these is proven working end to end

## 13. macOS: transport, service, hotkey

- [ ] 13.1 Keep the UNIX socket and its whole path-privacy analysis — macOS has `AF_UNIX` and the requirement is unchanged there
- [ ] 13.2 Read the peer's identity with `getpeereid(3)` through `ctypes.CDLL(None)`; it returns UID and GID and no PID, which is enough for the UID comparison the check already makes
- [ ] 13.3 Add a launchd service backend writing `~/Library/LaunchAgents/<label>.plist` with `Label`, `ProgramArguments`, `RunAtLoad` and `KeepAlive`
- [ ] 13.4 Drive it with `launchctl bootstrap gui/$UID`, `print`, `kickstart -k` and `bootout` — never `load`, which exits 0 and does nothing on a malformed plist and would make installation report success for a service that will never start
- [ ] 13.5 Add a hotkey backend calling Carbon `RegisterEventHotKey` through `ctypes` into HIToolbox, which needs no permission at all, unlike either mode of `CGEventTap`
- [ ] 13.6 Release the hotkey when the daemon stops, and report it as held by the running daemon
- [ ] 13.7 Report the mechanism's limitation in diagnostics: `RegisterEventHotKey` does not fire when the frontmost application consumes the combination itself, and cannot express a modifier-only chord

## 14. macOS: clipboard, injection, focus

- [ ] 14.1 Copy and read through `NSPasteboard`
- [ ] 14.2 Inject the paste with `CGEventPost` of Cmd+V
- [ ] 14.3 Register it as a method whose success cannot be observed: without an Accessibility grant the call does not fail, the event is dropped, and nothing arrives
- [ ] 14.4 Report the Accessibility grant with `AXIsProcessTrusted()`, which does not prompt
- [ ] 14.5 Request it with `AXIsProcessTrustedWithOptions(prompt: true)` only from `murmly install`, never from the daemon — a dialog raised by a background process the person did not just invoke is one they cannot connect to anything
- [ ] 14.6 Add a focus observer using `NSWorkspace.sharedWorkspace().frontmostApplication()`, which returns bundle identifier and PID as unprotected metadata
- [ ] 14.7 Do not use `CGWindowListCopyWindowInfo`: since macOS 10.15 it omits the window title without Screen Recording permission, and Murmly's target is the owning application, which needs no grant

## 15. macOS: overlay and runtime

- [ ] 15.1 Spike whether Qt's own `WindowTransparentForInput` and `WindowDoesNotAcceptFocus` give a non-activating, click-through, all-Spaces panel on macOS
- [ ] 15.2 If they do not, build the renderer directly on `NSPanel` through PyObjC — `NSWindowStyleMaskNonactivatingPanel`, `setLevel_`, `setIgnoresMouseEvents_`, and an all-Spaces `collectionBehavior` — rather than reflecting AppKit calls onto Qt's `NSWindow`, which is a community technique with no documented confirmation
- [ ] 15.3 Present nothing and report the reason where neither route gives the required properties
- [ ] 15.4 Confirm synthesis resolves the CoreML execution provider, which the stock `onnxruntime` macOS wheel carries, and that `[tts] device = "auto"` uses it while `[stt] device` correctly finds no accelerator — CTranslate2 has no GPU backend on macOS
- [ ] 15.5 Spike the bundled `espeakng-loader` macOS wheel for the same data-path defect, and name Homebrew's `espeak-ng` in the remedy if it carries it

## 16. The installer

- [ ] 16.1 Move what `setup.sh` exists to get right into `murmly` itself: reading which extras are installed before each sync so a sync does not remove a feature, and reapplying the ONNX Runtime GPU swap that every sync undoes
- [ ] 16.2 Reduce the per-platform entry points to a bootstrap — install `uv`, then hand off — as `sh` and as PowerShell
- [ ] 16.3 Refuse a machine with no build of the transcription runtime before syncing, naming the runtime and the characteristic, rather than letting the resolver name a package
- [ ] 16.4 State which permissions the platform will request and what each enables, before the first request is made
- [ ] 16.5 Keep every `setup.sh` subcommand and flag working: `install`, `upgrade`, `hooks`, `uninstall`, `--purge`, `--yes`, `--cuda`/`--no-cuda`, `--tts`/`--no-tts`
- [ ] 16.6 Keep declining every prompt, rather than assuming, when nothing is attached to the terminal and `--yes` was not given

## 17. Announcements without `os.fork`

- [ ] 17.1 Replace the `os.fork` + `os.setsid` detach in `hooks/murmly-announce.py:585-612` with a mechanism that exists on every platform, keeping the property the requirement states: the announcement does not hold up the turn
- [ ] 17.2 Resolve the chime playback command per platform, rather than trying `pw-play`, `paplay` and `aplay` alone
- [ ] 17.3 Confirm the hook still exits 0 for every reason it cannot speak, on every platform

## 18. Tests

- [ ] 18.1 Make `PlatformProfile` a value tests construct, and exercise every backend registry for every platform from any machine — the suite has no `sys.platform` check anywhere today because it never needed one
- [ ] 18.2 Test that the resolution answers for a supplied environment rather than the process's own
- [ ] 18.3 Test that an unsupported operating system is refused before any file is written
- [ ] 18.4 Test that a machine with no transcription runtime is refused at startup naming the runtime and the characteristic, and that a machine missing anything else starts with that capability reported unavailable
- [ ] 18.5 Test that the Linux configuration, data and runtime paths are byte-identical to what they are today, for every combination of `XDG_*` set and unset
- [ ] 18.6 Test the Windows pipe's security descriptor by reading back its DACL and asserting it names only the creating user's SID — a second account is not available in CI
- [ ] 18.7 Test that peer identity is read by the resolved platform's mechanism and that the same rule is applied to the result
- [ ] 18.8 Test the GNOME backend against a fake command runner: binding written, read back, conflict refused, and removal taking out exactly what was added
- [ ] 18.9 Test that one hotkey specification produces the same physical key on each platform's encoding, and that a modifier a platform does not have is refused by name
- [ ] 18.10 Test that a hotkey held in-process is reported as not held when the daemon is not running, and released when it stops
- [ ] 18.11 Test that `SendInput` and `CGEventPost` are treated as unconfirmable: the transcript stays on the clipboard and the previous contents are not restored over it
- [ ] 18.12 Test that an injection method whose permission is ungranted is reported as not permitted, distinctly from absent and from installed-but-unusable, and is not reported as available
- [ ] 18.13 Test that a permission whose state cannot be read is reported as undetermined rather than granted
- [ ] 18.14 Test that both overlay renderers handle the same protocol messages and present the same enumerated states, without a display
- [ ] 18.15 Test that the overlay is not presented, and the missing property named, where the platform cannot give a surface that takes no focus and intercepts no pointer input
- [ ] 18.16 Test that releasing a model reports system memory as not returned where the allocator cannot be asked, and still drops the model on schedule
- [ ] 18.17 Test that the diagnostics report carries the same field names on every platform, with an unserviceable concern reported unavailable rather than absent
- [ ] 18.18 Keep the runtime `self.skipTest(...)` pattern for anything needing a live session, extending it to skip on the wrong operating system, as `X11RuntimeIntegrationTests` already does for a missing display
- [ ] 18.19 Add Windows and macOS CI runners running everything that does not need a session

## 19. Documentation and the project page

- [ ] 19.1 Edit the `recording-overlay` capability's `## Purpose` line in `openspec/specs/recording-overlay/spec.md` directly — a delta's Purpose is ignored for an existing capability, so it does not change at archive time
- [ ] 19.2 Rewrite `manual/what-you-need.md` to describe three platforms rather than one distribution, keeping the KDE and Wayland detail as one platform's entry rather than the whole story
- [ ] 19.3 State the three machines that cannot run Murmly — musl Linux, Windows on ARM, Intel macOS — with the runtime and the reason for each, and state macOS support as Apple Silicon on macOS 14 or newer, in `manual/what-you-need.md`
- [ ] 19.4 Document each platform's permissions in `manual/what-you-need.md` and `manual/install.md`: the macOS Accessibility grant for pasting, the macOS microphone grant, and the Windows microphone privacy settings
- [ ] 19.5 Document in `manual/where-your-words-go.md` that pasting into an elevated window on Windows silently does nothing, in the same place the KDE input-consent dialog is documented, since it is the same class of failure
- [ ] 19.6 Give `manual/install.md` the per-platform install path, and `manual/changing-your-hotkey.md` the per-platform hotkey story, including the desktops where the binding is not automatic
- [ ] 19.7 Carry the same disclosures onto every page that shows an install command, since the requirement binds each such page and not only the landing page
- [ ] 19.8 Update `pyproject.toml`'s description, `README.md`'s opening and its requirements paragraph, and the landing page in `site/`: Murmly is no longer Fedora-first
- [ ] 19.9 Give `manual/troubleshooting.md` the per-platform failures: a silent macOS microphone, an ungranted Accessibility paste, a hotkey the frontmost macOS application consumes, and a Windows paste discarded by UIPI
- [ ] 19.10 Record a field note for anything that turns out to need an undocumented precondition on Windows or macOS

## 20. Verification

- [ ] 20.1 On Fedora + KDE + X11, confirm every existing behaviour is unchanged after phase 1: install, hotkey, capture, transcribe, paste, clipboard restore, overlay, speech output, and the full `doctor` report
- [ ] 20.2 On a GNOME Wayland session, install with a hotkey and confirm it binds, takes effect without a logout, survives a rebind, and is removed by uninstall
- [ ] 20.3 On a non-`dnf` distribution, confirm the installer names that distribution's packages and that the daemon starts, transcribes, and copies without any of them
- [ ] 20.4 On Windows, confirm the pipe refuses a connection from another account, the hotkey toggles capture, a transcript reaches the focused window, and a transcript aimed at an elevated window stays on the clipboard and is reported as copied but not pasted
- [ ] 20.5 On macOS, confirm a launchd-started daemon records — the phase 3 gate — then that the hotkey fires without any permission granted, that pasting fails silently until Accessibility is granted and is reported as ungranted until then, and that it works after
- [ ] 20.6 On each platform, stop the daemon and confirm the hotkey is released rather than left claimed
- [ ] 20.7 On each platform, run `doctor` and confirm every field is present, that the platform section names a mechanism or a reason for each concern, and that nothing is reported available whose permission is denied
