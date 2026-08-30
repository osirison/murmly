## ADDED Requirements

### Requirement: Non-disruptive placement on every platform

Wherever the overlay is presented, Murmly SHALL center it along the bottom edge of
one display with a configurable bottom margin. The overlay MUST remain above
ordinary application windows, MUST NOT request keyboard focus, MUST NOT intercept
pointer input, and MUST NOT move or resize in response to animation. Neither the
platform nor the display protocol MUST change the visible recording lifecycle.

Where a platform cannot provide one of those properties, Murmly SHALL NOT present
the overlay at all, and MUST report that property as the reason. An overlay that
takes focus from what the person is dictating into, or that swallows their clicks,
defeats the thing Murmly exists to do; not drawing it is the lesser failure and the
one Murmly already takes when the visual runtime is missing.

#### Scenario: Overlay appears over the focused application

- **WHEN** the overlay becomes visible while another application has focus
- **THEN** it appears at the configured bottom-center position without changing the
  focused application

#### Scenario: Pointer crosses the overlay

- **WHEN** the user points or clicks through the overlay area
- **THEN** the underlying application receives the pointer interaction

#### Scenario: Multiple displays are connected

- **WHEN** the overlay becomes visible with more than one display connected
- **THEN** Murmly selects one display deterministically and keeps the overlay on
  that display for the current recording session

#### Scenario: The same presentation on every platform

- **WHEN** the overlay becomes visible on any platform and display protocol Murmly
  supports
- **THEN** it provides the same position, stacking, focus, input, dimensions, and
  visual states as it does on every other

#### Scenario: A platform that cannot present it without taking input

- **WHEN** the platform cannot present a surface that is above ordinary windows,
  takes no focus, and intercepts no pointer input
- **THEN** Murmly presents no overlay
- **AND** the reported cause names the property the platform could not provide
- **AND** capture, transcription, and delivery continue unchanged

## REMOVED Requirements

### Requirement: Non-disruptive Plasma placement

**Reason**: The requirement specified placement, stacking, focus and input
behaviour only for a KDE Plasma X11 or Wayland session, and its scenarios named
those two sessions as the only presentations to hold equal. Murmly now presents the
overlay on Linux, Windows and macOS, so the guarantee has to be stated against
whatever platform is presenting it.

**Migration**: Replaced by "Non-disruptive placement on every platform", which
carries every property the removed requirement demanded — bottom-centred on one
display with a configurable margin, above ordinary windows, no keyboard focus, no
pointer interception, no movement under animation, and identical visible lifecycle
across display protocols — and adds what a platform must do when it cannot provide
them. No behaviour a KDE Plasma session had is withdrawn.
