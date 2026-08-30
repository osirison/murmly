## ADDED Requirements

### Requirement: Murmly's reference documentation is published as pages of this site

The site SHALL publish, as pages of its own, every piece of reference material a person needs after a first install: what Murmly requires and how to install it, first use and the recording overlay, changing or removing a hotkey, where a transcript goes and what happens to the clipboard, live transcription, ending a recording on silence, speech output, announcing a finished agent turn, the configuration reference, what Murmly holds in memory and how fast it is, troubleshooting, and the speech-session protocol together with the command socket's permission rules. Each page SHALL be authored as Markdown held in this repository and SHALL reach the published site through the generator rather than by hand-editing generated output.

No reference material `README.md` carries before this change SHALL be lost in the move. For each subject above the published site MUST state at least what `README.md` stated, and every measured figure, table, exact command, configuration key, default, documented range, diagnostic field name, refusal code, and protocol frame MUST survive with its meaning unchanged. Text a reader is expected to copy — an instruction to place in their own file, a udev rule, a pinned version — MUST be reproduced exactly rather than paraphrased.

Editing a published documentation page MUST be done by editing its Markdown source. Generated output MUST NOT be hand-edited, because an edit made there is overwritten by the next build without warning.

#### Scenario: The published site is examined after the move

- **WHEN** the published site is examined after the move
- **THEN** a page exists for each of installation and requirements, first use, hotkey management, transcript delivery, live transcription, ending a recording on silence, speech output, agent-turn announcements, the configuration reference, memory and speed, troubleshooting, and the developer reference
- **AND** each is reachable from the site's documentation index

#### Scenario: A configuration key is looked up

- **WHEN** a configuration key that `README.md` documented before the move is looked up on the published site
- **THEN** it is documented there with its meaning, its default, and its documented range

#### Scenario: Text the reader must copy

- **WHEN** the exact instruction text a person must place in their own file for an agent that cannot be told automatically is looked for
- **THEN** it appears in full on a documentation page and can be copied from it unaltered

#### Scenario: A measured figure is carried across

- **WHEN** a documentation page states a figure that `README.md` stated before the move
- **THEN** the figure is unchanged and carries the same qualification, including the machine it was measured on

#### Scenario: A documentation page is changed

- **WHEN** a documentation page is changed
- **THEN** the change was made in a Markdown source file committed to this repository
- **AND** no file under the generator's output directory was hand-edited

#### Scenario: The generator's output is looked for in version control

- **WHEN** the generator's output directory is inspected in version control
- **THEN** it is absent from version control

### Requirement: A reader who does not know the vocabulary can find the page they need

Every documentation page SHALL be reachable from the landing page by following visible links, without the reader knowing a URL, a filename, or Murmly's internal vocabulary. Every documentation page SHALL show a persistent, visible index of the documentation, SHALL indicate which page the reader is on, and SHALL link back to the landing page. No documentation page SHALL be a dead end.

Navigation entries and page titles SHALL be phrased in terms of what the reader wants to do rather than in terms of Murmly's internal components, so that a reader who has never heard the words "daemon", "compositor", or "session protocol" can still choose correctly. The documentation SHALL be ordered so that installing and first use come before the configuration reference and troubleshooting, and the developer reference comes last.

The index SHALL state, for each heading that `README.md` carried before this change and no longer carries, which page now holds that subject, so a reader arriving from a link to a heading that no longer exists can find where it went.

#### Scenario: A visitor looks for how to change the recording key

- **GIVEN** a visitor on the landing page who has never used Murmly
- **WHEN** they look for how to change the key that starts recording
- **THEN** they reach the hotkey documentation page by following visible links only, without typing a URL

#### Scenario: A documentation page is loaded

- **WHEN** any documentation page is loaded
- **THEN** it shows an index of the documentation, marks the current page within that index, and links to the landing page

#### Scenario: The navigation entries are read

- **WHEN** the navigation entries are read
- **THEN** each names a task or a subject a user would recognise
- **AND** none is titled only with an internal component name

#### Scenario: The documentation index is read from the top

- **WHEN** the documentation index is read from the top
- **THEN** installation and first use precede the configuration reference and troubleshooting
- **AND** the developer reference comes last

