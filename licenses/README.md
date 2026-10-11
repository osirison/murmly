# Licenses carried for the published site and the desktop app

Everything on Murmly's own pages -- the mark, the wordmark, the favicon, the
social preview, the diagrams and the screenshots -- is authored in this
repository. Two things on the published manual are not, and their license texts
are here because the site carries them. The desktop app carries two fonts of
its own, listed in the last two rows.

| File | Covers |
| --- | --- |
| `pictogrammers-free-license.txt` | The icons Material for MkDocs draws into the manual |
| `mkdocs-material-license.txt` | Material for MkDocs itself, the theme that generates the manual |
| `wespeaker-resnet34-lm-license.txt` | The speaker model `setup.sh` and `bootstrap.ps1` download; not part of the site |
| `instrument-sans-ofl.txt` | Instrument Sans, the app's text font; not part of the site |
| `ibm-plex-mono-ofl.txt` | IBM Plex Mono, the app's font for keys, paths and config names; not part of the site |

The speaker model row is not about the site. Murmly does not carry
or modify the speaker model: setup downloads it from its publisher onto the
user's machine. The file names the model, the pinned source and the licence so
the attribution lives with the other licenses.

## Why the icons need this

Material's stylesheet embeds 42 icons as `data:image/svg+xml` URIs: the twelve
admonition symbols, the navigation hamburger, the search glyph, the chevrons,
the back-to-top arrow and the copy-to-clipboard glyph. They are same-origin by
construction, so a check for off-origin requests cannot see them, but the site
publishes them all the same.

Admonitions are the largest single readability gain the manual has for a reader
who is not a developer, so the icons stay and the license is carried. This is a
deliberate exception to the rule that the site's imagery is authored here, and
it is recorded rather than assumed.

## Why the theme needs this

`overrides/partials/copyright.html` removes the theme's own footer credit,
because it is the one link in generated HTML that addresses another host. The
license is carried here in its place.

Both files are copied verbatim out of the pinned `mkdocs-material` wheel
recorded in `uv.lock`. Update them when that pin moves.

## Why the fonts need this

The desktop app draws every word in Instrument Sans or IBM Plex Mono, both under
the SIL Open Font License 1.1, which requires its text to accompany every copy of
the font. The app loads them from files inside the `murmly.app` package, never
from the network or from the system, so they are in the wheel, and the wheel
carries these two license files with them (`license-files` in `pyproject.toml`).

Each is a latin subset of the upstream static TrueType file at the weights the
design uses (Instrument Sans 400, 500, 600; IBM Plex Mono 400, 600), cut with
`pyftsubset` and with the style names set so Qt groups the weights into one
family. `src/murmly/app/fonts/README.md` records each file's source, version and
recipe.

The Plex license reserves the font name "Plex" for the unmodified font. The
subsets keep the family name "IBM Plex Mono", as the widely used web-font
packaging of the same files does, because the app asks for that family by name;
a stricter reading of the reserved name would mean renaming them.
