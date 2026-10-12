# Bundled fonts

Two SIL OFL 1.1 families, loaded by `murmly.app.fonts` from this directory.
Instrument Sans is a latin subset. IBM Plex Mono is the unmodified upstream
file: its licence reserves the font name "Plex", which a Modified Version (a
subset counts) may not use, so the Plex files are shipped byte-identical rather
than subset or renamed. Licence texts: `licenses/instrument-sans-ofl.txt` and
`licenses/ibm-plex-mono-ofl.txt` at the repository root.

| File | Source (static TTF) | Version |
| --- | --- | --- |
| `InstrumentSans-Regular.ttf`, `-Medium.ttf`, `-SemiBold.ttf` | `https://github.com/Instrument/instrument-sans`, `fonts/ttf/` | commit `7fa22308a3d0c94ee2b3cd537a1196b65db34a3e` (the repository has no releases) |
| `IBMPlexMono-Regular.ttf` | `https://raw.githubusercontent.com/IBM/plex/%40ibm/plex-mono%402.5.0/packages/plex-mono/fonts/complete/ttf/IBMPlexMono-Regular.ttf` | tag `@ibm/plex-mono@2.5.0` |
| `IBMPlexMono-SemiBold.ttf` | `https://raw.githubusercontent.com/IBM/plex/%40ibm/plex-mono%402.5.0/packages/plex-mono/fonts/complete/ttf/IBMPlexMono-SemiBold.ttf` | tag `@ibm/plex-mono@2.5.0` |

The licence files were taken from the same commit and tag.

## IBM Plex Mono: do not modify

sha256 of the files as shipped, equal to the upstream release files
(`tests/test_app_fonts.py` asserts them):

| File | sha256 |
| --- | --- |
| `IBMPlexMono-Regular.ttf` | `7c6fbddca4b700be918f5f6183d9bd4464fa427fe435f0b480d77fe2bb8c5a43` |
| `IBMPlexMono-SemiBold.ttf` | `f04d7c488ddf7d1fa99f2574efc3406ea4cbe17bb1af3a1ab960f84d0c96a172` |

## Instrument Sans recipe

Subset with fonttools 4.66.1, for each file:

    pyftsubset <upstream>.ttf --output-file=<name>.ttf --no-hinting --notdef-outline \
      --layout-features='*' \
      --unicodes=U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD

The unicode ranges are Google Fonts' `latin` subset. Then rewrite the name table
so each weight other than Regular is its own style of one family: name ID 1 is
"Family Style", ID 2 is "Regular", ID 16 is "Family", ID 17 is "Style". Qt reads
that to offer Medium and SemiBold under "Instrument Sans". Instrument Sans has
no reserved font name, so this is allowed.

## Sizes

| File | Upstream | Shipped |
| --- | --- | --- |
| `InstrumentSans-Regular.ttf` | 86,232 | 48,368 |
| `InstrumentSans-Medium.ttf` | 86,924 | 48,372 |
| `InstrumentSans-SemiBold.ttf` | 87,004 | 48,388 |
| `IBMPlexMono-Regular.ttf` | 173,052 | 173,052 |
| `IBMPlexMono-SemiBold.ttf` | 174,608 | 174,608 |
