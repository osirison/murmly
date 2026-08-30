## Context

See `proposal.md` — Why. The constraints that shape the approach:

- `openspec/changes/archive/2026-08-27-add-project-website/design.md` rejected a static
  site generator and named the condition that would reverse the decision: *"a second and
  third page would each duplicate the head, header, and footer. That is the point at
  which to reconsider, and it is not this change."* Moving 966 lines into a manual is
  that point. The many-pages objection is spent; the toolchain objection is not, and it
  is what still decides between candidates.
- The repository is Python, managed with `uv`. Its only Node use is a global
  `npm install -g @fission-ai/openspec` in `tests.yml`, with no manifest and no lockfile.
- `openspec/specs/project-website/spec.md` holds ten constraints written about "the page"
  singular, five of which most generator themes break by default: off-origin fonts,
  persistent browser storage, content that needs JavaScript, host-root asset paths, and
  third-party icon sets.
- The site is served from `/murmly/`, not a domain root.
- `docs/` is already occupied by `docs/agent-notes/`, which must never publish.
- The audience is explicitly non-technical. Navigation, ordering, and page titles are
  part of the deliverable, not decoration.

## Goals / Non-Goals

**Goals:**

- One place for each piece of reference material, reachable by someone who does not know
  the word "daemon".
- A toolchain that adds no Node, no second lockfile, and nothing to a developer's
  ordinary `uv sync`.
- Every constraint the landing page already satisfies extended to the manual, and checked
  against built output rather than against sources.
- A review guarantee that replaces "the published bytes are the committed bytes" with
  something a reviewer can actually run.

**Non-Goals:**

- Matching the landing page's components pixel for pixel. Colour, type, accent, focus
  ring, radius language and the mark are shared; Material's cards, admonitions and tab
  strips are its own.
- Search without JavaScript. It is client-side and it is an enhancement.
- Committing generated output to the repository or to a `gh-pages` branch.

## Decisions

### MkDocs with the Material theme

Verified by building a site with the configuration below before choosing it, not from
documentation.

| Option | Why not |
| --- | --- |
| Astro Starlight | Reintroduces exactly what the earlier design rejected: `package-lock.json`, a `node_modules` tree, and a second dependency-update surface in a repository whose only Node use has no manifest at all |
| Docusaurus | A React application and roughly a thousand transitive packages for a thirteen-page manual. `themeConfig.colorMode` persists the light/dark choice in `localStorage`, which the spec forbids outright and which must be disabled rather than merely not enabled |
| Sphinx with Furo | The closest runner-up; installs through `uv` identically. Rejected on the brief: Sphinx's native language is reStructuredText, and Markdown means `myst-parser` plus MyST directive syntax for anything past plain prose |
| Zola or Hugo | The smallest supply chain, and the best answer on reproducibility in the abstract. Rejected because this repository has exactly one pinning mechanism, `uv.lock`, and neither tool has a PyPI channel. CI would download a versioned tarball and verify a hand-maintained checksum |
| Jekyll | Needs Ruby. Branch-source Pages silently drops files beginning with `_`. The frontmatter trap the earlier design named is real in two places: `README.md` opens with a `---` block and so does every file under `docs/agent-notes/` |
| Stay hand-authored | Thirteen copies of the head, header, footer and navigation, hand-maintained, with no cross-link checking. The earlier design named this as the point to stop |

`uv sync --locked --only-group docs` installs 29 pure-Python packages and nothing else —
no `faster-whisper`, no `onnxruntime`, no CUDA wheels, no project install. Six of them are
already in `uv.lock`, so the lock grows by 23 entries.

`mkdocs` is pinned below 2. Material prints a warning on every build that MkDocs 2.0
removes the plugin system, rewrites theming, offers no migration path, and is currently
unlicensed. Until that settles, a major bump should be a deliberate `uv.lock` diff.

### `manual/` as the source directory, `_pages/` as the assembled artifact

