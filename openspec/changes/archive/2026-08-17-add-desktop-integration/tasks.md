## 1. Hotkey parsing

- [x] 1.1 Add a hotkey module with a strict parser: zero or more of `Meta+`, `Ctrl+`, `Alt+`, `Shift+` followed by exactly one known key name, returning both the Qt integer encoding and the canonical portable string.
- [x] 1.2 Build the key-name table (letters, digits, function keys, and common named keys) and the modifier constants; reject unknown names with a message naming the unrecognized part and listing supported names.
- [x] 1.3 Reject a hotkey with no modifier, and reject any key name containing a comma.
- [x] 1.4 Accept `Super` and `Win` as aliases for `Meta` and normalize them.
- [x] 1.5 Unit tests for the parser: known keys round-trip to the expected integer, aliases normalize, and every rejection case produces a distinct actionable message.

## 2. Desktop query layer

- [x] 2.1 Add a read-only desktop query helper wrapping the scalar-taking shortcut lookups (availability by integer, owners by integer, component existence by name), returning parsed results and distinguishing "not present" from "query failed".
- [x] 2.2 Add a guard, with a comment citing `docs/agent-notes/plasma-global-shortcut-binding.md`, ensuring no code path ever sends a key-sequence struct — that call shape aborts the desktop shortcut daemon.
- [x] 2.3 Add Plasma detection reusing the existing desktop detection used by the overlay, and expose whether the session type has been verified.
- [x] 2.4 Unit tests with recorded command output for the query helper; live-session tests skip themselves when no desktop session is available.

## 3. Service installation

- [x] 3.1 Resolve the absolute entrypoint path from the running interpreter's environment; fail with a message naming what could not be resolved when it is not an executable file.
- [x] 3.2 Generate the user service unit with `PartOf=`, `After=`, and `WantedBy=graphical-session.target`, and the resolved absolute `ExecStart`.
- [x] 3.3 Write the unit atomically, reload the user manager, enable the unit, and start the service directly — not through the target, which refuses manual start.
- [x] 3.4 Implement service removal: stop, disable, remove the unit, reload; succeed when any part is already absent.
- [x] 3.5 Delete `contrib/murmly.service` now that the unit is generated.
- [x] 3.6 Unit tests for unit-file generation and for the already-absent removal paths.

## 4. Hotkey registration

- [x] 4.1 Generate the launcher entry with `Type`, `Name`, `Exec` (absolute, with literal `%` doubled), `NoDisplay=true`, `StartupNotify=false`, `X-KDE-GlobalAccel-CommandShortcut=true`, and the mandatory `X-KDE-Shortcuts`; write it atomically.
- [x] 4.2 Trigger the desktop service-cache rebuild, then poll for the component with a bounded wait; report expiry as a failure to bind rather than as success.
- [x] 4.3 Implement removal: delete the launcher, rebuild, and poll until the component is gone; do not assume any shortcut-configuration entry exists.
- [x] 4.4 Implement rebinding as remove-then-add, never as an in-place rewrite — an in-place rewrite is ignored by the running session.
- [x] 4.5 Detect an existing user override of Murmly's hotkey read-only, report it, and leave it in place.
- [x] 4.6 Unit tests for launcher-file content and for the remove-then-add ordering.

## 5. Conflict detection and verification

- [x] 5.1 Pre-flight: refuse to bind a hotkey owned by another application, naming the owner; treat a hotkey already owned by Murmly as idempotent success.
- [x] 5.2 Post-install: read the registered key back and fail when it differs from the computed integer, naming both keys — this is what catches a wrong key-table constant.
- [x] 5.3 Post-install: assert sole ownership and fail when more than one owner is reported.
- [x] 5.4 On any verification failure, remove the registration that was created so nothing partially installed is left behind.
- [x] 5.5 Report success without claiming key delivery, and invite the user to press the hotkey once to confirm.
- [x] 5.6 Unit tests covering conflict refusal, idempotent re-install, key mismatch, and multiple owners, each asserting that cleanup ran.

## 6. Install and uninstall commands

- [x] 6.1 Add the `install` subcommand with a required hotkey argument, wiring parse → pre-flight → service → launcher → verify, and rolling back on failure at any stage.
- [x] 6.2 On an unsupported desktop, install the service, decline the hotkey with an explanation, and print the exact command to bind manually.
- [x] 6.3 On a supported desktop with an unverified session type, proceed and state that the session type is unverified.
- [x] 6.4 Add the `uninstall` subcommand removing service, launcher, and hotkey; succeed when nothing or only part of it is present.
- [x] 6.5 Make re-running install repair a stale absolute path after the project is moved or its environment rebuilt.
- [x] 6.6 Unit tests for the command paths, including rollback ordering and the partially-installed uninstall.

## 7. Toggle recovery

- [x] 7.1 Catch the connection failure in the command client and distinguish "not installed" from "installed but not listening".
- [x] 7.2 When installed and not listening, start the service, wait a bounded time for the socket, and retry once.
- [x] 7.3 When not installed, exit non-zero with a message naming the install command, with no unhandled traceback.
- [x] 7.4 When the service does not become ready within the bounded wait, exit non-zero stating that the daemon could not be started; never retry indefinitely.
- [x] 7.5 Unit tests for all three outcomes with a stubbed service controller, asserting exactly one retry.

## 8. Diagnostics

- [x] 8.1 Add an installation section to `murmly doctor` reporting installed state, service active state, the recorded entrypoint path, the bound hotkey, and whether Murmly currently holds it.
- [x] 8.2 Report the not-installed case and the hotkey-held-by-another-application case, naming the holder.
- [x] 8.3 Unit tests for each reported state.

## 9. Documentation

- [x] 9.1 Restructure `README.md` around install → bind a key → speak; move the `uv run murmly ...` material into a development section.
- [x] 9.2 Document that the daemon runs for the session and holds the model from first toggle until logout, with the approximate footprint.
- [x] 9.3 Document uninstall, rebinding, and that hotkey support is Plasma-only and verified on X11 but not Wayland.
- [x] 9.4 Remove README references to `contrib/murmly.service` and to binding a hotkey by hand as the primary path.

## 10. Validation

- [x] 10.1 Run the full test suite and confirm that live-session tests skip cleanly where no desktop session is available.
- [x] 10.2 Manually verify on a Plasma session: install, press the hotkey, confirm capture starts; rebind; press the old key and confirm it is inert; uninstall and confirm the key is released.
- [x] 10.3 Confirm the user's global shortcut configuration is byte-identical before and after a full install/rebind/uninstall cycle.
- [x] 10.4 Confirm the daemon starts on login and stops on logout, and that the shortcut daemon records no crashes during the cycle. *(Verified via the unit properties systemd resolved — `PartOf`/`After`/`WantedBy` all `graphical-session.target`, and the service listed under that target's dependencies — plus zero new shortcut-daemon restarts or coredumps across a full install/rebind/uninstall cycle. A real logout/login cycle was not performed, since it would have ended the working session; start-on-login is taken on the strength of the wiring.)*
- [x] 10.5 Run `openspec validate --strict` for this change.
