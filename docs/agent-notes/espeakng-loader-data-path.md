---
title: Point espeak-ng at the system install, not the bundled wheel
description: espeakng-loader ships a library whose data directory is compiled in as the CI machine that built it, and set_data_path does not override it
trigger: pip install kokoro-onnx, uv pip install kokoro-onnx, kokoro_onnx.Kokoro, phonemizer, EspeakWrapper

depends_on: pyproject.toml, uv.lock
recorded: 2026-08-20
---

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