```
mkdocs.yml            docs_dir: manual, site_dir: _pages/manual
manual/               Markdown only, nothing binary
overrides/            theme.custom_dir — Jinja partials
site/                 unchanged. Still the only copy of the mark and the assets
_pages/               gitignored. Assembled in CI, never committed
docs/agent-notes/     never read by the generator
```

Not `docs/`: a `docs_dir: docs` typo would publish the agent notes, and the whole point of
the directory choice is that the mistake is impossible rather than merely unlikely. Not
inside `site/`: everything in `site/` is copied to the artifact verbatim, so raw `.md`
files would be published alongside their rendered form.

### The landing page is copied, not regenerated

Rebuilding `site/index.html` as a Material page would put Material's bundle and its inline
head script onto the one page whose spec forbids persistent storage and demands
completeness without JavaScript, and would reopen ten already-verified requirements — the
1280×800 hero, the requirements panel, the inline SVGs that inherit `currentColor`, the
contrast checks — for no content gain.

CI runs `cp -r site/. _pages/` and then `diff -r site _pages --exclude=manual
--exclude=404.html`. A single differing byte fails the deploy before upload. This is
*stronger* than today's arrangement, which relies on `upload-pages-artifact` not modifying
its input.

`theme.logo` and `theme.favicon` are satisfied by copying two files out of `site/assets/`
during assembly, so there is no second copy of the mark in the repository to drift.

### What replaces "the published bytes are the committed bytes"

For `site/` the property survives and is now asserted rather than assumed. For the manual
it genuinely ends, and four things replace it:

1. **Pinned toolchain.** `uv.lock` fixes exact versions and hashes; `--locked` fails if
   `pyproject.toml` and the lock have drifted.
2. **Local reproduction.** `uv run --no-sync mkdocs build` produces the identical tree, so
   a reviewer can build the artifact and diff it against what is deployed.
3. **`strict: true`.** A link to a page that does not exist, or an unrecognised config
   key, fails the build and therefore the deploy. The hand-authored site never had this
   check; the earlier design listed its absence as a risk.
4. **Assertions on the artifact.** The origin check and the agent-notes check move from
   grepping sources to grepping `_pages/`, which is the thing actually served.

The honest residue: nobody reads Material's 114 KB minified bundle in review. It is
version-pinned, its behaviour was characterised empirically, and a bump is a reviewable
lock diff rather than a silent update.

### Four configuration keys are forbidden, in a comment in `mkdocs.yml`

Each of these silently breaks a spec requirement. They are named so a future maintainer
adding one for convenience meets the reason first.

| Key | What it breaks |
| --- | --- |
| `repo_url` | Material fetches `api.github.com` at view time for star and fork counts. Breaks origin-only, and publishes a star count, which the claims requirement forbids |
| `theme.palette` with a `toggle:` entry | Writes `__palette` to `localStorage` |
| `features: content.tabs.link` | Writes `__tabs` to `localStorage` the first time a reader clicks a linked tab |
| `features: announce.dismiss` | Writes `__announce` to `localStorage` |

`pymdownx.tabbed` with `alternate_style: true` still renders radio-input tabs — for the
X11/Wayland forks — with no script and no storage. Only cross-block linking is lost.

Dark mode is CSS, not JavaScript: with no `palette:` key Material never writes
`data-md-color-scheme` onto `<html>`, so `palette.css` is emitted but never linked, and a
`prefers-color-scheme` block in `manual/stylesheets/murmly.css` is the whole mechanism.
That block must redefine the fourteen `--md-code-hl-*` tokens as well as the base ones:
they are defined for the light scheme in `main.css` and redefined only under
`[data-md-color-scheme=slate]`, which never matches here. Without them the syntax colours
stay light-scheme on a dark background, which is a contrast failure, not a cosmetic one.

### Three things Material does not do, that overrides must

