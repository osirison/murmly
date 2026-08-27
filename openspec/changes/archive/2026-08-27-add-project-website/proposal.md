## Why

Murmly's only front door is `README.md` — around 600 lines that open with Wayland's
refusal to hand out global hotkeys and reach the first screenshot never, because
there is no screenshot. Someone who would love this tool has to read a page of
compositor politics before learning that you press a key, speak, press it again,
and the words appear. There is no URL to send them to, no picture of the overlay,
and no page that says in one screen what Murmly is and why it is worth installing.

The repository is public on GitHub with Pages available at no cost, and the
project has enough measured, defensible claims — fully local, a paste path that
cannot silently lose a transcript, synthesis at roughly five times real time, a
memory table from real hardware — to make a page that persuades without inventing
anything.

## What Changes

- A single-page website is added under `site/` and published to GitHub Pages at
  `https://osirison.github.io/murmly/`.
- The page is hand-authored HTML, CSS, and SVG with **no build step and no Node
  toolchain**. Murmly is a Python project; a static-site generator for one page
  would add a dependency tree larger than the page it produces. The deploy job
  uploads `site/` as-is.
- A GitHub Actions workflow (`.github/workflows/pages.yml`) publishes on push to
  `main` and on manual dispatch, using `actions/upload-pages-artifact` and
  `actions/deploy-pages`. It never runs on pull requests from forks, which cannot
  hold the `pages: write` permission.
- An original SVG logo and wordmark are authored in the repository — no stock
  asset, no third-party font file. The mark is the source for the site header,
  the favicon, and the social-preview image.
- Real screenshots of the recording overlay in its listening, live-transcript,
  and processing states, plus one terminal capture of `murmly doctor`, taken on a
  Plasma session and committed as optimized PNGs with authored SVG diagrams for
  anything a screenshot cannot show (the hotkey → speak → paste loop, and where
  audio does and does not travel).
- Every capability claim on the page is traceable to something already measured
  or specified in this repository. The page carries no benchmark that does not
  exist here.
- The page states Murmly's real requirements — Fedora-first, KDE Plasma for the
  hotkey and overlay, Python 3.12+, a terminal for installation, X11 verified and
  Plasma Wayland not verified end to end — in plain language, above the fold in
  the install section rather than buried. A consumer-grade page that hides a
  Plasma-only hotkey produces an install that fails on first press.
- `README.md` gains a link to the site. The README stays the reference document;
  the page does not duplicate it.

### Not in scope

- A documentation site. One landing page linking to the README, `config.example.toml`,
  and the repository. No multi-page information architecture, no search, no
  versioned docs.
- A custom domain. The project-page URL is the deliverable; a `CNAME` can be added
  later without touching the page.
- Analytics, cookies, embedded fonts, or any request to a third-party host. The
  page is self-contained, which is also the only honest posture for a tool whose
  entire pitch is that nothing leaves the machine.
- A packaged installer or one-line `curl | sh`. The page links the existing
  `setup.sh` flow; changing how Murmly installs is a different change.

## Capabilities

### New Capabilities

- `project-website`: what the published project page must contain, what claims it
  is permitted to make, what it must disclose about requirements, how it is
  deployed and at what address, and the constraints that keep it self-contained
  and readable without a network round trip to anyone else.

### Modified Capabilities

<!-- None. The website describes existing behavior; it does not change any of it.
     No requirement in command-interface, desktop-integration, live-transcription,
     recording-overlay, speech-output, or transcript-delivery moves. -->

## Impact

- `site/` — new: `index.html`, one stylesheet, `assets/` for the logo, favicon,
  screenshots, and diagrams. No JavaScript is required for the page to be
  complete; any script is progressive enhancement only.
- `.github/workflows/pages.yml` — new: the build-free publish job.
- `README.md` — a link to the site near the top.
- `docs/agent-notes/` — a note on capturing the overlay screenshots, if the
  capture turns out to need a precondition that is not obvious (the overlay is a
  layer-shell surface on Wayland, which not every screenshot tool includes).
- No Python dependency changes. No change to `pyproject.toml`, `uv.lock`, or the
  test suite. The existing `tests.yml` workflow is untouched; the Pages workflow
  is separate so a site edit never re-runs the Python matrix and a test failure
  never blocks a page fix.
- One repository setting must be changed by hand, once: **Settings → Pages →
  Source → GitHub Actions**. The workflow cannot set this for itself, and the
  first deploy fails without it.
