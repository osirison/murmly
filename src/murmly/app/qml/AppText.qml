import QtQuick

// Text set in one step of the design's type scale: `role` names the step
// ("title26", "label", "monoCaption"), and the theme says what that step is.
Text {
    property string role: "body"
    readonly property var spec: theme.typography[role]

    color: theme.colors.text2
    font.family: spec.family
    font.pixelSize: spec.pixelSize
    font.weight: spec.weight
    font.letterSpacing: spec.letterSpacing
    font.capitalization: spec.uppercase ? Font.AllUppercase : Font.MixedCase
    lineHeight: spec.lineHeight
    lineHeightMode: Text.ProportionalHeight
    wrapMode: Text.Wrap
}
