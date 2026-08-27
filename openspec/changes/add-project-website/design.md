## Context

See `proposal.md` — Why. The constraints that shape the approach:

- The repository is public on GitHub at `osirison/murmly`, so the page's address
  is a project page under a path prefix — `https://osirison.github.io/murmly/` —
  not a domain root.
- The repository carries no JavaScript, no Node dependency, and no bundler. Its
  CI installs Node once, only to run the OpenSpec CLI.
- `docs/` already exists and holds `docs/agent-notes/`, which is internal
  material not meant for publication.
- The claims worth making are already written down: `README.md` and
  `openspec/specs/` hold every fact the page needs, including two measured
  tables.
- The user's brief asks for elegance, consumer-facing UX, graphics, screenshots,
  and a logo. Murmly is a terminal-installed daemon for Fedora with KDE Plasma.
  Both are true at once, and the design has to hold them together rather than
  pick one.

## Goals / Non-Goals

**Goals:**

- One page that a non-specialist understands in fifteen seconds and can act on.
- Zero build step: the published bytes are the committed bytes.
- Zero third-party requests, which is the only posture consistent with a tool
  whose pitch is that nothing leaves the machine.
- Graphics that carry information — the recording loop, where audio does and does
  not travel — rather than decoration.
- A visual identity that survives at 16 px and in monochrome.

**Non-Goals:**

- A design system, a component library, or reusable page templates. This is one
  page; a second page can copy the stylesheet when there is one.
- Pixel-matching any particular desktop theme in the illustrations. The
  screenshots carry the real look; the diagrams are deliberately flat.
- Supporting browsers without CSS custom properties, `flex`/`grid`, or
  `prefers-color-scheme`. All three are a decade old.

## Decisions

### Hand-authored HTML and CSS, no static site generator

One page, one stylesheet, one directory of assets. The alternatives and why not:

