---
title: Pin onnxruntime-gpu to 1.24.4 so ONNX and CTranslate2 share one CUDA stack
description: onnxruntime-gpu 1.25+ moved to CUDA 13, which conflicts with the cu12 wheels Murmly's cuda extra installs for faster-whisper
trigger: uv pip install onnxruntime-gpu, uv add onnxruntime-gpu, uv sync --extra cuda, pip install kokoro-onnx

depends_on: pyproject.toml, uv.lock, docs/agent-notes/uv-sync-cuda-runtime.md
recorded: 2026-08-20
---

## Symptom

`onnxruntime-gpu` installs cleanly, `ort.get_available_providers()` lists
`CUDAExecutionProvider`, and the session still silently runs on CPU:

```text
[E:onnxruntime:Default, provider_bridge_ort.cc:2395 TryGetProviderInfo_CUDA]
  Failed to load library libonnxruntime_providers_cuda.so with error:
  libcublasLt.so.13: cannot open shared object file: No such file or directory
[W:onnxruntime:Default, onnxruntime_pybind_state.cc:1139]
  Failed to create CUDAExecutionProvider. Require cuDNN 9.* and CUDA 13.*
```

The failure is a warning, not an exception. `InferenceSession.get_providers()`
then returns `['CPUExecutionProvider']` while `get_available_providers()` still
advertises CUDA, so a benchmark that checks only the latter reports a GPU run
that never happened. Check `session.get_providers()`, not the module-level list.

## Cause

`onnxruntime-gpu` 1.29.0 links against CUDA 13 (`libcublasLt.so.13`). Murmly's
CUDA extra pins the CUDA 12 line for CTranslate2:

```toml
cuda = [
    "nvidia-cublas-cu12>=12,<13",
    "nvidia-cudnn-cu12>=9,<10",
]
```

The CUDA 13 wheels are not usable from PyPI yet — `nvidia-cublas-cu13` and
`nvidia-cuda-runtime-cu13` are 0.0.1 placeholders, and `nvidia-cublas` /
`nvidia-cuda-runtime` without a suffix are NVIDIA-index stubs whose install
fails with a build error pointing at `nvidia-pyindex`. Only
`nvidia-cudnn-cu13` (9.24.0.43) is real.

## Fix

Use `onnxruntime-gpu==1.24.4`. It runs against the cu12 wheels the `cuda` extra
already installs, so Whisper and any ONNX model share a single CUDA runtime
instead of requiring two:

```bash
uv pip install "onnxruntime-gpu==1.24.4"
```

Confirmed working on an RTX 3080 Laptop, driver 610.57.04, with
`nvidia-cublas-cu12==12.9.2.10` and `nvidia-cudnn-cu12==9.24.0.43` already
present. `InferenceSession(..., providers=['CUDAExecutionProvider'])` then
reports `['CUDAExecutionProvider', 'CPUExecutionProvider']`.

The CUDA libraries must be on `LD_LIBRARY_PATH`; the wheels install them under
`<site-packages>/nvidia/<component>/lib`. Murmly's own loader resolves these
through distribution metadata instead — see `src/murmly/stt.py`
`_load_cuda_runtime` and `docs/agent-notes/uv-sync-cuda-runtime.md`, which apply
the same provenance checks any ONNX path should reuse rather than duplicate.

## The CUDA extra is not enough on its own

Pinning the version fixes the CUDA *major* line. It does not give the provider
every library it links, and the two runtimes do not link the same set.
`libonnxruntime_providers_cuda.so` needs six:

```console
$ ldd .../onnxruntime/capi/libonnxruntime_providers_cuda.so | grep 'not found'
	libcublasLt.so.12 => not found
	libcublas.so.12 => not found
	libcurand.so.10 => not found
	libcufft.so.11 => not found
	libcudart.so.12 => not found
	libcudnn.so.9 => not found
```

Murmly's `cuda` extra shipped only the first two and the last. The provider then
fails to load and ONNX Runtime reports it as a **warning** before running on the
CPU, which is the same silent fallback this note already warns about, reached a
different way. The extra now also carries `nvidia-cuda-runtime-cu12`,
`nvidia-cufft-cu12`, `nvidia-curand-cu12` and `nvidia-nvjitlink-cu12`, and
`src/murmly/tts.py` preloads all seven through `load_cuda_libraries` with the
provenance checks in `src/murmly/stt.py`.

Do not request `TensorrtExecutionProvider`. It heads the runtime's default
provider list, fails on a missing `libnvinfer.so.10`, and prints a page of
errors before falling back — build the session with
`providers=["CUDAExecutionProvider", "CPUExecutionProvider"]` explicitly.

## Do not install both distributions

`kokoro-onnx` and `faster-whisper` both depend on the CPU `onnxruntime`, so
listing `onnxruntime-gpu` as a dependency alongside them resolves **both** into
one environment:

```console
$ uv pip install --dry-run "murmly[cuda,tts] @ ."
 + onnxruntime==1.29.0
 + onnxruntime-gpu==1.24.4
```

That is the broken combination described below. The GPU build is a swap, not an
addition, and the swap is clean in that direction:

```bash
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu==1.24.4"
```

Confirmed in a fresh environment on 2026-08-21: after the swap
`onnxruntime.InferenceSession` exists, `get_available_providers()` lists CUDA,
and `faster_whisper.vad.get_vad_model()` still loads through the survivor.

## `uv sync` is exact, so name every extra every time

`uv sync --extra cuda` does not add the CUDA extra to what is installed. It
makes the environment match exactly what it was given, and removes everything
else — including the speech synthesizer:

```console
$ uv sync --extra cuda --dry-run          # in an environment that has [tts]
Would uninstall 2 packages
 - kokoro-onnx==0.6.1
 + onnxruntime==1.28.0
```

Confirmed on 2026-08-21. The GPU recipe therefore has to start
`uv sync --extra cuda --extra tts`; starting it with `--extra cuda` alone
removes `kokoro-onnx` and leaves speech output unavailable for a reason that
looks nothing like the command that caused it.

## Synthesis is not gated on `[stt] device`

`resolve_providers` reads `config.device`, which is the `[stt] device` key —
there is no separate one for synthesis. It is read as a preference: a person who
pinned transcription to the GPU has not asked for GPU synthesis, so a missing
`onnxruntime-gpu` falls back to the CPU and logs the remedy rather than raising.
Raising made `device = "cuda"` refuse every speech session on the documented
`--extra cuda --extra tts` install, which deliberately carries the CPU runtime.

## Also

`onnxruntime` and `onnxruntime-gpu` install into the same `onnxruntime` package
namespace. Installing both and then uninstalling one leaves the survivor broken:

```text
AttributeError: module 'onnxruntime' has no attribute 'InferenceSession'
```

Recreate the environment rather than trying to repair it.

Re-check this note when a CUDA 13 line becomes real on PyPI. At that point the
right move may be to move the whole project to cu13 rather than to hold
`onnxruntime-gpu` back.