#### Scenario: A reader arrives on a documentation page from a search engine

- **GIVEN** a reader who followed a link to a documentation page directly rather than through the landing page
- **WHEN** that page is loaded
- **THEN** they can determine from the page alone what Murmly is, and reach the landing page and the documentation index from it

#### Scenario: A link to a heading that no longer exists is followed

- **GIVEN** a link to a `README.md` heading that this change removed
- **WHEN** the reader reaches the documentation index
- **THEN** the index names the page that now holds that subject

### Requirement: README.md is a short entry point rather than a reference manual

`README.md` SHALL state in plain language what Murmly is, state the requirements a reader must meet before installing — including that the X11 session is verified end to end while Plasma Wayland is not — give the command that installs it, show what using it looks like, and link to the published site as the place all further documentation lives. The link to the site SHALL appear near the top and SHALL address the published site rather than a file path in the repository.

`README.md` MUST NOT carry the configuration reference, the speech-session protocol, the command socket's permission rules, troubleshooting steps, or any other reference material the documentation pages hold, because a second copy drifts from the first. It SHALL nonetheless remain complete enough that a reader who never leaves the repository can install Murmly and produce one transcript.

#### Scenario: README.md is read from top to bottom

- **WHEN** `README.md` is read from top to bottom
- **THEN** it states what Murmly is, states the requirements, gives the install command, shows a first use, and links to the published site

#### Scenario: README.md is searched for reference material

- **WHEN** `README.md` is searched for a configuration option, the speech-session protocol, or a troubleshooting step
- **THEN** it links to the site's documentation instead of reproducing it

#### Scenario: A reader follows README.md without opening the site

- **GIVEN** a reader who follows `README.md` without opening the site
- **WHEN** they follow it to the end
- **THEN** they have installed Murmly and produced one transcript

#### Scenario: README.md is measured after the move

- **WHEN** `README.md` is measured after the move
- **THEN** it is a small fraction of the 966 lines it held before
- **AND** no section of reference material that moved to the site remains duplicated in it

#### Scenario: The link to the site is followed

- **WHEN** the link to the site in `README.md` is followed
- **THEN** it resolves to `https://osirison.github.io/murmly/`

### Requirement: Internal notes are excluded from everything the site publishes

`docs/agent-notes/` holds internal operational notes and SHALL NOT be published. The set of sources the generator reads SHALL be defined so that `docs/agent-notes/` lies outside it, and that exclusion SHALL be explicit in the generator's configuration rather than incidental to it — a note added later MUST NOT become published merely because it was placed in a directory the generator happened to be pointed at.

No file under `docs/agent-notes/` SHALL appear in the generator's output, in a search index, in a sitemap, or in any other artifact uploaded for publication, and no text SHALL be reproduced from one verbatim. A maintainer MAY write up, in their own words on a documentation page, a fix they learned from an internal note; what is forbidden is publishing the note.

#### Scenario: The built output is listed

- **WHEN** the site is built and every file in its output is listed
- **THEN** no file originates in `docs/agent-notes/` and no file reproduces text from one verbatim

#### Scenario: The published site is searched for internal text

- **WHEN** the published site is searched for text that appears only in a file under `docs/agent-notes/`
- **THEN** nothing is found, including in any search index the site serves

#### Scenario: A new internal note is added

- **GIVEN** a new file added to `docs/agent-notes/`
- **WHEN** the site is built and published
- **THEN** no new page appears and the published output is unchanged apart from anything else in that commit

#### Scenario: The generator's configuration is read

- **WHEN** the generator's configuration is read
- **THEN** the boundary of its source set is stated explicitly and `docs/agent-notes/` lies outside it

#### Scenario: A documentation page carries a fix learned from an internal note

- **WHEN** a documentation page describes a fix that an internal note also describes
- **THEN** it is written in the documentation's own words rather than reproduced from the note

### Requirement: The build between the sources and the published bytes is pinned and fails closed

The published site is generated rather than committed byte-for-byte, so what a reviewer approves is the Markdown sources, the templates, and the generator's configuration rather than the bytes that reach the visitor. The generator, its theme, and every plugin the build depends on SHALL be pinned to exact versions recorded in a file committed to this repository, so the same sources produce the same site until a version is deliberately changed.

