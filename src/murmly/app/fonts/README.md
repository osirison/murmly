# Bundled fonts

Latin subsets of two SIL OFL 1.1 families, loaded by `murmly.app.fonts` from this
directory. Licence texts: `licenses/instrument-sans-ofl.txt` and
`licenses/ibm-plex-mono-ofl.txt` at the repository root.

| File | Source (static TTF) | Version |
| --- | --- | --- |
| `InstrumentSans-Regular.ttf`, `-Medium.ttf`, `-SemiBold.ttf` | `https://github.com/Instrument/instrument-sans`, `fonts/ttf/` | commit `7fa22308a3d0c94ee2b3cd537a1196b65db34a3e` (the repository has no releases) |
| `IBMPlexMono-Regular.ttf`, `-SemiBold.ttf` | `https://github.com/IBM/plex`, `packages/plex-mono/fonts/complete/ttf/` | tag `@ibm/plex-mono@2.5.0` |

The licence files were taken from the same commit and tag.

## Recipe

Subset with fonttools 4.66.1, for each file:

    pyftsubset <upstream>.ttf --output-file=<name>.ttf --no-hinting --notdef-outline \
      --layout-features='*' \
      --unicodes=U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD

The unicode ranges are Google Fonts' `latin` subset. Then rewrite the name table
so each weight other than Regular is its own style of one family: name ID 1 is
"Family Style", ID 2 is "Regular", ID 16 is "Family", ID 17 is "Style". Qt reads
that to offer Medium and SemiBold under "Instrument Sans" and SemiBold under
"IBM Plex Mono".

## Sizes

| File | Upstream | Subset |
| --- | --- | --- |
| `InstrumentSans-Regular.ttf` | 86,232 | 48,368 |
| `InstrumentSans-Medium.ttf` | 86,924 | 48,372 |
| `InstrumentSans-SemiBold.ttf` | 87,004 | 48,388 |
| `IBMPlexMono-Regular.ttf` | 173,052 | 27,260 |
| `IBMPlexMono-SemiBold.ttf` | 174,608 | 27,236 |