- **Tables do not scroll without JavaScript.** Material's `md-typeset__scrollwrap`
  container is built by a script; the server-rendered HTML carries a bare `<table>`. With
  scripting off a wide table scrolls the document body. Thirteen tables move to the manual,
  several of them four to six columns. `manual/stylesheets/murmly.css` adds
  `.no-js .md-typeset table:not([class]) { display: block; overflow-x: auto; max-width: 100%; }`
  — `no-js` is on `<html>` in every generated page and removed by the bundle, so the rule
  applies exactly when the wrapper will not arrive.
- **Generated pages emit no Open Graph tags.** Material emits only `description`,
  `canonical`, `prev`/`next`, `icon` and `generator`. Its `social` plugin would fix this
  and is rejected because it downloads fonts at build time. `overrides/main.html` adds
  `og:type`, `og:title`, `og:description`, `og:url` from `page.canonical_url`, `og:image`
  pointing at the existing `social-preview.png`, and `twitter:card` in about eight lines.
- **The theme ships its own favicon and its own footer credit.** `assets/images/favicon.png`
  is emitted even when `theme.favicon` is overridden and is deleted during assembly;
  `overrides/partials/copyright.html` removes the `squidfunk.github.io` link, the only
  off-origin `href` in the built HTML.

### Material's icons are carried under the Pictogrammers Free License

`main.css` embeds 42 `data:image/svg+xml` icons — the twelve admonition symbols, the
hamburger, search, chevrons, back-to-top and copy glyphs — and the wheel ships four icon
sets under `material/templates/.icons/`. These are same-origin by construction, so an
origin grep cannot see them, but the mark requirement is about provenance, not about hosts.

Admonitions are the single largest readability gain for a non-technical reader, so the
icons stay and the licence is carried: `material/templates/.icons/material/LICENSE`
(Pictogrammers Free License) is vendored to `licenses/`. This is a deliberate, recorded
exception to "authored in this repository", and the spec's mark requirement is worded to
permit exactly this — carried under a license permitting redistribution, with the license
text committed alongside.

### The page tree is task-shaped, and it is not the README's headings

A manual whose navigation reads *Transcript delivery, Auto-transcribe, Scope and
limitations* is the README with a sidebar. Thirteen pages plus an index:

| # | Page | Title | ~lines |
| --- | --- | --- | --- |
| 1 | `index.md` | The murmly manual | 40 |
| 2 | `what-you-need.md` | What you need before you start | 90 |
| 3 | `install.md` | Installing murmly | 170 |
| 4 | `using-murmly.md` | Using murmly | 60 |
| 5 | `changing-your-hotkey.md` | Changing your hotkey | 55 |
| 6 | `where-your-words-go.md` | Where your words go | 95 |
| 7 | `words-as-you-speak.md` | Seeing your words as you speak | 60 |
| 8 | `pause-to-finish.md` | Finishing a recording by pausing | 55 |
| 9 | `making-murmly-speak.md` | Making murmly speak | 90 |
| 10 | `announcements.md` | Hearing when your coding assistant finishes | 150 |
| 11 | `settings.md` | All the settings | 140 |
| 12 | `speed-and-memory.md` | Speed, memory, and your graphics card | 190 |
| 13 | `troubleshooting.md` | When something goes wrong | 110 |
| 14 | `for-developers.md` | For developers | 140 |

`speed-and-memory` is the largest structural change: what is currently spread across
*Where synthesis runs*, *Releasing idle model memory*, and the profile-mapping tail of
*The command socket* is one subject — what Murmly holds and how fast it is — and belongs
on one page.

`for-developers` is last and is the only page written for someone building against Murmly
rather than using it: the session protocol in full and the command socket's permission
rules. It is not the README's `## Development` section, which is contributor material and
does not go to the site at all.

### The authoritative section map

Every heading in `README.md`, with the line it starts at, its length, and where it goes.
Nothing may be dropped without appearing in this table.