Every byte the site publishes SHALL derive either from a committed source in this repository or from that pinned toolchain. No published page MAY contain content fetched from a third-party host at build time or at view time. Every file in the published artifact that lies outside the generated documentation tree SHALL be byte-identical to its committed source, and that identity SHALL be asserted by the publishing process rather than assumed.

A build that fails SHALL leave the published site as it was: a failed build MUST NOT publish a partial or empty site. The build SHALL be runnable on a change before it reaches the default branch, so a reviewer can see the generated pages without publishing them. Publication SHALL continue to neither run nor wait on Murmly's Python test matrix, and a failure in that matrix MUST NOT prevent a documentation correction from publishing.

#### Scenario: The pinned toolchain is inspected

- **WHEN** the repository is inspected
- **THEN** a committed file records the exact version of the generator, its theme, and every plugin the build uses

#### Scenario: The same sources are built twice

- **GIVEN** the committed sources at one commit
- **WHEN** the site is built twice from them
- **THEN** the two outputs are the same

#### Scenario: A published page is traced to its source

- **WHEN** any published page is traced back
- **THEN** every part of it derives from a committed source in this repository or from the pinned toolchain
- **AND** no part was fetched from a third-party host

#### Scenario: The artifact is assembled

- **WHEN** the artifact for publication is assembled
- **THEN** every file it carries outside the generated documentation tree is byte-identical to its committed source
- **AND** a difference stops publication rather than being published

#### Scenario: A commit breaks the build

- **GIVEN** a published site
- **WHEN** a commit reaches the default branch whose sources cause the build to fail
- **THEN** publication does not replace the published site and the previously published pages continue to serve

#### Scenario: A pull request changes a documentation source

- **WHEN** a pull request changes a documentation source
- **THEN** the site can be built from that pull request and the result inspected
- **AND** the published site is unchanged until the change reaches the default branch

#### Scenario: Publication remains independent of the test suite

- **WHEN** publication runs
- **THEN** it neither runs nor waits on Murmly's Python test matrix
- **AND** a failure in that matrix does not prevent a documentation correction from publishing

## MODIFIED Requirements

### Requirement: The project page is published at a stable public address

Murmly SHALL publish a site at `https://osirison.github.io/murmly/`, whose root address serves the landing page and which additionally serves the documentation pages at stable addresses beneath that prefix. Publication MUST run automatically when the default branch changes any input that determines the published bytes — the documentation's Markdown sources, the generator's configuration and its pinned versions, the site's templates, styles and assets, and the publishing workflow itself — and MUST also be startable on demand. A proposed change that has not reached the default branch MUST NOT replace the published site. A documentation page's address SHALL NOT change when unrelated pages are added.

#### Scenario: A site change reaches the default branch

- **WHEN** a commit changing any input that determines the published bytes is pushed to the default branch
- **THEN** the site at `https://osirison.github.io/murmly/` is republished from that commit without any further manual step

#### Scenario: Only a documentation source changes

- **WHEN** a commit that edits only a documentation Markdown source is pushed to the default branch
- **THEN** publication runs
- **AND** the corresponding documentation page on the published site shows the edit

#### Scenario: A site change is proposed but not merged

- **WHEN** a pull request changing any site or documentation input is opened, updated, or closed without merging
- **THEN** the published site is unchanged

#### Scenario: Republishing without a code change

- **WHEN** a maintainer starts publication manually
- **THEN** the site is republished from the current default branch

#### Scenario: Publication is independent of the test suite

- **WHEN** publication runs
- **THEN** it neither runs nor waits on Murmly's Python test matrix
- **AND** a failure in that matrix does not prevent a page correction from publishing

#### Scenario: A documentation page address is quoted elsewhere

- **GIVEN** a documentation page address that has been published
- **WHEN** further documentation pages are added and the site is republished
- **THEN** that address still resolves to the same page

### Requirement: The first screen says what Murmly is and what using it looks like

