// Kreafinity in the taskbar (Plasma 5.27): a button opening the assistant panel.
//
// The panel shows the compact web interface served by the local daemon
// (same as the desktop bubble). The widget also listens to the daemon for
// questions asked from the Plasma search, and opens itself to show the answer.

import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtWebEngine 1.10
import org.kde.plasma.plasmoid 2.0
import org.kde.plasma.core 2.0 as PlasmaCore
import org.kde.plasma.components 3.0 as PlasmaComponents3

Item {
    id: root

    readonly property string daemonUrl: plasmoid.configuration.daemonUrl
    readonly property bool english: Qt.locale().name.indexOf("en") === 0
    property string pendingConversation: ""
    property int lastRequest: -1

    function tr(french, englishText) {
        return english ? englishText : french
    }

    Plasmoid.icon: "kreafinity"
    Plasmoid.toolTipMainText: "Kreafinity"
    Plasmoid.toolTipSubText: tr("Votre assistant IA", "Your AI assistant")
    Plasmoid.preferredRepresentation: Plasmoid.compactRepresentation

    // A question asked from the Plasma search: open and show its answer.
    function show(conversationId) {
        pendingConversation = conversationId
        plasmoid.expanded = true
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
                    for (let i = 0; i < answer.requests.length; i++) {
                        if (answer.requests[i].type === "show_conversation")
                            show(answer.requests[i].conversation_id)
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

    // The web page's file request (paperclip), answered by KDE's own file dialog.
    property var fileRequest: null

    // The paperclip: in a panel, the web engine's own file dialog does not show
    // (the panel closes when it loses the focus). KDE's dialog is asked instead,
    // and the panel stays open meanwhile. The commands are fixed: no page data.
    function pickFiles(request) {
        if (request.mode === FileDialogRequest.FileModeSave)
            return
        request.accepted = true
        if (root.fileRequest !== null)
            root.fileRequest.dialogReject()
        root.fileRequest = request
        Plasmoid.hideOnWindowDeactivate = false
        var title = tr("Joindre des fichiers", "Attach files")
        if (request.mode === FileDialogRequest.FileModeUploadFolder)
            picker.connectSource("kdialog --title '" + title + "' --getexistingdirectory ~")
        else if (request.mode === FileDialogRequest.FileModeOpenMultiple)
            picker.connectSource("kdialog --title '" + title + "' --multiple --separate-output --getopenfilename ~")
        else
            picker.connectSource("kdialog --title '" + title + "' --getopenfilename ~")
    }

    PlasmaCore.DataSource {
        id: picker
        engine: "executable"
        connectedSources: []
        onNewData: {
            disconnectSource(sourceName)
            var request = root.fileRequest
            root.fileRequest = null
            Plasmoid.hideOnWindowDeactivate = true
            Plasmoid.expanded = true
            if (request === null)
                return
            var files = data["exit code"] === 0
                ? data.stdout.split("\n").filter(function (line) { return line.length > 0 }) : []
            if (files.length > 0)
                request.dialogAccept(files)
            else
                request.dialogReject()
        }
    }

    // Starts the daemon's systemd user service when it is not running.
    PlasmaCore.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: disconnectSource(sourceName)
    }

    Plasmoid.fullRepresentation: Item {
        id: panel

        property bool failed: false

        Layout.preferredWidth: PlasmaCore.Units.gridUnit * 26
        Layout.preferredHeight: PlasmaCore.Units.gridUnit * 34
        Layout.minimumWidth: PlasmaCore.Units.gridUnit * 18
        Layout.minimumHeight: PlasmaCore.Units.gridUnit * 20

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

            onLoadingChanged: function (loadRequest) {
                if (loadRequest.status === WebEngineView.LoadFailedStatus)
                    panel.failed = true
                else if (loadRequest.status === WebEngineView.LoadSucceededStatus)
                    panel.failed = false
            }
            // Only our interface opens here; links go to the browser.
            onNavigationRequested: function (request) {
                if (request.url.toString().indexOf(root.daemonUrl + "/") !== 0) {
                    request.action = WebEngineNavigationRequest.IgnoreRequest
                    Qt.openUrlExternally(request.url)
                }
            }
            onNewViewRequested: function (request) {
                Qt.openUrlExternally(request.requestedUrl)
            }
            onFileDialogRequested: function (request) {
                root.pickFiles(request)
            }
            // The microphone, to talk to Kreafinity: for our interface only.
            onFeaturePermissionRequested: function (securityOrigin, feature) {
                var ours = securityOrigin.toString().indexOf(root.daemonUrl) === 0
                grantFeaturePermission(securityOrigin, feature,
                                       ours && feature === WebEngineView.MediaAudioCapture)
            }
        }

        ColumnLayout {
            anchors.centerIn: parent
            width: parent.width - PlasmaCore.Units.gridUnit * 2
            visible: panel.failed
            spacing: PlasmaCore.Units.largeSpacing

            PlasmaCore.IconItem {
                source: "kreafinity"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: PlasmaCore.Units.iconSizes.huge
                Layout.preferredHeight: PlasmaCore.Units.iconSizes.huge
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
            target: plasmoid
            function onExpandedChanged() {
                if (plasmoid.expanded)
                    view.forceActiveFocus()
            }
        }
    }
}
