## 1. Toolchain and publishing pipeline

- [x] 1.1 Add a `docs` dependency group to `pyproject.toml` pinning `mkdocs>=1.6.1,<2` and `mkdocs-material>=9.7,<10`. Do **not** add it to `tool.uv.default-groups` — a developer's plain `uv sync` and the test matrix must be unchanged. Comment why `mkdocs` is pinned below 2 (MkDocs 2.0 removes the plugin system, rewrites theming, has no migration path, and is currently unlicensed)
- [x] 1.2 Run `uv lock`. Confirm the lock grows by about 23 pure-Python entries and that `uv sync --locked --only-group docs` installs 29 packages with no `faster-whisper`, no `onnxruntime`, no CUDA wheel and no project install
- [x] 1.3 Confirm a plain `uv sync` and `uv run --no-sync python -m unittest discover -s tests` are unaffected by the new group
- [x] 1.4 Add `_pages/` to `.gitignore`
- [x] 1.5 Create `mkdocs.yml` at the repository root: `site_name: murmly`, `site_url: https://osirison.github.io/murmly/manual/`, `docs_dir: manual`, `site_dir: _pages/manual`, `use_directory_urls: true`, `strict: true`, `theme.name: material`, `theme.custom_dir: overrides`, `theme.font: false`, `theme.logo: assets/murmly-mark.svg`, `theme.favicon: assets/favicon.ico`, `extra.homepage: https://osirison.github.io/murmly/`, `extra_css: [stylesheets/murmly.css]`, `plugins: [search]`
- [x] 1.6 Set `theme.features` to `navigation.sections`, `navigation.expand`, `navigation.footer`, `navigation.top`, `toc.follow`, `content.code.copy`. Add nothing else without checking it against 1.7
- [x] 1.7 Write the forbidden-key comment block into `mkdocs.yml`, naming all four and what each breaks: `repo_url` (fetches `api.github.com` at view time and publishes a star count), `theme.palette` with any `toggle:` entry (writes `__palette` to localStorage), `features: content.tabs.link` (writes `__tabs`), `features: announce.dismiss` (writes `__announce`)
- [x] 1.8 Add `markdown_extensions`: `admonition`, `attr_list`, `md_in_html`, `tables`, `toc` with `permalink: true`, `pymdownx.details`, `pymdownx.highlight` with `anchor_linenums: true`, `pymdownx.superfences`, and `pymdownx.tabbed` with `alternate_style: true` (radio-input tabs, no script, no storage)
- [x] 1.9 Write the `nav:` block using the titles from `design.md`'s page tree, not the README's headings. End it with absolute-URL entries for the landing page and the GitHub source
- [x] 1.10 Edit `.github/workflows/pages.yml`: add `manual/**`, `overrides/**`, `mkdocs.yml`, `pyproject.toml` and `uv.lock` to the `paths` trigger
- [x] 1.11 Replace the upload step with: `astral-sh/setup-uv@v6` (python 3.12, cache on), `uv sync --locked --only-group docs`, `uv run --no-sync mkdocs build`. Keep the `--no-sync` comment explaining that a plain `uv run` would drag the whole ML runtime into a runner that is here to render Markdown
- [x] 1.12 Add the assembly step: `cp -r site/. _pages/`; copy `murmly-mark.svg` and `favicon.ico` from `site/assets/` into `_pages/manual/assets/`; `cp _pages/manual/404.html _pages/404.html`; `rm -f _pages/manual/assets/images/favicon.png` (Material emits its own favicon even when `theme.favicon` is set); then `diff -r site _pages --exclude=manual --exclude=404.html`, which must produce no output
- [x] 1.13 Add the internal-notes guard: fail if `find _pages -name '*agent-note*'` matches anything or `grep -rql 'agent-notes' _pages` finds anything
- [x] 1.14 Add the configuration guard: fail unless `mkdocs.yml` contains `docs_dir: manual`, and fail if it contains `docs_dir: docs` or any of the four forbidden keys. A one-line typo is the whole failure mode
- [x] 1.15 Add the origin guard over built output: no `href`/`src` naming a host other than the publishing origin, checked in HTML **and** in CSS
- [x] 1.16 Point `upload-pages-artifact` at `_pages/`. Leave `permissions`, `concurrency` and `environment` unchanged
- [x] 1.17 Add a `docs` job to `.github/workflows/tests.yml` running `uv sync --locked --only-group docs && uv run --no-sync mkdocs build`, so a broken internal link fails at review rather than at deploy. Confirm `pages.yml` still neither runs nor waits on the Python matrix
- [x] 1.18 Deploy with only `manual/index.md` present. Open `https://osirison.github.io/murmly/` and `https://osirison.github.io/murmly/manual/` and read the network log — a green badge is not evidence

