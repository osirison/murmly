## 1. Publishing pipeline

- [ ] 1.1 Confirm with the repository owner that Settings → Pages → Source is set to *GitHub Actions*. Nothing else in this change can be verified end to end until it is, and a workflow cannot set it for itself
- [ ] 1.2 Create `site/` with a placeholder `index.html` so the pipeline can be proven before the real page exists
- [ ] 1.3 Add `.github/workflows/pages.yml`: triggers `push` to `main` and `workflow_dispatch`; permissions `contents: read`, `pages: write`, `id-token: write`; a `concurrency` group named for Pages with `cancel-in-progress: false` so two pushes deploy in order; steps `actions/checkout`, `actions/configure-pages`, `actions/upload-pages-artifact` with `path: site/`, then `actions/deploy-pages` in a job with the `github-pages` environment
- [ ] 1.4 Confirm the workflow has no build step and installs nothing — the published bytes are the committed bytes
- [ ] 1.5 Confirm `tests.yml` is untouched and that neither workflow triggers or waits on the other

## 2. Identity

- [ ] 2.1 Author `site/assets/murmly-mark.svg`: a rounded speech form containing three waveform bars, the rightmost drawn at text-caret proportions. Single path set, no embedded raster, no external reference
- [ ] 2.2 Draw the mark with `fill="currentColor"` and no hard-coded colour, so one file serves the light header, the dark header, and a monochrome icon
- [ ] 2.3 Check the mark at 16 px, 32 px, and 180 px. If the bars merge at 16 px, thicken the strokes rather than shipping a second simplified file
- [ ] 2.4 Author `site/assets/murmly-wordmark.svg`: "murmly" lowercase, converted to outlines so no font file is required to render it
- [ ] 2.5 Derive `favicon.svg`, `favicon.ico` (16/32/48), and `apple-touch-icon.png` (180×180) from the mark
- [ ] 2.6 Render `site/assets/social-preview.png` at 1200×630 from the mark plus the wordmark and the one-sentence description, on a solid background. Confirm the text is legible at the ~500 px width a feed actually shows

## 3. Screenshots

- [ ] 3.1 Read `docs/agent-notes/wayland-overlay-preload-and-paste.md` before capturing, then start an **X11 Plasma session** — on Wayland the overlay is a layer-shell surface that a region capture may omit, and X11 is the session Murmly is verified on
- [ ] 3.2 Prepare a neutral desktop: no personal content, no identifiable filenames, no notification badges in frame
- [ ] 3.3 Capture `overlay-listening.png` — the overlay with the microphone symbol and the waveform responding to speech
- [ ] 3.4 Capture `overlay-partial.png` — the same with `stt.live_transcribe = true` and a partial transcript visible in the panel. Dictate a sentence about Murmly itself, not placeholder text
- [ ] 3.5 Capture `overlay-processing.png` — the processing presentation after capture stops
- [ ] 3.6 Capture `doctor.png` — `murmly doctor` in a terminal, with any machine-identifying path redacted by re-running under a neutral home rather than by painting over the image
- [ ] 3.7 Optimize every PNG losslessly, target under ~150 KB each, and record each image's pixel dimensions for the `width`/`height` attributes
- [ ] 3.8 Confirm total `site/assets/` weight is under 1 MB

## 4. Diagrams

- [ ] 4.1 Author `diagram-loop.svg`: three panels — press the hotkey, speak, press again and the text appears in the window you were in. This is the first screen's graphic and it replaces the paragraph the README opens with
- [ ] 4.2 Author `diagram-local.svg`: a boundary around the machine holding the microphone, the model, and the target window, with nothing crossing outward, and exactly one inward arrow labelled "first run: model download". Omitting that arrow would overstate the local-only claim
- [ ] 4.3 Keep both files in `site/assets/` as the editable source, and inline their markup into `index.html` rather than referencing them as `<img>`, so they inherit `currentColor` and follow the colour scheme
- [ ] 4.4 Give each inline SVG a `<title>` and `role="img"` with `aria-labelledby`, so the diagram announces what it shows

## 5. The page