| README:line | Len | Heading | Destination |
| --- | --- | --- | --- |
| 6 | 13 | `## Overview` | README (pitch verbatim); the Wayland-hotkey paragraph at 15-17 is dropped from user-facing text |
| 19 | 28 | `## Requirements` | `what-you-need` |
| 47 | 41 | `### Pasting with ydotool` | `install` (47-67); overlay packages at 68-79 to `install`; the `RTLD_GLOBAL` paragraph at 80-86 is not user-facing and is dropped |
| 88 | 35 | `## Install` | `install`, and the command plus three `setup.sh` forms stay in README |
| 123 | 42 | `### Installing by hand` | `install` |
| 165 | 9 | `### Choosing a hotkey` | `install` |
| 174 | 11 | `### What installation writes` | `install` |
| 185 | 11 | `## Use it` | README, verbatim; also opens `using-murmly` |
| 196 | 30 | `## Change or remove the hotkey` | `changing-your-hotkey` |
| 226 | 13 | `## Speech output` | `making-murmly-speak` |
| 239 | 25 | `### Turning it on` | `making-murmly-speak` |
| 264 | 69 | `### Where synthesis runs` | `speed-and-memory` |
| 333 | 56 | `### Announcing a finished agent turn` | `announcements` |
| 389 | 51 | `#### Asking the agent for a voice note` | `announcements` |
| 440 | 14 | `### The two hotkeys` | `making-murmly-speak` |
| 454 | 63 | `### The session protocol` | `for-developers` |
| 517 | 7 | `### What speech output does not do` | `making-murmly-speak` |
| 524 | 17 | `## Scope and limitations` | split three ways — see below |
| 541 | 64 | `## Configuration` | `settings` |
| 605 | 51 | `### The command socket` | split two ways — see below |
| 656 | 30 | `### Live transcription` | `words-as-you-speak` |
| 686 | 28 | `### Auto-transcribe` | `pause-to-finish` |
| 714 | 84 | `### Releasing idle model memory` | `speed-and-memory` |
| 798 | 24 | `## Transcript delivery` | `where-your-words-go` |
| 822 | 14 | `### What each session gets` | `where-your-words-go` |
| 836 | 27 | `### Restoring your previous clipboard` | `where-your-words-go` |
| 863 | 13 | `## The recording overlay` | `using-murmly` |
| 876 | 55 | `## Troubleshooting` | `troubleshooting` |
| 931 | 36 | `## Development` | six lines stay in README; the rest leaves the site entirely |

**Two headings split.** `### The command socket` (605-655) is two subjects that share a
heading by accident of ordering: the socket ownership and directory-permission rules
(610-641) go to `for-developers`; the profile mapping and device resolution (645-655) are
about models, not sockets, and go to `speed-and-memory`. `## Scope and limitations`
(524-540) splits three ways: the Plasma-only and X11-verified bullets to `what-you-need`,
the resident-model bullet to `speed-and-memory`, the live-transcript privacy bullet to
`words-as-you-speak`.

### `README.md` at about 62 lines

`murmly` (12) · What you need (10) · Install (14) · Use it (8) · It can also speak (6) ·
Documentation (6) · Development (6).

Two passages are kept **verbatim** because they are the only two in the current 966 lines
already written in the register a non-technical reader needs: the pitch at 11-13 and the
three-step loop at 185-195.

`## Development` stays, at six lines, for one reason: everyone who installs Murmly clones
this repository, so a contributor is already reading this file, and
`uv run --no-sync python -m unittest discover -s tests` is the command `CLAUDE.md` names as
canonical. Moving it off-repo would make the project's own instructions point at a website.
Everything else in the current 36 lines leaves.

The `---` frontmatter at lines 1-4 stays in `README.md` — it is what the earlier design
cites as the reason Jekyll was rejected — and is not carried into any `manual/` page, where
MkDocs would consume it as page metadata.

### The moved-anchors map lives on the manual index, not in the README