## 2. Identity, and the three things Material gets wrong

- [x] 2.1 Write `manual/stylesheets/murmly.css` redefining Material's tokens from `site/style.css`'s values: `--md-text-font-family`, `--md-code-font-family`, `--md-default-bg-color` (`#ffffff`), `--md-default-bg-color--light` (`#f5f6f8`), `--md-default-fg-color` (`#111318`), `--md-default-fg-color--light` (`#4a4f5a`), `--md-default-fg-color--lighter` (`#e2e5ea`), `--md-code-bg-color`, `--md-primary-fg-color` (`#5b3df5`), `--md-primary-bg-color`, `--md-accent-fg-color`, `--md-typeset-a-color`
- [x] 2.2 Add the `@media (prefers-color-scheme: dark)` block with `site/style.css`'s dark values (`#0d0f14`, `#161a22`, `#eef0f4`, `#a8b0be`, `#262c38`, accent `#a78bfa`). Dark mode is CSS here, not JavaScript: with no `palette:` key Material never writes `data-md-color-scheme`, so `palette.css` is emitted but never linked
- [x] 2.3 In that same dark block, redefine all fourteen `--md-code-hl-*` tokens (`color`, `color--light`, `comment`, `constant`, `function`, `generic`, `keyword`, `name`, `number`, `operator`, `punctuation`, `special`, `string`, `variable`). Without them the syntax colours stay light-scheme on a dark background, which is a contrast failure, not a cosmetic one
- [x] 2.4 Copy `site/style.css`'s `:focus-visible` rule and its `prefers-reduced-motion` block verbatim. Material's own reduced-motion rule stops transitions but not animations
- [x] 2.5 Add `.no-js .md-typeset table:not([class]) { display: block; overflow-x: auto; max-width: 100%; }`. Material's table scroll wrapper is built by a script; the server-rendered HTML is a bare `<table>`. `no-js` sits on `<html>` and is removed by the bundle, so the rule applies exactly when the wrapper will not arrive
- [x] 2.6 Create `overrides/partials/copyright.html` dropping Material's `squidfunk.github.io` footer credit — the only off-origin `href` in built HTML
- [x] 2.7 Create `overrides/partials/logo.html` replacing Material's `alt="logo"` default with a real text alternative
- [x] 2.8 Create `overrides/main.html` with a `{% block extrahead %}` emitting `og:type`, `og:title` (`{{ page.title }} — murmly`), `og:description`, `og:url` from `{{ page.canonical_url }}`, `og:image` pointing at the existing `social-preview.png`, and `twitter:card`. Material emits no Open Graph tags, and its `social` plugin is rejected because it downloads fonts at build time
- [x] 2.9 Vendor `material/templates/.icons/material/LICENSE` (Pictogrammers Free License) to `licenses/pictogrammers-free-license.txt`. Material's CSS embeds 42 `data:` SVG icons including the twelve admonition symbols; they are same-origin by construction so an origin grep cannot see them, but the mark requirement is about provenance
- [x] 2.10 Confirm no page shows Material's mark or favicon: check the assembled artifact after 1.12's `rm`

## 3. The manual

Write each page from the section map in `design.md`. Titles and order come from that map, not from the README's headings.

