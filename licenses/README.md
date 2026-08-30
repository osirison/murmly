# Licenses carried for the published site

Everything on Murmly's own pages -- the mark, the wordmark, the favicon, the
social preview, the diagrams and the screenshots -- is authored in this
repository. Two things on the published manual are not, and their license texts
are here because the site carries them.

| File | Covers |
| --- | --- |
| `pictogrammers-free-license.txt` | The icons Material for MkDocs draws into the manual |
| `mkdocs-material-license.txt` | Material for MkDocs itself, the theme that generates the manual |

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
