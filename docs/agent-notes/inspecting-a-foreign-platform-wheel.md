---
title: Inspecting a wheel built for a platform this machine cannot run
description: This venv has no pip and uv has no download subcommand -- fetch the wheel straight from PyPI's JSON API and files.pythonhosted.org instead
trigger: pip download, uv pip download, inspect wheel, macosx wheel, win_amd64 wheel, strings a dylib, strings a dll

depends_on: uv.lock
recorded: 2026-08-31
---

## Symptom

`python3 -m pip download ...` fails: `No module named pip` (this project's
venv has none installed). `uv pip download ...` fails: `error: unrecognized
subcommand 'download'` (uv 0.12.3 has no such subcommand; `uv pip install`
only installs into an environment matching the current machine, so it cannot
fetch a `macosx_*` wheel from Linux or a `win_amd64` one from either).

## Fix

Query PyPI's JSON API directly for the exact pinned version, find the wheel
filename for the target platform tag, then `curl` it straight from
`files.pythonhosted.org`:

```bash
curl -s https://pypi.org/pypi/<package>/json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for f in d['releases']['<exact-pinned-version>']:
    if 'macosx_11_0_arm64' in f['filename']:   # or win_amd64, etc.
        print(f['filename'], f['url'])
"
curl -sL -o out.whl '<url from above>'
```

A wheel is a zip file regardless of which platform it targets:

```bash
mkdir unpacked && cd unpacked && unzip -o ../out.whl
file some_library.dylib          # confirms arch/format without running it
strings some_library.dylib | grep -iE 'some-string-of-interest'
```

This is how the macOS `espeakng-loader` wheel was inspected for the
compiled-in data-path defect (task 15.5,
`docs/agent-notes/espeakng-loader-data-path.md`) without ever running on
macOS -- and the same technique an earlier session used for the Windows
wheel, per that same note.

## Why it was not obvious

`pip download` is the tool everyone reaches for first, and it is genuinely
absent here rather than misremembered — this venv was built by `uv`, which
manages installs into itself, not a general-purpose package fetcher.
