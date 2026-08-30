---
title: Preview the manual without touching .venv
description: Build and serve the documentation site without reinstalling the CPU onnxruntime over a GPU swap
trigger: mkdocs serve, mkdocs build, uv sync --only-group docs
depends_on: mkdocs.yml, manual/, overrides/, .github/workflows/pages.yml
recorded: 2026-08-30
---

## Preview the manual with this, and only this

```bash
uvx --from "mkdocs==1.6.1" --with "mkdocs-material==9.7.7" mkdocs serve
```

`uvx` builds a throwaway environment in the uv cache and does not read or write
`.venv`. Swap `serve` for `build -d /tmp/manual` to inspect the output instead.

Keep both pins matching `uv.lock`. Without them you preview a version of the
theme that CI does not build with.

## Two commands that look right and are not

**`uv run mkdocs serve`** — `uv run` syncs before it runs anything. The CPU build
of `onnxruntime` arrives as a dependency of `faster-whisper`, so on a machine
that has had the GPU swap applied the sync reinstalls the CPU build over it. The
suite still passes and every synthesis measurement afterwards silently reports a
CPU session. This is exactly the failure `onnxruntime-gpu-cuda-version.md`
describes.

**`uv sync --only-group docs`** in the project — worse. `uv` prunes to match what
it was asked for, so it strips `faster-whisper`, `onnxruntime` and the
synthesizer out of `.venv` and leaves you with a documentation toolchain where
the project used to be. Recover with `uv sync --locked` and then reapply the GPU
swap.

`uv run --no-sync mkdocs build` is what CI runs, and it is correct **there**
because that runner has nothing but the docs group installed. On a developer
machine it fails, because the `docs` group is not a default and `mkdocs` is not
in `.venv`.

## `_pages/` is assembled, never committed

`mkdocs build` writes only `_pages/manual/`. The Pages workflow then copies
`site/` over the top of it, drops in the mark and the favicon, and asserts with
`diff -r site _pages --exclude=manual --exclude=404.html` that the copy changed
nothing. `_pages/` is in `.gitignore`; a commit of it is a mistake.

## Five keys that must never enter `mkdocs.yml`

Each one silently breaks a requirement in
`openspec/specs/project-website/spec.md`. The reasons are in a comment block in
`mkdocs.yml`, and CI asserts the list against a comment-stripped copy of the
file, so adding one fails the deploy rather than publishing quietly.

| Key | What it does |
| --- | --- |
| `repo_url` | Fetches `api.github.com` on every page view, and publishes a star count |
| `theme.palette` with a `toggle:` | Writes `__palette` to `localStorage` |
| `features: content.tabs.link` | Writes `__tabs` |
| `features: announce.dismiss` | Writes `__announce` |
| `extra.version` | Writes `__outdated` to `sessionStorage` |

Dark mode is a `prefers-color-scheme` block in `manual/stylesheets/murmly.css`.
With no `palette:` key Material never writes `data-md-color-scheme` onto
`<html>`, so its palette stylesheet is emitted but never linked, and the CSS is
the whole mechanism. Do not "fix" this by adding a toggle.

## The landing page can no longer be reviewed over `file://`

`use_directory_urls: true` emits `href=".."` and `href="./"`, which do not
resolve as documents over `file://`, and `site/index.html`'s link to `manual/`
is dead there too. Review both halves together instead:

```bash
uvx --from "mkdocs==1.6.1" --with "mkdocs-material==9.7.7" mkdocs build
cp -r site/. _pages/
cp site/assets/murmly-mark.svg site/assets/favicon.ico _pages/manual/assets/
python3 -m http.server -d _pages 8000
```

## `http.server` serves at the host root; the published site does not

The published site lives under the path prefix `/murmly/`. A local
`python3 -m http.server` serves `_pages/` at `/`, which exercises every relative
reference but none of the absolute ones.

`404.html` is the file that matters here: it is served by GitHub Pages from the
host root for any unmatched path, so its references have to be absolute, and
locally they resolve to the wrong place. They are only verifiable against the
published prefix. Do what `github-pages-source-setting.md` already says: open
`https://osirison.github.io/murmly/` and a made-up path beneath it, and read the
network log. A green badge on the workflow is not evidence that anything
resolved.
