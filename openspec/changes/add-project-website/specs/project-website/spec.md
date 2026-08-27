## Purpose

Defines the public page that introduces Murmly to someone who has never seen it: what it must show and in what order, what it is allowed to claim, what it must disclose about the machine Murmly actually needs, where and how it is published, and the constraints that keep it self-contained, readable without JavaScript, and free of any request to a third party.

## ADDED Requirements

### Requirement: The project page is published at a stable public address

Murmly SHALL publish a project page at `https://osirison.github.io/murmly/`. Publication MUST run automatically when the default branch changes and MUST also be startable on demand. A proposed change that has not reached the default branch MUST NOT replace the published page.

#### Scenario: A site change reaches the default branch

- **WHEN** a commit changing the site's files is pushed to the default branch
- **THEN** the page at `https://osirison.github.io/murmly/` is republished from that commit without any further manual step

#### Scenario: A site change is proposed but not merged

- **WHEN** a pull request changing the site's files is opened, updated, or closed without merging
- **THEN** the published page is unchanged

#### Scenario: Republishing without a code change

- **WHEN** a maintainer starts publication manually
- **THEN** the page is republished from the current default branch

#### Scenario: Publication is independent of the test suite

- **WHEN** publication runs
- **THEN** it neither runs nor waits on Murmly's Python test matrix
- **AND** a failure in that matrix does not prevent a page correction from publishing

### Requirement: The first screen says what Murmly is and what using it looks like

The page SHALL, without scrolling on a 1280×800 viewport, present Murmly's name, a single sentence describing what it does in the user's own terms, a visual depiction of the press-speak-press loop, and a link to installation. That first screen MUST NOT open with an explanation of Wayland, compositors, hotkey protocols, or any other implementation subject.

#### Scenario: A first-time visitor opens the page

- **WHEN** the page is loaded at a 1280×800 viewport and nothing is scrolled
- **THEN** the project name, a one-sentence description, a depiction of the record-transcribe-paste loop, and a link to installation are all visible

#### Scenario: The opening text is checked for implementation detail

- **WHEN** the text visible on the first screen is read
- **THEN** it names no compositor, protocol, window system, or library

### Requirement: The page discloses what Murmly requires before it asks for an install

The page SHALL state, in a place a visitor reaches before or alongside the install instructions and without following a link off the page, that Murmly targets Fedora, that hotkey registration and the recording overlay require KDE Plasma, that Python 3.12 or newer is required, that installation is run from a terminal, and that the X11 session is verified end to end while Plasma Wayland is not. Presenting Murmly as broadly consumer-ready MUST NOT come at the cost of omitting any of these.

#### Scenario: A visitor reads the install section

- **WHEN** the install instructions are read on the page
- **THEN** the Fedora target, the Plasma requirement, the Python version floor, the terminal requirement, and the X11-verified/Wayland-unverified status are each stated on the same page

#### Scenario: A visitor on an unsupported desktop

- **WHEN** someone running a desktop other than KDE Plasma reads the page
- **THEN** they can determine from the page alone that the hotkey will not register itself on their desktop

### Requirement: Every capability claim on the page is traceable to this repository

The page SHALL make no factual claim about Murmly's behavior, performance, or resource use that is not already stated in the repository's documentation or specifications. Measured figures MUST be reproduced with the same qualification they carry at their source, including the hardware they were measured on. The page MUST NOT present a comparison against a named competitor, a benchmark that exists nowhere else, a testimonial, a user count, or a rating.

#### Scenario: A performance figure appears on the page

- **WHEN** the page states a latency, a throughput, or a memory figure
- **THEN** the same figure appears in the repository's documentation or specifications
- **AND** the page repeats the qualification given there, including the machine it was measured on

#### Scenario: A claim has no source in the repository

- **WHEN** a proposed claim cannot be traced to the repository
- **THEN** it is not published on the page

#### Scenario: Social proof is proposed

- **WHEN** a testimonial, install count, star count, star-history chart, or rating is proposed for the page
- **THEN** it is not published

### Requirement: The page loads entirely from its own origin

The page SHALL request no resource from any host other than the one serving it. Fonts, stylesheets, images, icons, and any script MUST be served from the site itself or embedded in the document. The page MUST NOT load analytics, tracking pixels, embedded third-party media, or an external font service, and MUST NOT set a cookie or write to persistent browser storage.

#### Scenario: The page is loaded with network requests recorded

- **WHEN** the page and every asset it references are loaded
- **THEN** every request goes to the origin serving the page

#### Scenario: The page is loaded offline after a first visit

- **WHEN** the page's files are opened from local disk with no network available
- **THEN** the page renders with its logo, screenshots, diagrams, and typography intact

#### Scenario: Storage is inspected after a visit

