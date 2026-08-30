## Why

`README.md` is 966 lines and is the whole reference manual: the configuration table,
the speech-session protocol, three tables of measured memory and latency, the socket
permission rules, and every troubleshooting step. A person deciding whether to try
Murmly meets Wayland's refusal to hand out global hotkeys in the third paragraph, and
`## Speech output` alone is 298 lines — longer than the entire page that is meant to
sell the project.

The site at `https://osirison.github.io/murmly/` already introduces Murmly well and
then, at its last line, hands every question past "how do I install it" back to that
file. Most people who install Murmly are not going to read a Python project's README
to find out how to change a hotkey. The reference belongs on the site, written for
someone who does not know what a compositor is; the README belongs at ~60 lines,
saying what Murmly is, what it needs, how to install it, and what using it looks like.

## What Changes

- **The site grows from one page to a manual.** Thirteen documentation pages plus an
  index are added beneath the existing landing page at
  `https://osirison.github.io/murmly/manual/`. The landing page keeps the root
  address, its content, and its verified requirements.
- **The manual is authored as Markdown and built by a static site generator.**
  MkDocs with the Material theme, installed as a non-default `docs` dependency group
  pinned by the repository's existing `uv.lock`. No Node, no second lockfile,
  no `package.json`.
- **Pages are titled and ordered by what a reader wants to do**, not by Murmly's
  internal vocabulary: "What you need before you start", "Where your words go",
  "Finishing a recording by pausing" — not "Transcript delivery", "Auto-transcribe".
- **`README.md` is cut from 966 lines to roughly 62.** What stays: the pitch, one
  screenshot, what you need, the install command, the three-step loop, one paragraph
  on speech, a link list into the manual, and six lines of development. Every piece
  of reference material moves to the manual and is not duplicated back.
- **`.github/workflows/pages.yml` gains a build step.** It installs the pinned docs
  toolchain, runs `mkdocs build` under `strict: true`, copies `site/` into the
  artifact unchanged, and proves that copy with `diff -r` before uploading.
- **BREAKING for deep links.** Anchors people have bookmarked on GitHub —
  `README.md#configuration`, `#the-session-protocol`, `#troubleshooting` — stop
  resolving once those headings are gone. GitHub cannot redirect a fragment and
  neither can a Pages site. The manual's index carries a table mapping every old
  README heading to its new page.

The property that "the published bytes are the committed bytes" ends for the manual
and is replaced by four things: a toolchain pinned in `uv.lock`, a build any reviewer
can reproduce locally, `strict: true` failing the deploy on a broken internal link,
and CI assertions run against the built artifact rather than against its sources. For
the landing page the property survives and is now asserted by `diff` rather than
assumed.

### Not in scope

- Rewriting or restyling the landing page. Its content, its layout, and its ten
  already-verified requirements are untouched; one link changes.
- A search engine that works without JavaScript. Search is client-side and is an
  enhancement; every page stays complete and navigable with scripting off.
- Translations, versioned documentation, or a changelog on the site.
- Moving `docs/agent-notes/`. It stays internal and unpublished, and the generator's
  source directory is deliberately named so that it can never read it.
- Contributor documentation beyond the six lines the README keeps. The foreground
  daemon, `murmly spike`, and venv activation leave the README; where they land is a
  separate decision, not this change.

## Capabilities

### New Capabilities

None. The site's whole public surface is already one capability.

### Modified Capabilities

- `project-website`: every requirement changes. One — "The page routes to
  installation rather than replacing the documentation" — currently forbids exactly
  this change, and is rewritten so the single-copy rule points at the site instead of
  at the README. Nine are written about "the page" singular and must be rescoped:
  eight outward, to bind every page including anything the generator's theme emits,
  and one inward, pinning the above-the-fold requirement to the landing page so it
  does not become absurd. Five requirements are added, covering the manual's
  existence, its findability by a reader who does not know the vocabulary, README's
  new role, the exclusion of `docs/agent-notes/`, and the pinned build that now
  stands between the reviewed sources and the published bytes.

`openspec/specs/project-website/spec.md`'s `## Purpose` is written in the singular
("the public page that introduces Murmly"). No delta in this repository has ever
edited a Purpose block and there is no mechanism to do so, so it needs a hand edit to
the main spec at sync or archive time. Recorded here so it is not forgotten.

## Impact

| Area | Change |
| --- | --- |
| `README.md` | 966 lines to ~62. Also the package long description — `pyproject.toml` sets `readme = "README.md"`, so what PyPI and `pip show` display shrinks with it |
| `manual/` | New. The generator's source directory, Markdown only |
| `overrides/` | New. Two or three Jinja partials: the off-origin footer credit removed, Material's `alt="logo"` fixed, Open Graph tags added |
| `mkdocs.yml` | New, at the repository root |
| `pyproject.toml` | A non-default `docs` dependency group. `uv sync` for development and for the test matrix is unchanged |
| `uv.lock` | Grows by 23 pure-Python entries, 42 to about 65 |
| `.gitignore` | `_pages/`, the assembled artifact, never committed |
| `.github/workflows/pages.yml` | Build, assemble, and two CI guards before upload. Still independent of the test matrix |
| `.github/workflows/tests.yml` | A docs-build job so a broken link fails at review rather than at deploy |
| `site/index.html` | One line: the README link becomes a link to the manual |
| `site/style.css`, `site/assets/` | Untouched. Still the only copy of the mark, and the source of the manual's colour tokens |
| `docs/agent-notes/` | Untouched, and never read by the generator. Gains one note on building the manual |
| `licenses/` | New. Material's admonition and navigation icons are Pictogrammers Free License and must be carried |
| Runtime code, tests, specs other than `project-website` | Untouched. Nothing about how Murmly behaves changes |