The site's landing page — the page served at the site's root address — SHALL, without scrolling on a 1280×800 viewport, present Murmly's name, a single sentence describing what it does in the user's own terms, a visual depiction of the press-speak-press loop, and a link to installation. That first screen MUST NOT open with an explanation of Wayland, compositors, hotkey protocols, or any other implementation subject. This requirement governs the landing page only; a documentation page is not required to open this way.

#### Scenario: A first-time visitor opens the page

- **WHEN** the site's root address is loaded at a 1280×800 viewport and nothing is scrolled
- **THEN** the project name, a one-sentence description, a depiction of the record-transcribe-paste loop, and a link to installation are all visible

#### Scenario: The opening text is checked for implementation detail

- **WHEN** the text visible on the landing page's first screen is read
- **THEN** it names no compositor, protocol, window system, or library

### Requirement: The page discloses what Murmly requires before it asks for an install

Wherever the site presents instructions for installing Murmly, that same page SHALL state, without requiring the visitor to follow a link away from it, that Murmly targets Fedora, that hotkey registration and the recording overlay require KDE Plasma, that Python 3.12 or newer is required, that installation is run from a terminal, and that the X11 session is verified end to end while Plasma Wayland is not. Presenting Murmly as broadly consumer-ready MUST NOT come at the cost of omitting any of these on any page that invites an install.

#### Scenario: A visitor reads the install section

- **WHEN** install instructions are read on any page of the site
- **THEN** the Fedora target, the Plasma requirement, the Python version floor, the terminal requirement, and the X11-verified/Wayland-unverified status are each stated on that same page

#### Scenario: A visitor on an unsupported desktop

- **WHEN** someone running a desktop other than KDE Plasma reads any page that invites them to install
- **THEN** they can determine from that page alone that the hotkey will not register itself on their desktop

#### Scenario: An install command appears on a documentation page

- **WHEN** a documentation page shows an install command
- **THEN** the five disclosures appear on that page as well, not only on the landing page

### Requirement: Every capability claim on the page is traceable to this repository

No page of the site SHALL make a factual claim about Murmly's behavior, performance, or resource use that is not traceable to this repository's specifications under `openspec/specs/`, to a measurement recorded in this repository, or to the source itself. A documentation page MUST NOT be accepted as the source for its own claim, because the documentation now lives on the site and a figure invented there would otherwise trace to itself. Measured figures MUST be reproduced with the same qualification they carry at their source, including the hardware they were measured on. No page MAY present a comparison against a named competitor, a benchmark that exists nowhere else, a testimonial, a user count, or a rating.

#### Scenario: A performance figure appears on the page

- **WHEN** any page of the site states a latency, a throughput, or a memory figure
- **THEN** the same figure is traceable to a specification under `openspec/specs/` or to a measurement recorded in this repository outside the documentation
- **AND** the page repeats the qualification given there, including the machine it was measured on

#### Scenario: A claim has no source in the repository

- **WHEN** a proposed claim on any page cannot be traced outside the documentation itself
- **THEN** it is not published

#### Scenario: Social proof is proposed

- **WHEN** a testimonial, install count, star count, star-history chart, or rating is proposed for any page of the site
- **THEN** it is not published

### Requirement: The page loads entirely from its own origin

Every page of the site SHALL request no resource from any host other than the one serving it. Fonts, stylesheets, images, icons, search indexes, and any script MUST be served from the site itself or embedded in the document. No page MAY load analytics, tracking pixels, embedded third-party media, or an external font service, and no page MAY set a cookie or write to persistent browser storage. This constrains the generator's output and anything a theme or a plugin contributes to it, not only hand-written markup — a theme feature that persists a preference, or a header widget that queries a code-hosting API at view time, is forbidden by this requirement however convenient it is.

#### Scenario: The page is loaded with network requests recorded

- **WHEN** every page of the site and every asset any of them references are loaded
- **THEN** every request goes to the origin serving the site

#### Scenario: The generator's output is checked before the generator is adopted

- **WHEN** the site is built with a candidate generator and theme and the output is searched for references to an external host
- **THEN** no output file names a host other than the one serving the site
- **AND** a theme that emits one is rejected, or its emission removed, before that generator is adopted

#### Scenario: The page is loaded offline after a first visit