- [ ] 5.1 Write `site/index.html` in the section order from `design.md` — hero, how it works, what makes it different, it also speaks, install, footer
- [ ] 5.2 Hero: mark, wordmark, the one-sentence description, the loop diagram, the install link, and a compact "Fedora · KDE Plasma · Python 3.12+" line. Verify at 1280×800 that all of it is above the fold and that nothing visible there names a compositor, protocol, window system, or library
- [ ] 5.3 "How it works": three steps, each paired with its real screenshot
- [ ] 5.4 "What makes it different": four points drawn from the claim table in `design.md`, one sentence and one supporting detail each. The local-only point carries `diagram-local.svg`
- [ ] 5.5 "It also speaks": speech output and the agent-announce hook, stated as optional and off by default
- [ ] 5.6 "Install": the requirements panel — Fedora target, Plasma for hotkey and overlay, Python 3.12+, terminal install, X11 verified and Plasma Wayland not verified end to end — then the install command, then the link to the README. Style the panel as part of the product, not as a disclaimer
- [ ] 5.7 Footer: Apache-2.0, the repository link, and a link to `openspec/specs/`
- [ ] 5.8 Head: `<title>`, `<meta name="description">`, `<link rel="canonical">` and `og:image` as absolute URLs under `https://osirison.github.io/murmly/`, `og:title`/`og:description`/`og:type`, and `twitter:card` set to `summary_large_image`. These two absolute URLs are the only ones in the document
- [ ] 5.9 Write every other `href` and `src` as a relative path, so the page resolves under the `/murmly/` prefix and also when opened from local disk
- [ ] 5.10 Ship no JavaScript. If a copy-to-clipboard control is added later, the command must remain selectable and readable with scripting disabled

## 6. Styling

- [ ] 6.1 Write `site/style.css`: colour, spacing, and radius tokens on `:root`; a `@media (prefers-color-scheme: dark)` block that redefines only the tokens
- [ ] 6.2 Set the type stack to the platform's own UI and monospace faces. Load no font file and reference no font service
- [ ] 6.3 Lay the page out so it reads from 360 px upward with no horizontal scrolling of the document body; give code blocks and any table their own `overflow-x: auto` region
- [ ] 6.4 Give every `<img>` explicit `width` and `height`, and `loading="lazy"` for anything below the first screen, so the page does not reflow as images arrive
- [ ] 6.5 Suppress every transition and animation under `@media (prefers-reduced-motion: reduce)`, and confirm nothing is conveyed by motion alone

## 7. Content review

- [ ] 7.1 Check every factual statement on the page against the claim table in `design.md`, and delete any sentence with no source in `README.md`, `openspec/specs/`, `config.example.toml`, `LICENSE`, or `pyproject.toml`
- [ ] 7.2 Confirm no figure is sourced from `openspec/changes/` — those describe behaviour that is planned, not shipped, and `unload-idle-gpu-models` in particular carries a memory figure for code that does not exist yet
- [ ] 7.3 Confirm each measured figure carries the qualification its source gives it, including the machine it was measured on
- [ ] 7.4 Confirm the page carries no competitor comparison, testimonial, install count, star count, or rating
- [ ] 7.5 Confirm the page reproduces no configuration reference, no speech-session protocol detail, and no troubleshooting steps — those stay in the README

## 8. Documentation

- [ ] 8.1 Add the site link near the top of `README.md`
- [ ] 8.2 Write `docs/agent-notes/` entries for whatever the capture and publish work turned up that is not obvious — at minimum the layer-shell screenshot constraint if it bit, and the Pages source setting being manual
- [ ] 8.3 Set the repository's social preview image to `social-preview.png` and add the site URL to the About field

## 9. Verification

- [ ] 9.1 Open `site/index.html` from local disk with no network and confirm the logo, screenshots, diagrams, and typography all render — this proves both the relative paths and the no-third-party-requests requirement in one pass
- [ ] 9.2 Grep `site/` for `http://` and `https://` and confirm every hit is either a navigation `href` or one of the two absolute URLs in the head. No subresource may point off origin
- [ ] 9.3 After deploy, load `https://osirison.github.io/murmly/` with the network panel recording, and confirm no request leaves the origin, nothing returns 404, no cookie is set, and no persistent storage entry is written
- [ ] 9.4 Load the page with scripting disabled and confirm every section, image, link, and the install command are present and readable
- [ ] 9.5 Check every text and background pair against 4.5:1, and 3:1 for text at 24 px or larger, in both colour schemes
- [ ] 9.6 Tab through the page and confirm every link and control takes focus in reading order with a visible indicator
- [ ] 9.7 Confirm every informative image has a text alternative describing what it shows, and that decorative images are marked so a screen reader skips them
- [ ] 9.8 View the page at 360 px, 768 px, and 1280 px and confirm no horizontal scrolling of the body at any of them
- [ ] 9.9 Load the page under both `prefers-color-scheme` values and confirm the mark and wordmark are legible in each
- [ ] 9.10 Paste the published URL into a link-preview renderer and confirm the title, description, and image come from this origin
- [ ] 9.11 Open a pull request that changes a file under `site/` and confirm the published page does not change until it merges
- [ ] 9.12 Run `workflow_dispatch` and confirm the page republishes from the current `main` without a code change
- [ ] 9.13 Confirm the deploy did not run the Python test matrix
- [ ] 9.14 Run `openspec validate add-project-website --strict` and the existing suite once, to confirm this change left neither of them worse