- [x] 3.1 `manual/index.md` — what the manual covers, where to start, and the "where things moved" table mapping every `README.md` heading removed by this change to the page that now holds it
- [x] 3.2 `manual/what-you-need.md` — Fedora, Plasma, Python 3.12+, a terminal, and the X11-verified/Wayland-unverified status first. The paste-injector matrix and the KDE permission dialog below, under a heading phrased by symptom ("If murmly says it cannot paste"). Rewrite the KDE dialog paragraph as an instruction, not an explanation
- [x] 3.3 `manual/install.md` — open with the five disclosures (Fedora, Plasma, Python 3.12+, terminal, X11-verified) **before** the first command; the spec requires them on any page that invites an install. Then one command, `murmly doctor`, choosing a hotkey, what installation writes. Below that: installing by hand, the GPU runtime, the overlay's system packages, and the ydotool fallback under "If murmly cannot paste on your desktop"
- [x] 3.4 `manual/using-murmly.md` — the three-step loop, the three overlay screenshots already in `site/assets/`, that the overlay takes neither keyboard nor pointer, and how to switch it off
- [x] 3.5 `manual/changing-your-hotkey.md` — rebinding, moving both keys at once, removing, and repairing after moving the project folder under its own heading phrased by symptom ("If you moved the murmly folder")
- [x] 3.6 `manual/where-your-words-go.md` — answer "why did nothing get pasted?" first, then the per-session capability matrix and clipboard restoration
- [x] 3.7 `manual/words-as-you-speak.md` — one setting, the guarantee that partials can never change what gets typed, the shared-screen warning, and the honest note that `balanced` on CPU cannot keep pace
- [x] 3.8 `manual/pause-to-finish.md` — the three modes described by what the user experiences, plus the two surprises: a muted microphone will not end a recording, and an auto-stopped recording pastes without printing
- [x] 3.9 `manual/making-murmly-speak.md` — turning it on, the two hotkeys, and what speech output does not do. Link to `speed-and-memory` for the processor choice and to `announcements` for the hook
- [x] 3.10 `manual/announcements.md` — open with what it sounds like. `./setup.sh hooks` for both agents, the voice-note convention including the exact `AGENTS.md` text, the four environment variables, replacing the chime, and every file registration touches with how to undo it
- [x] 3.11 `manual/settings.md` — where the file lives, that copying the example changes nothing, that an out-of-range value falls back rather than refusing to start, then every key at its default with its range and a one-line plain-language gloss. Give each key an anchor so task pages can link straight to it. End with the restart command
- [x] 3.12 `manual/speed-and-memory.md` — the three profiles and their models, how `auto` resolves, download sizes, why speech uses the CPU by default with the measured table, the ONNX Runtime GPU swap, idle release and what it costs, and the upgrade note. Every figure keeps the sentence naming the machine
- [x] 3.13 `manual/troubleshooting.md` — symptom-first, starting with `murmly doctor`. Restate the Intel SOF microphone fix **in the documentation's own words**; do not reproduce text from `docs/agent-notes/murmly-spike-sof-dmic.md` and do not link to it
- [x] 3.14 `manual/for-developers.md` — the session protocol in full and the command socket's ownership and permission rules. Last in the nav
- [x] 3.15 Confirm no `manual/*.md` file opens with a `---` frontmatter block; MkDocs would consume it as page metadata
- [x] 3.16 Read the nav aloud and check that no entry is titled only with an internal component name

## 4. README and the landing page

- [x] 4.1 Cut `README.md` to about 62 lines: `murmly` (12), What you need (10), Install (14), Use it (8), It can also speak (6), Documentation (6), Development (6)
- [x] 4.2 Keep the pitch (current lines 11-13) and the three-step loop (current 185-195) **verbatim**. They are the only two passages already written in the register a non-technical reader needs
- [x] 4.3 Delete "This file is the reference" and put the link to the site in its place, near the top
- [x] 4.4 Do not carry the Wayland-hotkey paragraph (current 15-17) into the new README. Three unexplained terms in the third paragraph a new reader meets is the single worst thing about the current file
- [x] 4.5 List five requirement bullets, not four: Fedora, KDE Plasma, Python 3.12+, a terminal, **and** the X11-verified/Wayland-unverified status
- [x] 4.6 Keep in Install: `./setup.sh install Meta+X`, `./setup.sh upgrade`, `./setup.sh uninstall`, `uv run murmly doctor`, and the `--yes` with `--purge` warning as one sentence. Send the two-hotkey form and `./setup.sh hooks` to the install page
- [x] 4.7 Keep the `---` frontmatter at lines 1-4
- [x] 4.8 Keep Development at six lines: `uv run --no-sync python -m unittest discover -s tests`, half a line on why `--no-sync` is required, one sentence on OpenSpec, one link. Everything else in the current 36 lines goes
- [x] 4.9 Confirm `README.md` contains no configuration reference, no protocol, no socket rules and no troubleshooting steps, and that no link in it addresses a heading anchor inside itself
- [x] 4.10 Change the one line in `site/index.html`: the README link becomes a relative link to `manual/`. Change nothing else on that page
- [x] 4.11 Note in the PR that `pyproject.toml` sets `readme = "README.md"`, so the package long description shrinks with it