- **WHEN** the assembled site is served from a local server with no external network available
- **THEN** every page renders with its logo, screenshots, diagrams, navigation, and typography intact
- **AND** no request leaves that server

#### Scenario: Storage is inspected after a visit

- **WHEN** browser storage is inspected after loading the landing page and after exercising every interactive control on a documentation page
- **THEN** neither has set a cookie nor written a persistent entry

### Requirement: The page resolves correctly under a project path prefix

The published site is served from a path prefix rather than a domain root. Every internal link and asset reference on every page, including links between documentation pages and anything the generator or its theme emits, SHALL resolve correctly under that prefix. The generator's configuration SHALL declare the prefix so generated references are correct rather than corrected afterwards. No page MAY depend on being served from the root of a host.

#### Scenario: The published page is opened

- **WHEN** `https://osirison.github.io/murmly/` and every documentation page beneath it are loaded
- **THEN** every stylesheet, image, icon, and internal link resolves and no reference returns 404

#### Scenario: A documentation page links to another

- **WHEN** a link from one documentation page to another is followed on the published site
- **THEN** it resolves under the project prefix without returning 404

#### Scenario: An asset reference is written from the host root

- **WHEN** the built output is searched for a reference beginning at the host root
- **THEN** no such reference remains except where the published prefix is part of it, because a bare host-root reference resolves outside the project's prefix

### Requirement: The page is complete without JavaScript and legible on a phone

All of every page's content SHALL be present and readable with JavaScript disabled. Any script is enhancement only, and its absence MUST NOT remove content, navigation, an install command, or the ability to reach any documentation page from any other. Navigation between pages SHALL be plain links that work with scripting disabled. Search is the one feature that MAY require a script; where it does, its control MUST NOT remain visible when it cannot function, so a reader is never offered a search box that does nothing.

Every page SHALL remain readable from a 360 px viewport upward, with no horizontal scrolling of the document body, and navigation MUST remain usable at that width without a script. Content wider than the viewport MUST scroll within its own region, and that MUST hold with scripting disabled as well as enabled — a scroll container created by a script is not sufficient.

#### Scenario: JavaScript is disabled

- **WHEN** any page of the site is loaded with scripting disabled
- **THEN** every section, image, link, and install command is present and readable

#### Scenario: Navigating the documentation without scripting

- **WHEN** a visitor with scripting disabled starts at the landing page
- **THEN** they can reach every documentation page by following links alone

#### Scenario: A feature that needs a script is unavailable

- **WHEN** a page is loaded with scripting disabled
- **THEN** no control is shown for a feature that cannot function without a script

#### Scenario: The page is opened on a narrow viewport

- **WHEN** any page is viewed at 360 px wide, with scripting enabled and again with it disabled
- **THEN** the document body does not scroll horizontally in either case
- **AND** the navigation to other documentation pages is reachable in either case
- **AND** any wide element, such as a code block or a table, scrolls within its own region in either case

### Requirement: Screenshots depict the interface Murmly actually ships

On every page of the site, images presented as screenshots SHALL be captures of Murmly running, showing the interface as it is currently specified. An illustration that is not a capture MUST be visually distinguishable from a capture and MUST NOT be captioned as one. The landing page SHALL show the recording overlay in its listening state, in its processing state, and with a live partial transcript visible.

#### Scenario: A screenshot is placed on the page

- **WHEN** an image on any page of the site is presented as a screenshot
- **THEN** it is a capture of Murmly running rather than a mockup or a rendering

#### Scenario: A state cannot be captured

- **WHEN** an interface state cannot be captured and is drawn instead
- **THEN** the drawing is presented as an illustration and is not described as a screenshot

#### Scenario: The overlay's states are shown

- **WHEN** the landing page's imagery is reviewed
- **THEN** the listening state, the processing state, and a live partial transcript each appear

### Requirement: The project mark is original and carried by the repository

The site's logo, wordmark, favicon, and social-preview image SHALL be authored in this repository and distributable under its license. No page MAY use a third-party mark, stock illustration, or icon set that the repository does not carry and cannot relicense, and this applies to anything a generator theme contributes as much as to hand-written markup: an icon set that arrives with a theme is carried by the site as surely as one committed by hand, and either the repository carries its license or the icons do not ship. The mark SHALL be legible at 16 px and SHALL render on both a light and a dark background. Every page SHALL carry the project's own mark and favicon, and no page MAY present the generator's or the theme's default mark as Murmly's.

