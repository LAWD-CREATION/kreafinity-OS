// Kreafinity in the taskbar (Plasma 6): a button opening the assistant panel.
//
// The panel shows the compact web interface served by the local daemon
// (same as the desktop bubble). The widget also listens to the daemon for
// questions asked from the Plasma search, and opens itself to show the answer.

import QtQuick
import QtQuick.Layouts
import QtWebEngine
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.plasma5support as P5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    readonly property string daemonUrl: Plasmoid.configuration.daemonUrl
    readonly property bool english: Qt.locale().name.indexOf("en") === 0
    property string pendingConversation: ""
    property int lastRequest: -1
    // The web page's file request (paperclip), answered by KDE's own file dialog.
    property var fileRequest: null

    function tr(french, englishText) {
        return english ? englishText : french
    }

    Plasmoid.icon: "kreafinity"
    toolTipMainText: "Kreafinity"
    toolTipSubText: tr("Votre assistant IA", "Your AI assistant")
    preferredRepresentation: compactRepresentation

    // A question asked from the Plasma search: open and show its answer.
    function show(conversationId) {
        pendingConversation = conversationId
        root.expanded = true
    }

    // Long polling: the daemon answers when a conversation must be shown.
    function poll() {
        const request = new XMLHttpRequest()
        let url = daemonUrl + "/api/ui/requests"
        if (lastRequest >= 0)
            url += "?after=" + lastRequest + "&wait=25"
        request.open("GET", url)
        request.setRequestHeader("X-Kreafinity-Client", "web")
        request.onreadystatechange = function () {
            if (request.readyState !== XMLHttpRequest.DONE)
                return
            let answer = null
            try {
                answer = request.status === 200 ? JSON.parse(request.responseText) : null
            } catch (error) {
                answer = null
            }
            if (answer === null) {
                lastRequest = -1 // daemon stopped: start again when it is back
                pollTimer.interval = 5000
            } else {
                if (lastRequest >= 0) {
                    for (const item of answer.requests) {
                        if (item.type === "show_conversation")
                            show(item.conversation_id)
                    }
                }
                lastRequest = answer.last
                pollTimer.interval = 50
            }
            pollTimer.start()
        }
        request.send()
    }

    Timer {
        id: pollTimer
        interval: 50
        onTriggered: root.poll()
    }

    Component.onCompleted: pollTimer.start()

    // Starts the daemon's systemd user service when it is not running.
    P5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => disconnectSource(sourceName)
    }

    // The paperclip: in a panel, the web engine's own file dialog does not show
    // (the panel closes when it loses the focus). KDE's dialog is asked instead,
    // and the panel stays open meanwhile. The commands are fixed: no page data.
    function pickFiles(request) {
        if (request.mode === FileDialogRequest.FileModeSave)
            return  // not used by Kreafinity: the engine's default
        request.accepted = true
        if (root.fileRequest !== null)
            root.fileRequest.dialogReject()
        root.fileRequest = request
        root.hideOnWindowDeactivate = false
        const title = tr("Joindre des fichiers", "Attach files")
        if (request.mode === FileDialogRequest.FileModeUploadFolder)
            picker.connectSource("kdialog --title '" + title + "' --getexistingdirectory ~")
        else if (request.mode === FileDialogRequest.FileModeOpenMultiple)
            picker.connectSource("kdialog --title '" + title + "' --multiple --separate-output --getopenfilename ~")
        else
            picker.connectSource("kdialog --title '" + title + "' --getopenfilename ~")
    }

    P5Support.DataSource {
        id: picker
        engine: "executable"
        connectedSources: []
        onNewData: (sourceName, data) => {
            disconnectSource(sourceName)
            const request = root.fileRequest
            root.fileRequest = null
            root.hideOnWindowDeactivate = true
            root.expanded = true
            if (request === null)
                return
            const files = data["exit code"] === 0
                ? data.stdout.split("\n").filter(line => line.length > 0) : []
            if (files.length > 0)
                request.dialogAccept(files)
            else
                request.dialogReject()
        }
    }

    fullRepresentation: Item {
        id: panel

        property bool failed: false

        Layout.preferredWidth: Kirigami.Units.gridUnit * 26
        Layout.preferredHeight: Kirigami.Units.gridUnit * 34
        Layout.minimumWidth: Kirigami.Units.gridUnit * 18
        Layout.minimumHeight: Kirigami.Units.gridUnit * 20

        function address() {
            let url = root.daemonUrl + "/?compact=1"
            if (root.pendingConversation !== "")
                url += "&conversation=" + encodeURIComponent(root.pendingConversation)
            return url
        }

        WebEngineView {
            id: view
            anchors.fill: parent
            visible: !panel.failed
            // Loaded once; later requests reach the page itself (live events).
            Component.onCompleted: url = panel.address()

            onLoadingChanged: loadingInfo => {
                if (loadingInfo.status === WebEngineLoadingInfo.LoadFailedStatus)
                    panel.failed = true
                else if (loadingInfo.status === WebEngineLoadingInfo.LoadSucceededStatus)
                    panel.failed = false
            }
            // Only our interface opens here; links go to the browser.
            onNavigationRequested: request => {
                if (request.url.toString().indexOf(root.daemonUrl + "/") !== 0) {
                    request.reject()
                    Qt.openUrlExternally(request.url)
                }
            }
            onNewWindowRequested: request => Qt.openUrlExternally(request.requestedUrl)
            onFileDialogRequested: request => root.pickFiles(request)

            // The microphone, to talk to Kreafinity: for our interface only.
            Connections {
                target: view
                ignoreUnknownSignals: true
                // Qt 6.8 and later.
                function onPermissionRequested(permission) {
                    const ours = permission.origin.toString().indexOf(root.daemonUrl) === 0
                    if (ours && permission.permissionType === WebEnginePermission.PermissionType.MediaAudioCapture)
                        permission.grant()
                    else
                        permission.deny()
                }
                // Before Qt 6.8.
                function onFeaturePermissionRequested(securityOrigin, feature) {
                    const ours = securityOrigin.toString().indexOf(root.daemonUrl) === 0
                    view.grantFeaturePermission(securityOrigin, feature,
                                                ours && feature === WebEngineView.MediaAudioCapture)
                }
            }
        }

        ColumnLayout {
            anchors.centerIn: parent
            width: parent.width - Kirigami.Units.gridUnit * 2
            visible: panel.failed
            spacing: Kirigami.Units.largeSpacing

            Kirigami.Icon {
                source: "kreafinity"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: Kirigami.Units.iconSizes.huge
                Layout.preferredHeight: Kirigami.Units.iconSizes.huge
            }
            PlasmaComponents3.Label {
                text: root.tr("Kreafinity ne répond pas.", "Kreafinity is not responding.")
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            PlasmaComponents3.Button {
                text: root.tr("Démarrer Kreafinity", "Start Kreafinity")
                icon.name: "media-playback-start"
                Layout.alignment: Qt.AlignHCenter
                onClicked: {
                    executable.connectSource("systemctl --user start kreafinity.service")
                    retry.start()
                }
            }
        }

        Timer {
            id: retry
            interval: 3000
            onTriggered: view.url = panel.address()
        }

        Connections {
            target: root
            function onExpandedChanged() {
                if (root.expanded)
                    view.forceActiveFocus()
            }
        }
    }
}
