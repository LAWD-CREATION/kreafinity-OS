/* Kreafinity OS: the slideshow shown while the installer copies the system. */
import QtQuick 2.15
import calamares.slideshow 1.0

Presentation {
    id: presentation

    function onActivate() { }
    function onLeave() { }

    Timer {
        interval: 12000
        running: presentation.activatedInCalamares
        repeat: true
        onTriggered: presentation.goToNextSlide()
    }

    component KreafinitySlide: Slide {
        property string title
        property string body
        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#0b0f19" }
                GradientStop { position: 1.0; color: "#1b1536" }
            }
        }
        Image {
            id: logo
            source: "logo.png"
            width: 120; height: 120
            fillMode: Image.PreserveAspectFit
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: parent.height * 0.12
        }
        Text {
            id: heading
            text: title
            color: "#ffffff"
            font.pixelSize: 26
            font.bold: true
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: logo.bottom
            anchors.topMargin: 24
        }
        Text {
            text: body
            color: "#b9c0d4"
            font.pixelSize: 16
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            width: parent.width * 0.75
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: heading.bottom
            anchors.topMargin: 16
        }
    }

    KreafinitySlide {
        title: "Bienvenue dans Kreafinity OS"
        body: "L'installation se poursuit toute seule et ne prend que quelques minutes. Vous pourrez ensuite parler à votre ordinateur."
    }
    KreafinitySlide {
        title: "Votre IA, votre choix"
        body: "Ollama sur l'ordinateur (gratuit et privé), Claude, GPT, Mistral, Ollama Cloud ou OpenRouter : changez quand vous voulez, sans perdre vos conversations."
    }
    KreafinitySlide {
        title: "Vous gardez le contrôle"
        body: "Avant toute action sur l'ordinateur (déplacer, supprimer, installer…), Kreafinity vous montre ce qu'il va faire et attend votre accord."
    }
    KreafinitySlide {
        title: "Toujours à portée de main"
        body: "Le bouton ∞ de la barre des tâches, Meta+C, le menu démarrer ou le clic droit sur un fichier : posez votre question par écrit ou à voix haute."
    }
}
