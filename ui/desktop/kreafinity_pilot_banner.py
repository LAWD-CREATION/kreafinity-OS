#!/usr/bin/env python3
"""Le bandeau du mode pilote : visible tant que l'assistant pilote l'ordinateur.

Il reste au-dessus des fenêtres, en haut de l'écran, sans jamais prendre le
clavier. Il dit ce que fait l'assistant et propose un bouton « Arrêter ».

Programme autonome (Python 3 + GTK 3 du système), lancé par le démon
Kreafinity, qui lui écrit sur l'entrée standard une ligne JSON par mise à jour
(``{"status": "..."}``). Un clic sur « Arrêter » écrit ``stop`` sur la sortie
standard et ferme le bandeau ; la fin de l'entrée standard le ferme aussi.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

TEXTS = {
    "fr": {
        "title": "Kreafinity pilote votre ordinateur",
        "hint_escape": "Échap ou bougez la souris pour reprendre la main",
        "hint": "Bougez la souris pour reprendre la main",
        "stop": "Arrêter",
        "waiting": "Prêt",
    },
    "en": {
        "title": "Kreafinity is driving your computer",
        "hint_escape": "Press Escape or move the mouse to take back control",
        "hint": "Move the mouse to take back control",
        "stop": "Stop",
        "waiting": "Ready",
    },
}

CSS = b"""
window.kreafinity-pilot { background: transparent; }
.banner {
  background: rgba(24, 20, 58, 0.94); color: #ffffff; border-radius: 16px;
  border: 2px solid #8b5cf6; padding: 6px 8px 6px 14px;
}
.logo { color: #22d3ee; font-size: 20px; font-weight: bold; }
.title { font-weight: bold; font-size: 13px; }
.status { color: #c4b5fd; font-size: 12px; }
.hint { color: #a5b4fc; font-size: 11px; }
button.stop {
  background: #8b5cf6; color: #ffffff; border-radius: 10px; border: none;
  padding: 4px 14px; font-weight: bold; box-shadow: none; text-shadow: none;
}
button.stop:hover { background: #a78bfa; }
"""

WIDTH = 560


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="fr")
    parser.add_argument("--escape", action="store_true", help="Escape stops the pilot")
    args = parser.parse_args()
    texts = TEXTS.get(args.lang, TEXTS["fr"])
    # On Wayland, windows cannot stay on top nor choose their place: through XWayland they can.
    if os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ["GDK_BACKEND"] = "x11"

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    window = Gtk.Window(title="Kreafinity — mode pilote")
    window.get_style_context().add_class("kreafinity-pilot")
    window.set_wmclass("kreafinity-pilot", "Kreafinity")
    window.set_decorated(False)
    window.set_keep_above(True)
    window.stick()
    window.set_skip_taskbar_hint(True)
    window.set_skip_pager_hint(True)
    window.set_accept_focus(False)  # typing must keep going to the applications
    window.set_focus_on_map(False)
    window.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
    visual = screen.get_rgba_visual()
    if visual is not None and screen.is_composited():
        window.set_visual(visual)
        window.set_app_paintable(True)

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.get_style_context().add_class("banner")
    logo = Gtk.Label(label="∞")
    logo.get_style_context().add_class("logo")
    texts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    title = Gtk.Label(label=texts["title"], xalign=0)
    title.get_style_context().add_class("title")
    status = Gtk.Label(label=texts["waiting"], xalign=0)
    status.set_ellipsize(3)  # Pango.EllipsizeMode.END
    status.set_max_width_chars(60)
    status.get_style_context().add_class("status")
    hint = Gtk.Label(label=texts["hint_escape" if args.escape else "hint"], xalign=0)
    hint.get_style_context().add_class("hint")
    texts_box.pack_start(title, False, False, 0)
    texts_box.pack_start(status, False, False, 0)
    texts_box.pack_start(hint, False, False, 0)
    stop = Gtk.Button(label=texts["stop"])
    stop.get_style_context().add_class("stop")
    stop.set_valign(Gtk.Align.CENTER)
    box.pack_start(logo, False, False, 0)
    box.pack_start(texts_box, True, True, 0)
    box.pack_end(stop, False, False, 0)
    window.add(box)

    def place() -> None:
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        window.resize(WIDTH, 1)
        window.move(area.x + (area.width - WIDTH) // 2, area.y + 8)

    def finish(*_: Any) -> bool:
        Gtk.main_quit()
        return False

    def stop_clicked(*_: Any) -> None:
        print("stop", flush=True)
        finish()

    def read(source: Any, condition: Any) -> bool:
        line = sys.stdin.readline()
        if not line:
            return finish()
        try:
            message = json.loads(line)
        except ValueError:
            return True
        if message.get("quit"):
            return finish()
        if "status" in message:
            status.set_text(str(message["status"])[:160])
        return True

    stop.connect("clicked", stop_clicked)
    window.connect("delete-event", lambda *_: True)  # only Kreafinity closes it
    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP, read)
    window.show_all()
    place()
    print("ready", flush=True)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
