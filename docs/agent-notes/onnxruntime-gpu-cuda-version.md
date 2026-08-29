---
title: Pin onnxruntime-gpu to 1.24.4 so ONNX and CTranslate2 share one CUDA stack
description: onnxruntime-gpu 1.25+ moved to CUDA 13, which conflicts with the cu12 wheels Murmly's cuda extra installs for faster-whisper
trigger: uv pip install onnxruntime-gpu, uv add onnxruntime-gpu, uv sync --extra cuda, pip install kokoro-onnx, uv sync --no-group tts, uv run --extra cuda, uv pip uninstall onnxruntime

depends_on: pyproject.toml, uv.lock, docs/agent-notes/uv-sync-cuda-runtime.md
recorded: 2026-08-20
updated: 2026-08-30
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


## `uv run --extra` undoes the swap, silently

The GPU build is a swap, not an addition (`pyproject.toml` says so), which means
nothing in the project metadata records that it happened. `kokoro-onnx` installs
by default now — it is a `[dependency-groups]` group listed in
`[tool.uv] default-groups`, not something a sync has to be told to fetch — and it
needs the CPU `onnxruntime`, the same as `faster-whisper` does. Either dependency
pulls `onnxruntime` back in and installs it *alongside* `onnxruntime-gpu`, whose
libraries it then shadows:

```console
$ uv run --extra cuda python bench.py
Installed 1 package in 35ms
Speech output falling back to the CPU: ...
provider: CPUExecutionProvider
```

There is no error. The run completes and reports numbers for a CPU session.

**Run one-off scripts and benchmarks with `uv run --no-sync`.** A bare `uv run`
syncs the environment before it runs anything, so it repairs the "missing" CPU
package every time. `--no-sync` skips that step and leaves the swap in place; it
is what `.github/workflows/tests.yml` and `setup.sh` already use. Passing
`--no-group tts` is not a substitute — the CPU `onnxruntime` arrives as a
dependency of `faster-whisper`, which is a base dependency, so every sync still
reinstalls it.

`.venv/bin/python` also works and skips `uv` entirely, but it resolves only from
the repository root. From a worktree under `.worktrees/` it fails outright, and
`uv run --no-sync` there creates an empty `.venv` in the worktree before failing
with `ModuleNotFoundError` — after which `.venv/bin/python` appears to exist. A
worktree needs its own synced environment, with the swap reapplied, before any
measurement taken in it means anything.

A long-lived daemon started before the swap was undone keeps its original
mappings and goes on using the GPU, so `nvidia-smi` and the daemon log will
disagree with a fresh interpreter — check
`uv run --no-sync python -c "import onnxruntime as ort; print(ort.get_available_providers())"`,
not the running process.

## Repairing it needs `--reinstall`

The obvious repair leaves the environment worse than it found it:

```console
$ uv pip uninstall onnxruntime            # also deletes files onnxruntime-gpu owns
$ uv pip install "onnxruntime-gpu==1.24.4"
Checked 1 package in 2ms                  # metadata is intact, so nothing is restored
```

Both distributions install into the same `onnxruntime/` package directory. The
uninstall removes the shared files, the plain install sees `onnxruntime-gpu`
already recorded as present and does nothing, and the result raises
`the ONNX Runtime is missing or unusable` from `resolve_providers`. Force it:

```bash
uv pip install --reinstall "onnxruntime-gpu==1.24.4"
```

Then confirm against a session rather than the module list, per the Symptom
above:

```console
$ .venv/bin/python -c "import onnxruntime as ort; print(ort.get_available_providers())"
['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']
```

The reinstall also upgrades whatever shares the dependency closure - it moved
`protobuf` 7.35.1 -> 7.36.0 here. Run the suite afterwards.

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
addition, and the swap is clean **from a fresh environment**:

```bash
uv pip uninstall onnxruntime
uv pip install "onnxruntime-gpu==1.24.4"
```

Confirmed in a fresh environment on 2026-08-21: after the swap
`onnxruntime.InferenceSession` exists, `get_available_providers()` lists CUDA,
and `faster_whisper.vad.get_vad_model()` still loads through the survivor.

It is **not** clean when `onnxruntime-gpu` is already installed and the CPU build
has been reintroduced on top of it — the case you are in whenever `uv run` or
`uv sync` has touched the environment since the swap. The install then finds its
own metadata intact and restores nothing the uninstall deleted. Use
`--reinstall`; see "Repairing it needs `--reinstall`" above.

## `uv sync --extra cuda` used to remove speech output — history

`uv sync` matches the environment exactly to the extras it is given, and until
2026-08-30 `tts` was one of those extras. `uv sync --extra cuda` on its own did
not add the CUDA extra to what was installed; it made the environment match
exactly what it was given, and removed everything else not named — including
the speech synthesizer:

```console
$ uv sync --extra cuda --dry-run          # old behaviour, in an environment that has [tts]
Would uninstall 2 packages
 - kokoro-onnx==0.6.1
 + onnxruntime==1.28.0
```

Confirmed on 2026-08-21, back when the GPU recipe had to start
`uv sync --extra cuda --extra tts`; starting it with `--extra cuda` alone
removed `kokoro-onnx` and left speech output unavailable for a reason that
looked nothing like the command that caused it. That was the incident: someone
typed exactly the command above to add CUDA support and lost speech output as a
side effect, with nothing in the output naming what was lost.

**Fixed on 2026-08-30** by moving `tts` out of
`[project.optional-dependencies]` into `[dependency-groups]`, listed in
`[tool.uv] default-groups`. Extras and dependency groups are matched
independently, so naming `cuda` no longer touches `tts` at all. Confirmed with
uv 0.12.3:

```console
$ uv sync                    # installs kokoro-onnx: tts is a default group
$ uv sync --extra cuda       # installs the seven nvidia cu12 wheels, uninstalls nothing
$ uv sync --no-group tts     # uninstalls 6 packages, including kokoro-onnx
$ uv sync --locked           # what CI runs; installs kokoro-onnx
```

`uv sync --extra cuda` is the command that caused the incident above, and it is
now harmless — it neither installs nor removes `kokoro-onnx`. `--no-group tts`
is the only way left to remove speech output, and typing it is deliberate: a
real "I don't want this" rather than a side effect of asking for something
else.

A `[tool.uv] default-extras` setting would have kept `tts` as an extra while
making it install by default, but it does not exist in uv 0.12.3 — uv rejects it
as an unknown field. A dependency group was the only mechanism available.

`uv sync --extra cuda` is still exact with respect to *extras*: there is only
one now (`cuda`), so the "name every extra every time" hazard is gone for speech
output, but naming extras is still exact for whatever extras exist.

## Synthesis is not gated on `[stt] device`

`resolve_providers` reads `config.device`, which is the `[stt] device` key —
there is no separate one for synthesis. It is read as a preference: a person who
pinned transcription to the GPU has not asked for GPU synthesis, so a missing
`onnxruntime-gpu` falls back to the CPU and logs the remedy rather than raising.
Raising made `device = "cuda"` refuse every speech session on the documented
`--extra cuda` install, which carries the CPU runtime regardless — `kokoro-onnx`
installs by default now, and `faster-whisper` always did.

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