## 5. Nothing is lost — content survival checklist

Verify each item appears on the manual page named in the section map, unaltered. Tables and measured figures keep the sentence naming the machine they were measured on.

- [x] 5.1 Tables: what installation writes (176-180) with the "nothing else" promise (182-183); the speech model files (256-259); synthesis memory by device (275-278) with its qualification at 272-273; the five-row upgrade matrix (298-304)
- [x] 5.2 Tables: what an announcement sounds like (359-364); the four announcement environment variables (382-387); the two hotkeys (442-445)
- [x] 5.3 Tables: frames a sender may send (486-490) including the 65536-byte limit; frames Murmly sends (494-501) including `playing` null and `name` null
- [x] 5.4 Tables: partial-pass ceilings (668-673) with the 15-second-window qualification at 666; idle-release returns and costs (728-732) with its qualification at 726; per-session verification and clipboard preservation (827-832)
- [x] 5.5 Commands, byte-for-byte: the ydotool udev rule and its three lines (56-59); the overlay `dnf` lines (71-72); every `uv` and `setup.sh` invocation at 91, 99-103, 126-127, 134, 151-152, 162, 201, 208, 217-218, 242-243, 249, 346-349
- [x] 5.6 The ONNX Runtime swap (316-320) with the pin `onnxruntime-gpu==1.24.4` unrounded and undropped
- [x] 5.7 The `AGENTS.md` instruction block (403-415) reproduced exactly, including the sentence about leaving the element empty. A reader copies this into their own file
- [x] 5.8 Announcement paths and files (427-438): the chime WAV path, `pw-play`/`paplay`/`aplay`, that the chime does not use `[tts] output_device`, `~/.claude/settings.json` under `Stop` and `SessionStart`, `settings.json.murmly-backup`, `~/.copilot/hooks/murmly-announce.json`, and the assurance that nothing is written into `CLAUDE.md`
- [x] 5.9 Protocol detail: the declaration and acknowledgement JSON (464-468); every refusal code (`speech_disabled`, `speech_unavailable`, `speech_session_in_use`, `command_failed`, `over_capacity`, `shutting_down`, `busy`, `malformed_request`, `unsupported_command`); the rule that any `"ok": false` frame is a refusal; the 65536-byte frame limit and the 64-frame backpressure disconnect
- [x] 5.10 Every configuration key from 558-603 at its default with its documented range. The range comments are the only place the bounds are written down in prose
- [x] 5.11 Configuration file locations and the fall-back-rather-than-refuse rule (548-556), and the restart command (794-795)
- [x] 5.12 Socket security rules (610-641): `0600` socket, `0700` directories, cross-account refusal, the whole-path permission rule, the symlink consequence, the sticky-bit carve-out with the `/tmp/murmly-yours` worked example, and the `chmod go-w` remedy
- [x] 5.13 Profile mapping (645-647) and `auto` resolution with the 1.8 GB / 1.6 GB figures and the pinned model revision (649-654)
- [x] 5.14 Live transcription guarantees (658-662, 680-684) and auto-transcribe behaviour (691-712) including the bolded "delivers without printing"
- [x] 5.15 Idle-release defaults and the upgrade-behaviour warning (734-745, 779-790), and the `model_resident: null` example with the rule that `null` means unanswerable, not idle (760-773)
- [x] 5.16 Delivery honesty rules (38-45, 838-843) and the `"delivered": false` JSON example (810-818) with the note about `murmly spike --paste`
- [x] 5.17 Every `murmly doctor` field name a user is told to look up: `paste_injection`, `paste_injection.confirms_delivery`, `speech_output` with `available`/`detail`/`output_device_in_use`, `command_socket`, `delivery`, `partial_pass_ceiling_ms`, `live_transcription.partial_pass_loaded_model`, `model_resident`, `model_resident_detail`, `speech_output.resident`, `resident_detail`, `installation.hotkey_held`, `installation.hotkey_holder`, `installation.hotkeys` with `purpose`/`hotkey`/`held`/`holder`
- [x] 5.18 Hotkey grammar (167-171) including `Meta+V` as Plasma's clipboard history; overlay dimensions and behaviour (865-874); the service log commands (896-898); the overlay `--check` invocations (919-924); scope caveats (526-539); announcement behaviour guarantees (366-380); the 340 MB model figure (251) and the synthesis speed figures (282-286)
- [x] 5.19 Contributor commands (935-955) — `murmly daemon`, `toggle`, `toggle-session`, `status`, `spike --seconds 5`, venv activation, and the `--no-sync` reasoning. These leave the site; confirm they are not simply deleted

