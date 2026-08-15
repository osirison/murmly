---
title: Validate CUDA with inference before enabling GPU transcription
description: Installer requirements for Murmly CUDA runtime dependencies and smoke validation
trigger: uv add --optional cuda, uv sync --extra cuda, uv run murmly daemon

depends_on: pyproject.toml, uv.lock, src/murmly/stt.py
recorded: 2026-08-15
---

## Symptom

`nvidia-smi` reports a supported NVIDIA GPU and CTranslate2 reports CUDA compute
modes, but transcription fails only when segment generation begins:

```text
RuntimeError: Library libcublas.so.12 is not found or cannot be loaded
```

An interrupted `uv add --optional cuda` can also leave no `cuda` entry in
`pyproject.toml`. A later sync then fails with:

```text
Extra `cuda` is not defined in the `optional-dependencies` table for `murmly`
```

## Confirmed requirements

Declare and lock the CUDA 12 runtime separately from the default CPU install:

```toml
[project.optional-dependencies]
cuda = [
    "nvidia-cublas-cu12>=12,<13",
    "nvidia-cudnn-cu12>=9,<10",
]
```

Then install it explicitly:

```bash
uv lock
uv sync --extra cuda
```

The resolved wheels include cuBLAS, cuDNN, and NVRTC and require roughly
1.4 GB of downloads on Linux x86-64. The Whisper model download is additional.

## Installer gates

1. Offer CPU and NVIDIA CUDA setup paths instead of installing GPU packages for
   every user.
2. Check disk space and disclose download sizes before syncing the CUDA extra.
3. Verify that `pyproject.toml` defines the extra and `uv.lock` provides it
   before starting the large download.
4. Preserve the package-manager cache so an interrupted sync can resume.
5. Use `nvidia-smi` and CTranslate2 device enumeration only as preliminary
   diagnostics. A stale or incompatible driver can make CTranslate2's device
   probe raise `RuntimeError`; automatic mode should fall back to CPU.
6. Run a short transcription with a cached small model on CUDA `float16` before
   selecting GPU mode.
7. Keep CPU `int8` fallback when CUDA libraries are absent or fail to load.
   A missing NVIDIA distribution is an unavailable runtime. A present library
   with invalid provenance, permissions, or ABI must fail closed.

## Confirmed wheel layout and validation

On Fedora with Python 3.14, both `nvidia.cublas.lib` and `nvidia.cudnn.lib` are
namespace packages with `origin = None`. Do not load bare sonames or trust
namespace search locations because `LD_LIBRARY_PATH` and `PYTHONPATH` can alter
those results. Resolve exact library files from the locked distributions'
metadata, require canonical paths inside `sys.prefix`, and reject symlinked or
group/world-writable binaries.

After preloading `libcublasLt.so.12`, `libcublas.so.12`, and `libcudnn.so.9`
with `RTLD_GLOBAL`, a cached `base.en` model completed CUDA `float16`
transcription on an RTX 3080. The successful runtime installation resolved:

```text
nvidia-cublas-cu12==12.9.2.10
nvidia-cuda-nvrtc-cu12==12.9.86
nvidia-cudnn-cu12==9.24.0.43
```

Package preparation took 18 minutes 43 seconds on the tested connection. The
installer must not mark CUDA ready until the preload path and a real GPU
transcription both succeed in the final environment.

The full balanced path was also validated with `large-v3-turbo`, CUDA
`float16`, beam size 5, and VAD enabled. The model completed transcription of
the retained diagnostic WAV after its one-time Hugging Face download. Its
cache directory used 1.6 GB, bringing the tested CUDA runtime and balanced
model footprint to roughly 3 GB. The balanced model is pinned to Hugging Face
revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`.
