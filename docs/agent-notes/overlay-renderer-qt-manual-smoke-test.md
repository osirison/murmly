---
trigger: overlay_renderer_qt.py, PySide6, testing the Windows overlay renderer
depends_on: src/murmly/overlay_renderer_qt.py, src/murmly/overlay.py, pyproject.toml
recorded: 2026-08-31
---

# Live-testing overlay_renderer_qt.py without a Windows machine

**Symptom:** `overlay_renderer_qt.py` cannot be exercised under `uv run`/the
project's own `.venv` -- PySide6 is deliberately not installed there. It is an
optional `overlay` extra (`sys_platform == 'win32'`) in `pyproject.toml`, so a
plain `uv sync` never resolves it, and assuming it needs installing first
costs a `uv lock`/network round trip for nothing.

**Fix:** Check the *system* interpreter first: on at least one Fedora dev
machine, `/usr/bin/python3` already has PySide6 installed as a distribution
package (`/usr/bin/python3 -c "from PySide6 import __version__; print(__version__)"`).
If it does, drive the renderer directly under it with an offscreen Qt
platform, no display and no real Windows machine required:

```
QT_QPA_PLATFORM=offscreen /usr/bin/python3 src/murmly/overlay_renderer_qt.py \
    --fd <fd> --backend windows [--transcript-panel] [--reduced-motion]
```

or, for finer control (driving specific protocol messages, monkeypatching
`apply_and_verify_exstyle` since the real Win32 call is not present on
Linux), import the module directly and construct `OverlayApplication` with a
real `socket.socketpair()` end, calling `_application.processEvents()`/
`repaint()` between sent messages.

**Why it was not obvious:** The module's own dual-mode import bootstrap and
lazy PySide6 imports make it look like nothing can run it outside the
project's `.venv` (which lacks PySide6) or a real Windows box. The system
interpreter is a third option this project's own tooling does not mention,
because the GTK4 renderer already depends on that same system interpreter
for an unrelated reason (PyGObject).

**What this does and does not prove:** Confirms the shared state machine
(`overlay_shared.RendererViewState`), the `QSocketNotifier` message loop, and
every `QPainter` drawing path run without raising. It proves nothing about
the real `SetWindowLongPtr`/`GetWindowLongPtr` behaviour (`ctypes.windll`
does not exist on Linux -- `apply_and_verify_exstyle` has to be monkeypatched
or is exercised via its own pure-function unit tests instead) or about the
`socket.share()`/`socket.fromshare()` Windows-only handoff in
`OverlayController._spawn_windows_renderer`. Those still need a real Windows
machine.