## 6. Verification against the spec

- [x] 6.1 Load every page with network requests recorded. Every request goes to the publishing origin — check CSS-embedded references, not just `href`/`src`
- [x] 6.2 Exercise every interactive control on a documentation page, then inspect cookies and localStorage. Nothing written. Grep the inline `<head>` script as well as the bundle: `__md_set` is defined in the head, so grepping the bundle alone cannot see a localStorage write
- [x] 6.3 Load every page with scripting disabled: all content present, navigation works, no search control visible
- [x] 6.4 View every page at 360 px, with scripting on and again with it off. The body never scrolls sideways; wide tables and code blocks scroll inside themselves in both states
- [x] 6.5 Serve the assembled `_pages/` with `python -m http.server` and no external network. Every page renders with logo, screenshots, diagrams, navigation and typography intact
- [x] 6.6 Tab through `settings` and `speed-and-memory` — the two densest pages — with the keyboard alone. Every control, including every generated navigation control, takes focus in reading order with a visible indicator
- [x] 6.7 Measure contrast on those two pages under both colour schemes, including code and its syntax highlighting
- [ ] 6.8 Traverse two pages with a screen reader: informative images announce, decorative ones are skipped, headings form a navigable structure
- [ ] 6.9 Paste a documentation page URL into a link-preview renderer and confirm the card shows Murmly's own image, title and description
- [x] 6.10 Follow every cross-link between documentation pages on the published site under the `/murmly/` prefix. `strict: true` covers internal links at build time; add a `lychee` run or a manual pass for external links, since nothing checks those
- [x] 6.11 Search the published site for text that appears only under `docs/agent-notes/`, including in the search index
- [x] 6.12 Build twice from the same commit and confirm the outputs match
- [ ] 6.13 Push a commit that breaks the build and confirm the previously published pages continue to serve

## 7. Notes and follow-up

- [x] 7.1 Write `docs/agent-notes/building-the-manual.md`: preview with `uvx --from "mkdocs-material==<pinned>" mkdocs serve`, never a bare `uv run mkdocs serve` (it syncs first and reinstalls the CPU `onnxruntime` over a GPU swap) and never `uv sync --only-group docs` locally (uv prunes and strips the runtime out of `.venv`); `_pages/` is assembled, not committed; the four forbidden `mkdocs.yml` keys; and that the landing page can no longer be reviewed over `file://`
- [x] 7.2 Record in that note that `python -m http.server` serves at the host root, so `404.html`'s absolute references are only verifiable against the published prefix
- [x] 7.3 After archive, edit `openspec/specs/project-website/spec.md`'s `## Purpose` by hand — it is written in the singular ("the public page that introduces Murmly") and no delta mechanism exists to change it

## 8. What was not verified, and why

Three checks in section 6 need something this repository cannot provide. They are
left unticked rather than assumed, so the record says what was actually done.

- **6.8** needs a person with a screen reader. The heading structure, the image
  alternatives and the keyboard order were all checked mechanically; how it
  actually sounds was not.
- **6.9** needs a documentation URL pasted into a third-party link-preview
  renderer. The Open Graph tags such a renderer would read were verified in the
  built output and on the published page, and the image they name returns 200.
- **6.13** needs a commit that breaks the build pushed to the default branch.
  Publication is guarded so that a failed build never reaches the deploy step,
  and every guard was shown to fail on the defect it exists to catch, but the
  end-to-end behaviour was not exercised against the live site.

**1.18 deviates from what it says.** It asks for a deploy carrying only
`manual/index.md`. The change landed as one merge, so the deploy carried the
whole manual. What the task exists to check was done against the published site:
both addresses were opened in a browser and every network request read. All of
them go to the publishing origin, no page writes to storage, and `404.html`
resolves its absolute references under the prefix, which is the one thing a
local server cannot show.