#### Scenario: The mark is rendered small

- **WHEN** the logo is rendered at 16 px as a favicon
- **THEN** its silhouette remains identifiable

#### Scenario: The page is viewed in either colour scheme

- **WHEN** any page of the site is viewed under a light colour scheme and under a dark one
- **THEN** the logo and wordmark are legible in both

#### Scenario: The page is shared as a link

- **WHEN** the address of the landing page or of any documentation page is pasted into a service that renders link previews
- **THEN** the preview shows the project's own image, title, and description, served from this origin

#### Scenario: A theme contributes an icon set

- **WHEN** the built output is inspected for images, icons, and fonts, including those embedded in stylesheets
- **THEN** every one is authored in this repository, or is carried by the repository under a license permitting redistribution with that license text committed alongside it

#### Scenario: A theme ships its own favicon

- **WHEN** the artifact uploaded for publication is inspected
- **THEN** it carries no mark or favicon belonging to the generator or its theme

### Requirement: The page is usable without sight, without a mouse, and without motion

On every page of the site, every image SHALL carry a text alternative that conveys what the image shows, or be marked as decorative when it carries no information. Text SHALL meet a contrast ratio of at least 4.5:1 against its background, and at least 3:1 for text at 24 px or larger, under both colour schemes and including any syntax highlighting the generator applies. Every interactive element, including every navigation control the generator or its theme emits, SHALL be reachable by keyboard and SHALL show a visible focus indicator. Every page SHALL expose a heading structure a screen reader can navigate by. Any animation SHALL be suppressed when the visitor has asked for reduced motion.

#### Scenario: The page is read by a screen reader

- **WHEN** any page of the site is traversed by a screen reader
- **THEN** every informative image announces what it shows
- **AND** decorative images are skipped rather than announced
- **AND** the page's headings form a structure that can be navigated by heading level

#### Scenario: The page is navigated by keyboard

- **WHEN** focus is advanced through any page with the keyboard alone
- **THEN** every link and control, including every generated navigation control, receives focus in reading order with a visible indicator

#### Scenario: Contrast is measured under both colour schemes

- **WHEN** the text on any page is measured against its background under a light colour scheme and under a dark one
- **THEN** every measurement meets the ratio required for its size, including code and its syntax highlighting

#### Scenario: The visitor has asked for reduced motion

- **WHEN** any page of the site is loaded by a visitor whose system requests reduced motion
- **THEN** no element animates, and any content conveyed by animation is still conveyed

### Requirement: The page routes to installation rather than replacing the documentation

The landing page SHALL show the command that installs Murmly and SHALL link to the site's own documentation pages for everything beyond a first install. Reference material — the configuration reference, the speech-session protocol, hotkey management, transcript delivery, the recording overlay, and troubleshooting — SHALL exist in exactly one place, the site's documentation pages built from this repository's Markdown sources. The landing page MUST NOT restate that material, because a second copy drifts from the first. A link to reference material, wherever it is written, SHALL address a documentation page on this site rather than a heading anchor in `README.md`.

#### Scenario: A visitor decides to install

- **WHEN** the install section of the landing page is read
- **THEN** it shows the install command
- **AND** it links to the site's installation documentation rather than to the repository's README

#### Scenario: A visitor looks for a configuration option

- **WHEN** the landing page is searched for a configuration reference
- **THEN** it links to the site's configuration documentation instead of reproducing it

#### Scenario: Reference material is checked for a second copy

- **WHEN** the configuration reference, the speech-session protocol, and the troubleshooting steps are searched for across `README.md`, the landing page, and the documentation pages
- **THEN** each appears in full on exactly one documentation page
- **AND** neither `README.md` nor the landing page reproduces it

#### Scenario: A documentation link is written

- **WHEN** a link on the site or in `README.md` points at reference material
- **THEN** it addresses a documentation page on this site
- **AND** no link addresses a heading anchor inside `README.md`
