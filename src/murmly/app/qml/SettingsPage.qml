import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Settings: the Dictation and Speech groups. Every control shows the value in the
// configuration file and, when changed, asks the controller to write that one key
// (`settings.change`); nothing here keeps a value of its own. The controller's
// `view` already holds every string and flag; this only lays them out.
Item {
    id: page

    property string title: ""
    // "dictation" shows the groups of dictation settings, "speech" the speaking ones.
    property string pane: "dictation"
    readonly property var view: settings.view
    readonly property bool loaded: view.loaded === true
    readonly property var values: view.values !== undefined ? view.values : ({})
    readonly property var options: view.options !== undefined ? view.options : ({})
    readonly property var keys: view.keys !== undefined ? view.keys : ({})
    readonly property var ranges: view.ranges !== undefined ? view.ranges : ({})
    readonly property bool locked: view.disabled === true

    function show(pane, section) {
        page.pane = pane
        Qt.callLater(function () {
            if (section === speechTop || section === computerTop) {
                scroller.contentY = 0
                return
            }
            var target = section.mapToItem(column, 0, 0).y + column.y - theme.space.space8
            scroller.contentY = Math.max(0, Math.min(target, scroller.contentHeight - scroller.height))
        })
    }

    RowLayout {
        anchors.fill: parent
        anchors.bottomMargin: footer.height
        anchors.leftMargin: theme.space.space32
        anchors.rightMargin: theme.space.space32
        anchors.topMargin: theme.space.space34
        spacing: theme.space.space32

        // The groups. They scroll when the window is too short to show them all.
        Flickable {
            id: groupsNav
            Layout.preferredWidth: 190
            Layout.maximumWidth: 190
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: navColumn.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds

        ColumnLayout {
            id: navColumn
            // Inset so a focus ring drawn outside a button is not clipped.
            x: 3
            width: groupsNav.width - 6
            spacing: 2

            AppText {
                role: "label"
                text: "Dictation"
                color: theme.colors.textLabel
                leftPadding: theme.space.space10
                bottomPadding: 6
            }

            SubNavButton {
                objectName: "settings-go-keys"
                text: "Your keys"
                selected: page.pane === "dictation"
                Layout.fillWidth: true
                onClicked: page.show("dictation", keysSection)
            }
            SubNavButton {
                objectName: "settings-go-accuracy"
                text: "Accuracy and speed"
                Layout.fillWidth: true
                onClicked: page.show("dictation", accuracySection)
            }
            SubNavButton {
                objectName: "settings-go-finishing"
                text: "Finishing a recording"
                Layout.fillWidth: true
                onClicked: page.show("dictation", finishingSection)
            }
            SubNavButton {
                objectName: "settings-go-live"
                text: "Words as you speak"
                Layout.fillWidth: true
                onClicked: page.show("dictation", liveSection)
            }
            SubNavButton {
                objectName: "settings-go-delivery"
                text: "Typing your words"
                Layout.fillWidth: true
                onClicked: page.show("dictation", deliverySection)
            }
            SubNavButton {
                objectName: "settings-go-indicator"
                text: "On-screen indicator"
                Layout.fillWidth: true
                onClicked: page.show("dictation", indicatorSection)
            }

            AppText {
                role: "label"
                text: "Speaking"
                color: theme.colors.textLabel
                leftPadding: theme.space.space10
                topPadding: theme.space.space18
                bottomPadding: 6
            }

            SubNavButton {
                objectName: "settings-go-speech"
                text: "Speech"
                selected: page.pane === "speech"
                Layout.fillWidth: true
                onClicked: page.show("speech", speechTop)
            }

            AppText {
                role: "label"
                text: "This computer"
                color: theme.colors.textLabel
                leftPadding: theme.space.space10
                topPadding: theme.space.space18
                bottomPadding: 6
            }

            SubNavButton {
                objectName: "settings-go-remove"
                text: "Remove murmly"
                selected: page.pane === "computer"
                Layout.fillWidth: true
                onClicked: page.show("computer", computerTop)
            }
        }   // navColumn
        }

        Flickable {
            id: scroller
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: column.implicitHeight + theme.space.space40
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            ColumnLayout {
                id: column
                width: Math.min(scroller.width - theme.space.space8, 760)
                spacing: theme.space.space26

                ColumnLayout {
                    spacing: theme.space.space10
                    Layout.fillWidth: true

                    AppText {
                        objectName: "settings-title"
                        role: "title26"
                        text: page.pane === "speech" ? "Speech" : page.pane === "computer" ? "This computer" : "Settings"
                        color: theme.colors.text
                        Layout.fillWidth: true
                        Accessible.role: Accessible.Heading
                    }

                    AppText {
                        role: "bodyLead"
                        text: page.pane === "computer"
                            ? "Removing murmly stops it and clears away what it set up. Nothing you wrote is deleted unless you say so."
                            : page.pane === "speech"
                            ? "Murmly can read your coding assistant's replies aloud and tell you when it has finished. It stays silent until you turn it on, so nothing makes a sound you didn't ask for."
                            : "Murmly works without changing anything here. Each change is saved straight away, and murmly restarts in the background to pick it up."
                        color: theme.colors.text3
                        Layout.fillWidth: true
                        Layout.maximumWidth: 580
                    }
                }

                // The file cannot be read: its own words, and nothing can be changed.
                Banner {
                    objectName: "settings-error"
                    visible: page.pane !== "computer" && view.error !== undefined && view.error !== ""
                    tone: "crimson"
                    mono: true
                    title: view.errorIsParse === true
                        ? "The settings file cannot be read" : "The settings file cannot be opened"
                    text: view.error !== undefined ? view.error : ""
                    textItem.objectName: "settings-error-text"
                }

                Banner {
                    objectName: "settings-message"
                    visible: page.pane !== "computer" && !page.locked && view.message !== undefined && view.message !== ""
                    tone: "crimson"
                    title: "That was not saved"
                    text: view.message !== undefined ? view.message : ""
                    textItem.objectName: "settings-message-text"
                }

                Banner {
                    objectName: "settings-notice"
                    visible: page.pane !== "computer" && view.notice !== undefined && view.notice !== ""
                    tone: "amber"
                    text: view.notice !== undefined ? view.notice : ""
                    textItem.objectName: "settings-notice-text"
                }

                AppText {
                    visible: !page.loaded && page.pane !== "computer"
                    text: "Reading the settings file…"
                    role: "body"
                    color: theme.colors.text4
                }

                // ---- This computer: taking murmly off it ------------------------
                // Outside the groups below, which are off while the settings file
                // cannot be read: removal does not depend on that file.
                ColumnLayout {
                    id: computerTop
                    visible: page.pane === "computer"
                    spacing: theme.space.space12
                    Layout.fillWidth: true

                    AppText {
                        role: "label"
                        text: "Remove murmly"
                        color: theme.colors.textLabel
                        Accessible.role: Accessible.Heading
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: removeRow.implicitHeight + theme.space.space16 * 2
                        radius: theme.radii.r13
                        color: theme.colors.panel
                        border.width: 1
                        border.color: theme.colors.borderSubtle

                        Flow {
                            id: removeRow
                            x: theme.space.space20
                            y: theme.space.space16
                            width: parent.width - theme.space.space20 * 2
                            spacing: theme.space.space20

                            Column {
                                width: Math.max(300, removeRow.width - removeButton.implicitWidth - removeRow.spacing)
                                spacing: 3

                                AppText {
                                    role: "title14"
                                    width: parent.width
                                    text: "Take murmly off this computer"
                                    color: theme.colors.textBright
                                }

                                AppText {
                                    role: "body"
                                    width: Math.min(parent.width, 520)
                                    text: "Stops the background service and frees your keys. Your settings and downloaded models are kept unless you choose to delete them on the next step."
                                    color: theme.colors.text4
                                }
                            }

                            AppButton {
                                id: removeButton
                                objectName: "remove-open"
                                kind: "danger"
                                text: "Remove murmly…"
                                enabled: !removal.view.busy
                                onClicked: removal.request()
                            }
                        }
                    }
                }

                // Every control below is off while the file cannot be read.
                ColumnLayout {
                    id: groups
                    visible: page.loaded
                    enabled: !page.locked
                    spacing: theme.space.space30
                    Layout.fillWidth: true

                    // ---- Dictation ------------------------------------------------
                    ColumnLayout {
                        visible: page.pane === "dictation"
                        spacing: theme.space.space30
                        Layout.fillWidth: true

                        ColumnLayout {
                            id: keysSection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "Your keys"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            // What binding a key came to; a refusal is an outline, never a fill.
                            Banner {
                                objectName: "settings-keys-note"
                                visible: hotkeys.view.note.tone !== undefined && hotkeys.view.note.tone !== ""
                                tone: hotkeys.view.note.tone === "amber" ? "amber" : "crimson"
                                title: hotkeys.view.note.status === "held" ? "Those keys are taken"
                                    : hotkeys.view.note.status === "unconfirmed" ? "Saved for your next sign-in"
                                    : "The keys were not set"
                                text: hotkeys.view.note.text !== undefined ? hotkeys.view.note.text : ""
                                textItem.objectName: "settings-keys-note-text"
                            }

                            AppText {
                                objectName: "settings-keys-detail"
                                visible: hotkeys.view.note.tone !== undefined && hotkeys.view.note.tone !== ""
                                    && hotkeys.view.note.detail !== undefined && hotkeys.view.note.detail !== ""
                                role: "monoMeta"
                                text: hotkeys.view.note.detail !== undefined ? hotkeys.view.note.detail : ""
                                color: theme.colors.text5
                                Layout.fillWidth: true
                            }

                            SettingsCard {
                                SettingRow {
                                    title: "Dictate"
                                    text: "Types into the window you started in."
                                    divider: true
                                    KeyField {
                                        objectName: "settings-key-window"
                                        purpose: "window"
                                    }
                                }
                                SettingRow {
                                    title: "Reply to your assistant"
                                    text: "Sends your reply to the assistant that is speaking, not to the window in front."
                                    KeyField {
                                        objectName: "settings-key-session"
                                        purpose: "session"
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            id: accuracySection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "Accuracy and speed"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            ColumnLayout {
                                spacing: theme.space.space10
                                Layout.fillWidth: true

                                Repeater {
                                    model: page.options["profile"] !== undefined ? page.options["profile"] : []

                                    ChoiceCard {
                                        required property var modelData
                                        objectName: "set-profile-" + modelData.id
                                        Layout.fillWidth: true
                                        title: modelData.name
                                        detail: modelData.detail
                                        body: modelData.text
                                        selected: modelData.checked === true
                                        onClicked: settings.change("profile", modelData.id)
                                    }
                                }
                            }

                            SettingsCard {
                                SettingRow {
                                    title: "Run it on"
                                    divider: true
                                    keyLine: page.keys["stt-device"] || ""
                                    Segmented {
                                        objectName: "set-stt-device"
                                        label: "Run it on"
                                        options: page.options["stt-device"] || []
                                        onPicked: function (id) { settings.change("stt-device", id) }
                                    }
                                }
                                SettingRow {
                                    title: "Give memory back when idle"
                                    text: view.unloadText || ""
                                    keyLine: page.keys["unload"] || ""
                                    ToggleSwitch {
                                        objectName: "set-unload"
                                        text: "Give memory back when idle"
                                        on: page.values["unload"] === true
                                        onClicked: settings.change("unload", !on)
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            id: finishingSection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "Finishing a recording"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                implicitHeight: finishColumn.implicitHeight + theme.space.space16 * 2
                                radius: theme.radii.r13
                                color: theme.colors.panel
                                border.width: 1
                                border.color: theme.colors.borderSubtle

                                ColumnLayout {
                                    id: finishColumn
                                    anchors.fill: parent
                                    anchors.margins: theme.space.space16
                                    anchors.leftMargin: theme.space.space20
                                    anchors.rightMargin: theme.space.space20
                                    spacing: theme.space.space14

                                    Segmented {
                                        objectName: "set-finish"
                                        label: "How a recording ends"
                                        options: page.options["finish"] || []
                                        onPicked: function (id) { settings.change("finish", id) }
                                    }

                                    AppText {
                                        objectName: "finish-text"
                                        role: "body"
                                        text: view.finishText || ""
                                        color: theme.colors.text3
                                        Layout.fillWidth: true
                                        Layout.maximumWidth: 560
                                    }

                                    RowLayout {
                                        visible: view.showPause === true
                                        spacing: theme.space.space16
                                        Layout.fillWidth: true

                                        AppText {
                                            role: "row"
                                            text: "Pause length"
                                            color: theme.colors.textBright
                                            wrapMode: Text.NoWrap
                                        }

                                        SliderField {
                                            id: pauseSlider
                                            objectName: "set-pause"
                                            label: "Pause length"
                                            Layout.fillWidth: true
                                            from: page.ranges["pause"] ? page.ranges["pause"].min : 250
                                            to: page.ranges["pause"] ? page.ranges["pause"].max : 5000
                                            stepSize: page.ranges["pause"] ? page.ranges["pause"].step : 250
                                            bound: page.values["pause"] !== undefined ? page.values["pause"] : 2000
                                            format: function (v) { return (v / 1000) + " s" }
                                            onCommitted: function (v) { settings.change("pause", v) }
                                        }

                                        AppText {
                                            objectName: "set-pause-value"
                                            role: "monoMeta"
                                            text: pauseSlider.valueText
                                            color: theme.colors.textBright
                                            wrapMode: Text.NoWrap
                                            Layout.minimumWidth: 48
                                            horizontalAlignment: Text.AlignRight
                                        }
                                    }

                                    AppText {
                                        role: "monoFootnote"
                                        text: page.keys["finish"] || ""
                                        color: theme.colors.text5
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            id: liveSection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "Words as you speak"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            SettingsCard {
                                SettingRow {
                                    title: "Show my words while I speak"
                                    text: "A rough guess in the on-screen indicator. It never changes what gets typed, and anyone who can see your screen can read it."
                                    keyLine: page.keys["live"] || ""
                                    ToggleSwitch {
                                        objectName: "set-live"
                                        text: "Show my words while I speak"
                                        on: page.values["live"] === true
                                        onClicked: settings.change("live", !on)
                                    }
                                }
                            }

                            Banner {
                                objectName: "live-behind"
                                visible: view.liveBehind === true
                                tone: "amber"
                                title: "On the processor, your words will trail behind your voice"
                                text: "What gets typed is unaffected. Pick Fast, or run it on the graphics card, for words that keep up."
                            }
                        }

                        ColumnLayout {
                            id: deliverySection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "Typing your words"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            SettingsCard {
                                SettingRow {
                                    title: "Check the window before typing"
                                    text: "If you switch windows while murmly works, your words wait on the clipboard instead of landing somewhere else."
                                    divider: true
                                    keyLine: page.keys["verify"] || ""
                                    ToggleSwitch {
                                        objectName: "set-verify"
                                        text: "Check the window before typing"
                                        on: page.values["verify"] === true
                                        onClicked: settings.change("verify", !on)
                                    }
                                }
                                SettingRow {
                                    title: "Put my clipboard back afterwards"
                                    text: "Waits so the app can read your words first. Murmly never puts it back over words it couldn't confirm were typed."
                                    keyLine: page.keys["restore"] || ""
                                    ToggleSwitch {
                                        objectName: "set-restore"
                                        text: "Put my clipboard back afterwards"
                                        on: page.values["restore"] === true
                                        onClicked: settings.change("restore", !on)
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            id: indicatorSection
                            spacing: theme.space.space12
                            Layout.fillWidth: true

                            AppText {
                                role: "label"
                                text: "On-screen indicator"
                                color: theme.colors.textLabel
                                Accessible.role: Accessible.Heading
                            }

                            SettingsCard {
                                SettingRow {
                                    title: "Show it at the bottom of the screen"
                                    text: "It never takes your typing or your clicks."
                                    divider: true
                                    keyLine: page.keys["overlay"] || ""
                                    ToggleSwitch {
                                        objectName: "set-overlay"
                                        text: "Show it at the bottom of the screen"
                                        on: page.values["overlay"] === true
                                        onClicked: settings.change("overlay", !on)
                                    }
                                }
                                SettingRow {
                                    title: "Reduce motion"
                                    text: "Steady symbols and stepped levels instead of moving bars."
                                    divider: true
                                    keyLine: page.keys["reduced"] || ""
                                    ToggleSwitch {
                                        objectName: "set-reduced"
                                        text: "Reduce motion"
                                        on: page.values["reduced"] === true
                                        onClicked: settings.change("reduced", !on)
                                    }
                                }
                                SettingRow {
                                    title: "Text size for words as you speak"
                                    keyLine: page.keys["text-size"] || ""
                                    AppButton {
                                        objectName: "set-text-size-less"
                                        text: "−"
                                        Accessible.name: "Smaller text"
                                        enabled: page.values["text-size"] > (page.ranges["text-size"] ? page.ranges["text-size"].min : 8)
                                        onClicked: settings.change("text-size", page.values["text-size"] - 1)
                                    }
                                    AppText {
                                        objectName: "set-text-size-value"
                                        role: "monoMeta"
                                        text: view.textSizeLabel || ""
                                        color: theme.colors.textBright
                                        wrapMode: Text.NoWrap
                                        height: 34
                                        verticalAlignment: Text.AlignVCenter
                                        horizontalAlignment: Text.AlignHCenter
                                        width: 52
                                    }
                                    AppButton {
                                        objectName: "set-text-size-more"
                                        text: "+"
                                        Accessible.name: "Larger text"
                                        enabled: page.values["text-size"] < (page.ranges["text-size"] ? page.ranges["text-size"].max : 48)
                                        onClicked: settings.change("text-size", page.values["text-size"] + 1)
                                    }
                                }
                            }
                        }
                    }

                    // ---- Speech ---------------------------------------------------
                    ColumnLayout {
                        id: speechTop
                        visible: page.pane === "speech"
                        spacing: theme.space.space30
                        Layout.fillWidth: true

                        SettingsCard {
                            SettingRow {
                                title: "Let murmly speak"
                                text: "Needs no permission. Pressing either of your keys stops speech before the microphone opens."
                                keyLine: page.keys["speak"] || ""
                                ToggleSwitch {
                                    objectName: "set-speak"
                                    text: "Let murmly speak"
                                    on: page.values["speak"] === true
                                    onClicked: settings.change("speak", !on)
                                }
                            }
                        }

                        ColumnLayout {
                            visible: page.values["speak"] === true
                            spacing: theme.space.space30
                            Layout.fillWidth: true

                            ColumnLayout {
                                spacing: theme.space.space12
                                Layout.fillWidth: true

                                AppText {
                                    role: "label"
                                    text: "Voice"
                                    color: theme.colors.textLabel
                                    Accessible.role: Accessible.Heading
                                }

                                GridLayout {
                                    columns: width >= 520 ? 2 : 1
                                    columnSpacing: theme.space.space10
                                    rowSpacing: theme.space.space10
                                    Layout.fillWidth: true

                                    Repeater {
                                        model: page.options["voice"] !== undefined ? page.options["voice"] : []

                                        ChoiceCard {
                                            required property var modelData
                                            objectName: "set-voice-" + modelData.id
                                            Layout.fillWidth: true
                                            Layout.preferredWidth: 1
                                            title: modelData.name
                                            detail: modelData.detail
                                            selected: modelData.checked === true
                                            onClicked: settings.change("voice", modelData.id)
                                        }
                                    }
                                }

                                SettingsCard {
                                    SettingRow {
                                        title: "Speaking rate"
                                        text: view.rateNote || ""
                                        keyLine: page.keys["rate"] || ""
                                        SliderField {
                                            id: rateSlider
                                            objectName: "set-rate"
                                            label: "Speaking rate"
                                            width: 200
                                            from: page.ranges["rate"] ? page.ranges["rate"].min : 50
                                            to: page.ranges["rate"] ? page.ranges["rate"].max : 200
                                            stepSize: page.ranges["rate"] ? page.ranges["rate"].step : 5
                                            bound: page.values["rate"] !== undefined ? page.values["rate"] : 100
                                            format: function (v) { return v + " %" }
                                            onCommitted: function (v) { settings.change("rate", v) }
                                        }
                                        AppText {
                                            objectName: "set-rate-value"
                                            role: "monoMeta"
                                            text: rateSlider.valueText
                                            color: theme.colors.textBright
                                            wrapMode: Text.NoWrap
                                            height: 28
                                            width: 52
                                            verticalAlignment: Text.AlignVCenter
                                            horizontalAlignment: Text.AlignRight
                                        }
                                    }
                                }
                            }

                            ColumnLayout {
                                spacing: theme.space.space12
                                Layout.fillWidth: true

                                AppText {
                                    role: "label"
                                    text: "Quiet hours"
                                    color: theme.colors.textLabel
                                    Accessible.role: Accessible.Heading
                                }

                                SettingsCard {
                                    SettingRow {
                                        title: "Keep quiet at night"
                                        text: "Murmly won't start speaking in this window. Speech already playing finishes its sentence, and dictation works at every hour."
                                        divider: page.values["quiet"] === true
                                        keyLine: page.values["quiet"] === true ? "" : (page.keys["quiet"] || "")
                                        ToggleSwitch {
                                            objectName: "set-quiet"
                                            text: "Keep quiet at night"
                                            on: page.values["quiet"] === true
                                            onClicked: settings.change("quiet", !on)
                                        }
                                    }
                                    SettingRow {
                                        visible: page.values["quiet"] === true
                                        title: "Between"
                                        text: view.quietSummary || ""
                                        keyLine: page.keys["quiet"] || ""
                                        AppText {
                                            text: "From"
                                            role: "row"
                                            color: theme.colors.text3
                                            height: 34
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                        TimeField {
                                            objectName: "set-quiet-from"
                                            label: "Quiet hours start"
                                            bound: page.values["quiet-from"] || ""
                                            onCommitted: function (t) { settings.change("quiet-from", t) }
                                        }
                                        AppText {
                                            text: "Until"
                                            role: "row"
                                            color: theme.colors.text3
                                            height: 34
                                            verticalAlignment: Text.AlignVCenter
                                        }
                                        TimeField {
                                            objectName: "set-quiet-to"
                                            label: "Quiet hours end"
                                            bound: page.values["quiet-to"] || ""
                                            onCommitted: function (t) { settings.change("quiet-to", t) }
                                        }
                                    }
                                }
                            }

                            ColumnLayout {
                                spacing: theme.space.space12
                                Layout.fillWidth: true

                                AppText {
                                    role: "label"
                                    text: "Sound and speed"
                                    color: theme.colors.textLabel
                                    Accessible.role: Accessible.Heading
                                }

                                SettingsCard {
                                    SettingRow {
                                        title: "Make speech on"
                                        text: view.values && view.values["tts-device"] === "cuda"
                                            ? "Starts sooner, but the graphics card keeps the memory speech used until murmly restarts."
                                            : "The processor is the default. Speech starts about 200 ms later than on the graphics card, and nothing is held afterwards."
                                        keyLine: page.keys["tts-device"] || ""
                                        Segmented {
                                            objectName: "set-tts-device"
                                            label: "Make speech on"
                                            options: page.options["tts-device"] || []
                                            onPicked: function (id) { settings.change("tts-device", id) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // Where the changes go and what became of the last one.
    Rectangle {
        id: footer
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        implicitHeight: footerFlow.implicitHeight + theme.space.space12 * 2
        height: implicitHeight
        color: theme.colors.surface

        Rectangle {
            anchors.top: parent.top
            width: parent.width
            height: 1
            color: theme.colors.divider
        }

        Flow {
            id: footerFlow
            x: theme.space.space32
            y: theme.space.space12
            width: parent.width - theme.space.space32 * 2
            spacing: theme.space.space16

            AppText {
                objectName: "settings-footer"
                role: "monoFootnote"
                text: view.footer !== undefined ? view.footer : ""
                color: theme.colors.text5
                height: 30
                verticalAlignment: Text.AlignVCenter
                width: Math.min(implicitWidth, footerFlow.width)
            }

            AppText {
                objectName: "settings-status"
                visible: text !== ""
                role: "monoFootnote"
                text: view.status !== undefined ? view.status : ""
                color: view.status !== undefined && view.status.indexOf("could not") >= 0
                    ? theme.colors.needsYouText : theme.colors.text3
                height: 30
                verticalAlignment: Text.AlignVCenter
                width: Math.min(implicitWidth, footerFlow.width)
            }
        }
    }
}