- **WHEN** browser storage is inspected after loading the page
- **THEN** the page has set no cookie and written no persistent entry

### Requirement: The page resolves correctly under a project path prefix

The published page is served from a path prefix rather than a domain root. Every internal link and asset reference SHALL resolve correctly under that prefix. The page MUST NOT depend on being served from the root of a host.

#### Scenario: The published page is opened

- **WHEN** `https://osirison.github.io/murmly/` is loaded
- **THEN** every stylesheet, image, icon, and internal link resolves and no reference returns 404

#### Scenario: An asset reference is written from the host root

- **WHEN** an asset is referenced by a path beginning at the host root
- **THEN** that reference is corrected before publishing, because it resolves outside the project's prefix

### Requirement: The page is complete without JavaScript and legible on a phone

All of the page's content SHALL be present and readable with JavaScript disabled; any script is enhancement only and its absence MUST NOT remove content, navigation, or an install command. The page SHALL remain readable from a 360 px viewport upward, with no horizontal scrolling of the document body. Content wider than the viewport MUST scroll within its own region.

#### Scenario: JavaScript is disabled

- **WHEN** the page is loaded with scripting disabled
- **THEN** every section, image, link, and install command is present and readable

#### Scenario: The page is opened on a narrow viewport

- **WHEN** the page is viewed at 360 px wide
- **THEN** the document body does not scroll horizontally
- **AND** any wide element, such as a code block or a table, scrolls within its own region

### Requirement: Screenshots depict the interface Murmly actually ships

Images presented as screenshots SHALL be captures of Murmly running, showing the interface as it is currently specified. An illustration that is not a capture MUST be visually distinguishable from a capture and MUST NOT be captioned as one. The page SHALL show the recording overlay in its listening state, in its processing state, and with a live partial transcript visible.

#### Scenario: A screenshot is placed on the page

- **WHEN** an image on the page is presented as a screenshot
- **THEN** it is a capture of Murmly running rather than a mockup or a rendering

#### Scenario: A state cannot be captured

- **WHEN** an interface state cannot be captured and is drawn instead
- **THEN** the drawing is presented as an illustration and is not described as a screenshot

#### Scenario: The overlay's states are shown

- **WHEN** the page's imagery is reviewed
- **THEN** the listening state, the processing state, and a live partial transcript each appear

### Requirement: The project mark is original and carried by the repository

The page's logo, wordmark, favicon, and social-preview image SHALL be authored in this repository and distributable under its license. The page MUST NOT use a third-party mark, stock illustration, or icon set that the repository does not carry and cannot relicense. The mark SHALL be legible at 16 px and SHALL render on both a light and a dark background.

#### Scenario: The mark is rendered small

- **WHEN** the logo is rendered at 16 px as a favicon
- **THEN** its silhouette remains identifiable

#### Scenario: The page is viewed in either colour scheme

- **WHEN** the page is viewed under a light colour scheme and under a dark one
- **THEN** the logo and wordmark are legible in both

#### Scenario: The page is shared as a link

- **WHEN** the page's address is pasted into a service that renders link previews
- **THEN** the preview shows the project's own image, title, and description, served from this origin

### Requirement: The page is usable without sight, without a mouse, and without motion

Every image SHALL carry a text alternative that conveys what the image shows, or be marked as decorative when it carries no information. Text SHALL meet a contrast ratio of at least 4.5:1 against its background, and at least 3:1 for text at 24 px or larger. Every interactive element SHALL be reachable by keyboard and SHALL show a visible focus indicator. Any animation SHALL be suppressed when the visitor has asked for reduced motion.

#### Scenario: The page is read by a screen reader

- **WHEN** the page is traversed by a screen reader
- **THEN** every informative image announces what it shows
- **AND** decorative images are skipped rather than announced

#### Scenario: The page is navigated by keyboard

- **WHEN** focus is advanced through the page with the keyboard alone
- **THEN** every link and control receives focus in reading order with a visible indicator

#### Scenario: The visitor has asked for reduced motion

- **WHEN** the page is loaded by a visitor whose system requests reduced motion
- **THEN** no element animates, and any content conveyed by animation is still conveyed

### Requirement: The page routes to installation rather than replacing the documentation

The page SHALL show the command that installs Murmly and SHALL link to the repository's README for everything beyond a first install. The page MUST NOT restate configuration reference material, the speech-session protocol, or troubleshooting steps, because a second copy of those drifts from the first.

#### Scenario: A visitor decides to install

- **WHEN** the install section is read
- **THEN** it shows the install command and links to the repository's README

#### Scenario: A visitor looks for a configuration option

- **WHEN** the page is searched for a configuration reference
- **THEN** the page links to the repository's documentation instead of reproducing it
