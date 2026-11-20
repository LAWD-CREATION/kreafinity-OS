// Kreafinity desktop layout, familiar to Windows users: one taskbar at the
// bottom with the start menu, the open applications, the assistant, the
// notification area and the clock.

var panel = new Panel;
panel.location = "bottom";
panel.height = 2 * Math.floor(gridUnit * 2.5 / 2);

// Start menu with the Kreafinity logo; the Windows key opens it.
var start = panel.addWidget("org.kde.plasma.kickoff");
start.currentConfigGroup = ["General"];
start.writeConfig("icon", "kreafinity");
start.currentConfigGroup = ["Shortcuts"];
start.writeConfig("global", "Alt+F1");

panel.addWidget("org.kde.plasma.icontasks");
panel.addWidget("org.kde.plasma.marginsseparator");

// The assistant, next to the notification area (Meta+C, like Copilot).
var assistant = panel.addWidget("org.kreafinity.assistant");
assistant.currentConfigGroup = ["Shortcuts"];
assistant.writeConfig("global", "Meta+C");

panel.addWidget("org.kde.plasma.systemtray");
panel.addWidget("org.kde.plasma.digitalclock");
panel.addWidget("org.kde.plasma.showdesktop");

var desktopsArray = desktopsForActivity(currentActivity());
for (var j = 0; j < desktopsArray.length; j++) {
    desktopsArray[j].wallpaperPlugin = "org.kde.image";
}