| Option | Why not |
| --- | --- |
| Jekyll (GitHub Pages' built-in) | Publishing from a branch runs Jekyll implicitly, which needs a `.nojekyll` escape hatch or a `_config.yml`, and adds Ruby to a Python repository's mental load for one page. Its own frontmatter is already a trap here: `README.md` opens with a `---` block, which Jekyll would consume. |
| Astro, Eleventy, Docusaurus | A `node_modules` tree, a lockfile, and a dependency-update surface larger than the page. Every one of them exists to solve the many-pages problem this change does not have. |
| Tailwind or any CSS framework | Requires a build to be usable at a sane size, and its output is harder to read than the ~300 lines of CSS this page needs. |

The cost of the choice is that a second and third page would each duplicate the
head, header, and footer. That is the point at which to reconsider, and it is not
this change.

### Publish with the Pages Actions workflow, not from a branch

`.github/workflows/pages.yml` runs `actions/configure-pages`,
`actions/upload-pages-artifact` with `path: site/`, and `actions/deploy-pages`.
It is gated on `push` to `main` and `workflow_dispatch`, with
`permissions: {contents: read, pages: write, id-token: write}` and a
`concurrency` group so two pushes cannot deploy out of order.

Against the older approach — commit built output to a `gh-pages` branch, or point
Pages at `docs/` on `main`:

- Nothing is built, so a branch of build output would hold a byte-identical copy
  of `site/` and a second history to keep in sync.
- Branch-source Pages runs Jekyll over the directory unless `.nojekyll` is
  present, and silently drops files whose names begin with `_`. The Actions path
  uploads the directory verbatim.
- `docs/` is already spoken for by `docs/agent-notes/`, which must not publish.

The workflow is separate from `tests.yml` deliberately: a typo fix on the page
should not re-run the Python matrix, and a failing test should not block a page
correction. This is the spec's "Publication is independent of the test suite".

**This requires one manual repository setting**: Settings → Pages → Source →
*GitHub Actions*. A workflow cannot set it for itself, and the first deploy fails
without it. It is the first item in the migration plan.

### `site/` at the repository root

```
site/
  index.html
  style.css
  assets/
    murmly-mark.svg          the logo, the source of every derived image
    murmly-wordmark.svg
    favicon.svg              plus favicon.ico / apple-touch-icon.png derived from it
    social-preview.png       1200x630, generated from the SVG
    overlay-listening.png    real captures
    overlay-partial.png
    overlay-processing.png
    doctor.png
    diagram-loop.svg         authored illustrations
    diagram-local.svg
```

Not `docs/` (internal notes live there), not `www/` or `public/` (no convention
in this repo to follow, and `site/` says what it is).

### Relative asset paths, with two deliberate absolute URLs

Every `href` and `src` in the document is relative — `assets/…`, `style.css` —
so the page works under the `/murmly/` prefix and also when opened from local
disk, which is how it will be reviewed before it is ever published.

Two tags must carry absolute URLs, because the specifications for them require
it: `<meta property="og:image">` and `<link rel="canonical">`. Link-preview
crawlers do not resolve a relative `og:image` against the page. These are the
only two, and the spec's requirement is that references resolve under the prefix
— an absolute URL to the published address does resolve.

### System fonts, no webfont file

The type stack is the platform's own UI and monospace faces. A bundled webfont
would be 30–150 KB for a page of a few hundred words, and a hosted one would
break the no-third-party-requests requirement outright. The consequence is that
the page looks slightly different per platform; for a page whose audience is
overwhelmingly on a Linux desktop, that is a small cost and it makes the page
feel native rather than branded.

### The mark: a speech pill that resolves into a text caret

The logo is an original SVG. The concept is the transformation the product
performs, not a microphone icon: a rounded speech-bubble form containing three
vertical bars taken from the overlay's own waveform, where the rightmost bar is
drawn at the proportions of a text caret. Voice on the left, text on the right,
in one silhouette.

Properties this buys, and which the spec requires:

- Legible at 16 px, because it is three bars in an outline — no detail to lose.
- Works in one colour. Colour is applied via `currentColor` so the same file
  serves the light header, the dark header, and the monochrome favicon.
- Derivable: the favicon, the apple-touch icon, and the 1200×630 social preview
  are all rendered from this one file rather than drawn separately.

The wordmark is "murmly" set lowercase in the stack's own face with tightened
tracking, not a lettered logotype. Lowercase because that is how the command is
typed. No third-party icon set anywhere on the page.

### Colour, in both schemes, from one accent

CSS custom properties on `:root` define the light palette; a
`@media (prefers-color-scheme: dark)` block redefines only the tokens. One accent
hue, used for the primary action, the active waveform in the diagrams, and link
focus. Every text/background pair is checked against 4.5:1 (3:1 for large text)
in both schemes before the page ships — that check is a task, not an aspiration.

### Screenshots are captured, not mocked

The overlay is the product's face and a drawing of it would be a lie the spec
forbids. Capture procedure:

- Take them on an **X11 Plasma session**. On Wayland the overlay is a
  `gtk4-layer-shell` surface, and not every screenshot tool includes layer-shell
  surfaces in a region capture. X11 is also the session Murmly is verified on, so
  the captures show the verified configuration.
- Use Spectacle's rectangular-region capture over the bottom-centred overlay,
  against a neutral desktop background with no personal content in frame.
- Capture at the display's native scale and commit one PNG per image, run through
  a lossless optimizer. Each `<img>` carries explicit `width` and `height` so the
  page does not reflow as images arrive, and every image below the first screen
  carries `loading="lazy"`.
- If a state genuinely cannot be captured, it is drawn and labelled an
  illustration — never captioned as a screenshot.

Screenshots go stale when the overlay changes. Mitigation is in the migration
plan: the capture procedure is written down as an agent note so the next person
re-shoots rather than re-derives.

### Two authored diagrams, inline SVG

- **The loop**: press → speak → press → text appears, as three panels. This is
  the first screen's graphic and it replaces the paragraph the README opens with.
- **Where the audio goes**: a boundary drawn around the machine, with the
  microphone, the model, and the target window inside it, and nothing crossing
  out. One arrow crosses in, once, labelled "first run: model download" — because
  that is true and omitting it would make the local-only claim overstated.

Both are inline SVG in the document rather than `<img>`, so they inherit
`currentColor` and change with the colour scheme without a second file.

### Claims are sourced from `README.md` and `openspec/specs/` only — never from `openspec/changes/`

`openspec/changes/` holds proposals that are planned but not implemented.
`unload-idle-gpu-models`, for example, carries an attractive figure — 2080 MiB
reclaimed — for behaviour that does not exist yet. Anything sourced from there
would be a claim about software nobody can install. The baseline for the page is
what has been archived into `openspec/specs/` plus what `README.md` documents as
shipped.

The claim set the page will draw on, each with its source:

| Claim on the page | Source |
| --- | --- |
| Everything runs on your machine; audio is never uploaded | `README.md` — Overview |
| First run downloads the model, then nothing leaves | `README.md` — Configuration, profile mapping |
| A transcript is never lost to a paste that silently failed | `README.md` — Requirements; `openspec/specs/transcript-delivery` |
| It refuses to paste if you changed windows mid-dictation | `config.example.toml` — `clipboard.verify_target` |
| Partial transcripts on screen never reach the clipboard | `openspec/specs/live-transcription` |
| Three model profiles, from `tiny.en` to `large-v3` | `README.md` — profile mapping |
| It can speak, at roughly five times real time | `README.md` — Speech output |
| Speech output is off until you turn it on | `README.md`; `config.example.toml` |
| Apache-2.0, no account, no subscription | `LICENSE`, `pyproject.toml` |

### Page order

1. **Hero** — mark, wordmark, one sentence, the loop diagram, the install button,
   and a compact "Fedora · KDE Plasma · Python 3.12+" line. The requirements are
   on the first screen, stated as a fact rather than a warning.
2. **How it works** — three steps, each with its real screenshot.
3. **What makes it different** — four points from the claim table, each one
   sentence and one supporting detail. Local-only carries the boundary diagram.
4. **It also speaks** — the speech-output section, including the agent-announce
   hook, marked as optional and off by default.
5. **Install** — the full requirements panel, the command, and the link to the
   README. Nothing here is a second copy of the README.
6. **Footer** — licence, repository, spec directory.

### Consumer polish without overstatement

The brief asks for mainstream appeal. The resolution: **the craft is consumer
grade, the claims stay literal.** Large type, generous spacing, real imagery, one
clear action — and no superlative that is not measured, no comparison to a named
product, no invented user count. The requirements panel is designed to look like
part of the product rather than a disclaimer, which is what lets it stay honest
without reading as a warning label. Presenting a Plasma-only hotkey as
universally ready would produce installs that fail on first keypress, which costs
more goodwill than the install it wins.

## Risks / Trade-offs

- **The Pages source setting is manual and the first deploy fails without it** →
  It is step 1 of the migration plan, before the merge, and the tasks verify the
  live URL rather than a green workflow badge.
- **Screenshots drift as the overlay changes** → The capture procedure is written
  as an agent note under `docs/agent-notes/`, and the images are named for the
  state they show so a stale one is identifiable.
- **A future contributor adds a font CDN, an analytics snippet, or a badge
  service** → The no-third-party rule is a spec requirement with a scenario, and
  the verification task is a grep over `site/` for subresource URLs pointing off
  origin. Links to `github.com` are navigations, not requests, and are allowed.
- **The page and the README diverge** → The page states no configuration
  reference and no protocol detail. Where it repeats a fact, it repeats a small
  one that has been stable across the project's history.
- **PNG weight in a Python repository's history** → Optimized captures, a budget
  of roughly 150 KB per screenshot and under 1 MB for `site/assets/` in total.
  The diagrams are SVG, which costs kilobytes.
- **Hand-authored HTML means no template guard against a broken relative path** →
  The verification task opens the built page from disk and from the published
  prefix, and checks for a 404 in the network log rather than by eye.
- **No build step means no minification** → A few hundred lines of CSS and one
  HTML file. Minification would save less than one screenshot.

## Migration Plan

1. Set Settings → Pages → Source → *GitHub Actions* on the repository. Nothing
   else in this plan works before this.
2. Land `site/` and `.github/workflows/pages.yml` on `main` through the usual
   pull request.
3. Confirm the deploy in the Actions run, then open
   `https://osirison.github.io/murmly/` and confirm every asset resolves under
   the `/murmly/` prefix, not just that the workflow is green.
4. Set the repository's social preview image to the generated
   `social-preview.png`, and add the site URL to the repository's About field.
5. Add the site link to `README.md`.

Rollback: revert the commit. Pages redeploys from `main` on push, so a revert
republishes the previous state; if the page must come down entirely, set
Settings → Pages → Source → *None*, which unpublishes without touching the
history.

## Open Questions

- Whether to register a custom domain later. It does not affect this change:
  adding a `CNAME` file and a DNS record works against the same `site/` directory
  and the same workflow, and only the two absolute URLs would change.
- Whether a short screen recording of a dictation belongs on the page. A silent,
  looping capture would be persuasive, but it is a video budget and an
  accessibility surface of its own. Deferred; the still captures are sufficient
  for a first page and nothing here forecloses adding one.