Anyone who bookmarked `README.md#configuration` on GitHub gets a silently no-op fragment
once that heading is gone. GitHub cannot redirect a fragment; neither can a Pages site;
`mkdocs-redirects` is irrelevant because the stale links point at GitHub.

A thirteen-row "where things moved" table in the README would work and is what a strict
reading of the problem asks for. It is not what goes in, because the brief is that the
README be as simple as possible, and a table of removed headings is the opposite of that.
Instead: the reader who follows a dead anchor lands at the top of a 62-line README whose
second element is a prominent link to the manual, and the manual's index carries the map.
The cost is one extra click for a small number of people, paid once.

## Risks / Trade-offs

- **A generated site can drift from what was reviewed.** → Pinned in `uv.lock`,
  reproducible locally with one command, `strict: true` on the build, and CI assertions
  run against `_pages/` rather than against sources.
- **A theme upgrade can reintroduce an off-origin request or a storage write.** → The
  origin grep and a storage check run over built output on every deploy, not once at
  adoption. The forbidden-key list is a comment in `mkdocs.yml` and an assertion in CI.
- **Reviewing the landing page with `file://` stops working.** `use_directory_urls: true`
  emits `href=".."` and `href="./"`, which do not resolve as documents over `file://`, and
  `site/index.html`'s new `manual/` link is dead there too. → Review is
  `python -m http.server -d _pages`. This is a real regression in the review workflow and
  is written into the tasks so it is not rediscovered.
- **`python -m http.server` serves at the host root, not under `/murmly/`.** It exercises
  the relative paths but not the absolute ones in `404.html`. → Those are only verifiable
  against the published prefix, which is what
  `docs/agent-notes/github-pages-source-setting.md` already says: open the published URL
  and read the network log; do not trust the green badge.
- **`uv run mkdocs serve` without `--no-sync` syncs first**, and a sync reinstalls the CPU
  build of `onnxruntime` over a GPU swap — the exact failure
  `docs/agent-notes/onnxruntime-gpu-cuda-version.md` warns about, where the suite still
  passes and every synthesis measurement afterwards silently reports a CPU session.
  `uv sync --locked --only-group docs` locally is worse: `uv` prunes, so it strips the
  runtime out of the developer's `.venv`. → Preview with
  `uvx --from "mkdocs-material==<pinned>" mkdocs serve`, which touches `.venv` not at all.
  This becomes a field note.
- **The package long description shrinks.** `pyproject.toml` sets `readme = "README.md"`,
  so what PyPI and `pip show` display goes from 966 lines to 62. Probably an improvement;
  recorded so it is a decision rather than a surprise.
- **`sitemap.xml` covers only the manual.** MkDocs writes it for its own tree, so the
  landing page — the site's root, and the page carrying the canonical and Open Graph tags
  — is absent from the only sitemap served. Accepted deliberately rather than hand-writing
  a second one; the landing page is the address people are given directly.
- **Thirteen new pages is thirteen new places for a claim to drift from its source.** → The
  claims requirement is rewritten so a documentation page cannot be its own source; every
  measured figure keeps the sentence naming the machine it was measured on.

## Migration Plan

No repository setting changes: Pages is already published from GitHub Actions, so
`docs/agent-notes/github-pages-source-setting.md` needs no action this time.

1. Land the toolchain and an empty manual first: `pyproject.toml`, `uv.lock`, `mkdocs.yml`,
   `overrides/`, `manual/index.md`, `.gitignore`, and the `pages.yml` build. Deploy and
   confirm the landing page is byte-identical and the manual index resolves under
   `/murmly/manual/`. Nothing has been removed from the README at this point, so the
   published site is strictly additive and the change is safe to stop at.
2. Write the thirteen pages, verifying each against the section map and the at-risk
   checklist in `tasks.md`.
3. Only then cut `README.md`, and change the one link in `site/index.html`.

Rollback at any point before step 3 is deleting `manual/` and reverting `pages.yml`; the
landing page is untouched throughout. After step 3, rollback is a revert of the README
commit — the manual can stay published either way.
