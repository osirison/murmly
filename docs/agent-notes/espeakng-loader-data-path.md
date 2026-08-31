---
title: Point espeak-ng at the system install, not the bundled wheel
description: espeakng-loader ships a library whose data directory is compiled in as the CI machine that built it -- but set_data_path (called before first use) does override it
trigger: pip install kokoro-onnx, uv pip install kokoro-onnx, kokoro_onnx.Kokoro, phonemizer, EspeakWrapper

depends_on: pyproject.toml, uv.lock
recorded: 2026-08-20
updated: 2026-08-31
---

## Correction (2026-08-31)

This note originally claimed `EspeakWrapper.set_data_path()` does not override
the compiled-in path. **That claim is false.** It does override it, provided
it runs before the first `EspeakWrapper` is constructed. Verified in a clean
`/home/qp/Cloud/Projects/murmly/.venv/bin/python3` process, against the exact
pinned versions (`phonemizer` 3.4.0, `espeakng-loader` 0.2.4, `kokoro-onnx`
0.6.1 -- unchanged since this note was first recorded):

```python
from kokoro_onnx.tokenizer import Tokenizer
tok = Tokenizer()   # no explicit EspeakConfig at all: pure bundled defaults
tok.phonemize("hello world")   # -> 'həlˈoʊ wˈɜːld', bundled library AND bundled data, no system espeak-ng
```

This is not a version fix arriving after the fact -- `git show 9425170:uv.lock`
shows the same three versions pinned the day this note was first written.
The claim was wrong from the start.

**The mechanism**, traced to source
(`phonemizer/backend/espeak/api.py:43-89`):

```python
class EspeakAPI:
    def __init__(self, library, data_path):
        if data_path is not None:
            data_path = str(data_path).encode('utf-8')
        ...
        self._library.espeak_Initialize(0x02, 0, data_path, 0)
```

`data_path` is forwarded straight into `espeak_Initialize`'s `path` argument.
The compiled-in directory this note is about is what the C library falls back
to only when that argument is a NULL pointer -- confirmed by reading the
`espeak-ng.dll`/`.so` string tables directly: both binaries carry
`getenv`, the literal string `ESPEAK_DATA_PATH`, the format string
`%s/espeak-ng-data`, and the compiled-in fallback (`D:/a/espeakng-loader/...`
on the `win_amd64` wheel, `/home/runner/work/espeakng-loader/...` on the
`manylinux` one) as three entries in the same small cluster of strings --
exactly the shape of `path = getenv("ESPEAK_DATA_PATH") ?: COMPILED_DEFAULT;
sprintf(buf, "%s/espeak-ng-data", path)`, run only when the API's own `path`
argument was NULL to begin with.

So there are, empirically, two independent ways to keep that argument
non-NULL, and both were tested clean:

- `EspeakWrapper.set_data_path(data_path)` before constructing the wrapper
  (`kokoro_onnx.Tokenizer.__init__` always does this, with either an explicit
  `EspeakConfig` or the bundled defaults from `espeakng_loader.get_data_path()`
  -- there is no code path through `Tokenizer` that leaves it unset).
- The `ESPEAK_DATA_PATH` environment variable, read by `getenv` inside the C
  library itself, ahead of Python entirely. Also verified: setting it before
  `import phonemizer` and never calling `set_data_path()` at all still
  succeeds.

What still reproduces the original symptom exactly (same stderr line, same
compiled-in path) is calling `ctypes.CDLL(lib_path)` and then constructing a
`phonemizer.backend.EspeakBackend` **without ever calling
`set_data_path()`** -- `_ESPEAK_DATA_PATH` stays `None`, encodes to a NULL
argument, and the library falls back. That is a real failure mode; it is just
not the one `kokoro_onnx.Tokenizer` -- or `resolve_espeak()` -- can produce,
since both always pass an explicit path.

**What is unaffected by this correction:** Linux still prefers the system
package below, and that preference is unchanged by this correction -- it is a
landed design decision (`design.md`, "espeak-ng: keep preferring the
platform's, and spike the bundled wheel") outside the scope of the section
that found this. What the correction does change is Windows: see
`src/murmly/tts.py`'s `_resolve_bundled_espeak`, added for task 11.4, which
uses the bundled wheel directly rather than hunting a system install Windows
has no package-manager route to.

## Symptom

Any phoneme-based TTS that goes through `phonemizer` + `espeakng-loader` fails
at first synthesis, naming a path that belongs to somebody else's build machine:

```text
Error processing file '/home/runner/work/espeakng-loader/espeakng-loader/
  espeak-ng/_dynamic/share/espeak-ng-data/phontab': No such file or directory
```

The process does not raise. It prints this to stderr and returns no audio, so a
caller that only checks for exceptions sees an empty result rather than a failure.

## Cause

`espeakng-loader` bundles `libespeak-ng.so.1.52.0` inside the wheel with its
data directory compiled in as the absolute path it was built at. The bundled
data files are present and `espeakng_loader.get_data_path()` returns a real
directory, so the usual existence check passes — but the library ignores
`EspeakWrapper.set_data_path()` and reads its compiled-in path anyway.

`kokoro_onnx.Tokenizer` calls `EspeakWrapper.set_data_path()` and
`set_library()`, which is why passing the bundled paths explicitly does not help.

## Fix

Install espeak-ng from the distribution and point at it:

```bash
sudo dnf install espeak-ng
```

```python
from kokoro_onnx import Kokoro, EspeakConfig

kokoro = Kokoro(
    "kokoro-v1.0.onnx",
    "voices-v1.0.bin",
    espeak_config=EspeakConfig(
        lib_path="/usr/lib64/libespeak-ng.so.1",
        data_path="/usr/share/espeak-ng-data",
    ),
)
```

Confirmed on Fedora 44 with `espeak-ng-1.52.0-3.fc44.x86_64`, which provides
`/usr/lib64/libespeak-ng.so.1 -> libespeak-ng.so.1.1.51`. Synthesis succeeds
with the system library where it fails with the bundled one.

Do not hard-code `/usr/lib64`. Resolve the library with `ctypes.util.find_library`
or the distribution's own path so this works outside Fedora.

## License note

`espeak-ng` and `phonemizer` are both GPL-3.0 and Murmly is Apache-2.0, so
`pip install kokoro-onnx` puts GPL-3.0 code in the process either way. This is
not a blocker for a source-distributed project: Apache-2.0 is one-way compatible
with GPL-3.0, the user assembles the combination at install time, and espeak-ng
is already a Fedora system package. It would need a decision only if Murmly ever
ships a bundled artifact (Flatpak, AppImage, PyInstaller).

Prefer the system `espeak-ng` because the bundled one is broken, not for
licensing reasons. Background in
`~/fedora/journal/murmly/2026-08-20-tts-model-verification.md`.
