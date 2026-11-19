#!/usr/bin/env python3
"""La bulle Kreafinity : un petit bouton rond en bas à droite du bureau.

Un clic ouvre un panneau de discussion compact (l'interface web de Kreafinity
en mode compact), un nouveau clic le referme. Glisser la bulle la déplace ;
un clic droit ouvre un menu. Quand une action attend votre validation et que
le panneau est fermé, une pastille orange apparaît sur la bulle.

Programme autonome : il ne dépend que de Python 3 et des bibliothèques GTK 3
et WebKitGTK du système (Debian/Ubuntu : python3-gi gir1.2-gtk-3.0
gir1.2-webkit2-4.1). Il est lancé par « kreafinity bubble », qui lui donne
l'adresse du démon.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import sys
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

BUBBLE_SIZE = 60
MARGIN = 24  # between the bubble and the screen edges
GAP = 12  # between the bubble and the panel
PANEL_SIZE = (400, 620)
DRAG_THRESHOLD = 6  # pixels the pointer must move before a click becomes a drag

TEXTS = {
    "fr": {
        "tooltip": "Kreafinity — cliquez pour discuter",
        "open_full": "Ouvrir l'interface complète",
        "quit": "Masquer la bulle",
    },
    "en": {
        "tooltip": "Kreafinity — click to chat",
        "open_full": "Open the full interface",
        "quit": "Hide the bubble",
    },
}

Rectangle = tuple[int, int, int, int]  # x, y, width, height


# --- Geometry and helpers (no GTK needed: unit-tested) -----------------------------------


def circle_spans(diameter: int) -> list[tuple[int, int, int]]:
    """Horizontal spans (x, y, width) covering a disc of `diameter` pixels.

    Used to give the bubble window a round shape even without a compositor.
    """
    radius = diameter / 2
    spans = []
    for y in range(diameter):
        dy = y + 0.5 - radius
        half = math.sqrt(max(radius * radius - dy * dy, 0.0))
        start, end = round(radius - half), round(radius + half)
        if end > start:
            spans.append((start, y, end - start))
    return spans


def bubble_position(workarea: Rectangle, size: int = BUBBLE_SIZE) -> tuple[int, int]:
    """Bottom-right corner of the work area (the screen minus panels and docks)."""
    x, y, width, height = workarea
    return x + width - size - MARGIN, y + height - size - MARGIN


def panel_position(
    bubble: tuple[int, int, int], panel: tuple[int, int], workarea: Rectangle
) -> tuple[int, int]:
    """Where to open the panel: above the bubble, right-aligned with it, on screen."""
    bubble_x, bubble_y, size = bubble
    width, height = panel
    area_x, area_y, area_width, area_height = workarea
    x = bubble_x + size - width
    y = bubble_y - GAP - height
    if y < area_y:  # no room above (bubble moved to the top): open below
        y = bubble_y + size + GAP
    # Keep it on screen; if it is bigger than the screen, keep its top-left corner visible.
    x = max(min(x, area_x + area_width - width), area_x)
    y = max(min(y, area_y + area_height - height), area_y)
    return x, y


def infinity_points(
    center_x: float, center_y: float, width: float, steps: int = 96
) -> list[tuple[float, float]]:
    """Points of an infinity sign (lemniscate of Bernoulli) of the given width."""
    half = width / 2
    points = []
    for index in range(steps + 1):
        angle = 2 * math.pi * index / steps
        divisor = 1 + math.sin(angle) ** 2
        points.append(
            (
                center_x + half * math.cos(angle) / divisor,
                center_y + half * math.sin(angle) * math.cos(angle) / divisor,
            )
        )
    return points


def is_internal(uri: str, base: str) -> bool:
    """Whether `uri` belongs to the daemon's interface (the only pages shown in the panel)."""
    return uri == base or uri.startswith(base + "/") or uri.startswith(base + "?")


def is_local_daemon(url: str) -> bool:
    """Whether `url` is the address of a daemon on this computer."""
    parts = urlsplit(url)
    return (
        parts.scheme == "http"
        and parts.hostname in ("127.0.0.1", "localhost")
        and not parts.path.strip("/")
    )


def data_dir() -> Path:
    """Kreafinity's data folder (``~/.local/share/kreafinity``)."""
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "share"
    return root / "kreafinity"


def acquire_single_instance(lock_path: Path) -> IO[str] | None:
    """Hold a lock for the life of the process; None if another bubble already holds it."""
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(lock_path, "w")  # noqa: SIM115 - kept open on purpose
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def parse_message(raw: str) -> dict[str, Any]:
    """A message from the page (JSON object), or an empty dict if malformed."""
    try:
        message = json.loads(raw)
    except ValueError:
        return {}
    return message if isinstance(message, dict) else {}


# --- The bubble -------------------------------------------------------------------------------


class Bubble:
    """The round button and its chat panel."""

    def __init__(self, modules: dict[str, Any], url: str, texts: dict[str, str]) -> None:
        self.m = modules
        Gtk, Gdk, WebKit2 = modules["Gtk"], modules["Gdk"], modules["WebKit2"]
        self.url = url.rstrip("/")
        self.texts = texts
        self.attention = 0
        self.hover = False
        self.press: tuple[float, float, int, int] | None = None
        self.dragged = False

        # The bubble: undecorated, always on top, on every workspace, not in the taskbar.
        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window = window
        window.set_title("Kreafinity — bulle")
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_keep_above(True)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        window.set_accept_focus(False)
        window.set_app_paintable(True)
        window.stick()
        screen = window.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = visual is not None and screen.is_composited()
        if self.composited:
            window.set_visual(visual)
        window.set_size_request(BUBBLE_SIZE, BUBBLE_SIZE)
        window.set_tooltip_text(texts["tooltip"])
        window.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        window.connect("realize", self.on_realize)
        window.connect("draw", self.on_draw)
        window.connect("button-press-event", self.on_press)
        window.connect("motion-notify-event", self.on_motion)
        window.connect("button-release-event", self.on_release)
        window.connect("enter-notify-event", self.on_hover, True)
        window.connect("leave-notify-event", self.on_hover, False)
        window.connect("destroy", lambda *_: Gtk.main_quit())

        # The panel: the web interface in compact mode, loaded at once (hidden) so
        # that it receives events and can flag pending approvals on the bubble.
        panel = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.panel = panel
        panel.set_title("Kreafinity")
        panel.set_decorated(False)
        panel.set_keep_above(True)
        panel.set_skip_taskbar_hint(True)
        panel.set_skip_pager_hint(True)
        panel.set_default_size(*PANEL_SIZE)
        panel.connect("delete-event", self.hide_panel)
        panel.connect("key-press-event", self.on_panel_key)
        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("kreafinity")
        manager.connect("script-message-received::kreafinity", self.on_message)
        view = WebKit2.WebView.new_with_user_content_manager(manager)
        self.view = view
        settings = view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_javascript_can_open_windows_automatically(False)
        view.connect("decide-policy", self.on_policy)
        view.connect("load-failed", self.on_load_failed)
        panel.add(view)
        view.load_uri(f"{self.url}/?compact=1")
        view.show()
        panel.realize()

        window.move(*bubble_position(self.workarea()))
        window.show_all()

    # --- Geometry ---------------------------------------------------------------------

    def workarea(self) -> Rectangle:
        Gdk = self.m["Gdk"]
        display = Gdk.Display.get_default()
        gdk_window = self.window.get_window()
        monitor = None
        if gdk_window is not None:
            monitor = display.get_monitor_at_window(gdk_window)
        monitor = monitor or display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        return area.x, area.y, area.width, area.height

    def on_realize(self, window: Any) -> None:
        # Round shape: clicks on the corners go through; without a compositor the
        # corners are not drawn either.
        cairo = self.m["cairo"]
        region = cairo.Region()
        for x, y, width in circle_spans(BUBBLE_SIZE):
            region.union(cairo.RectangleInt(x, y, width, 1))
        window.input_shape_combine_region(region)
        if not self.composited:
            window.shape_combine_region(region)

    # --- Drawing ----------------------------------------------------------------------

    def on_draw(self, window: Any, context: Any) -> bool:
        cairo = self.m["cairo"]
        size = BUBBLE_SIZE
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0, 0, 0, 0)
        context.paint()
        context.set_operator(cairo.OPERATOR_OVER)

        gradient = cairo.LinearGradient(0, 0, size, size)
        gradient.add_color_stop_rgb(0, 0x8B / 255, 0x6C / 255, 0xFF / 255)  # Kreafinity violet
        gradient.add_color_stop_rgb(1, 0x22 / 255, 0xD3 / 255, 0xEE / 255)  # Kreafinity cyan
        context.arc(size / 2, size / 2, size / 2 - 0.5, 0, 2 * math.pi)
        context.set_source(gradient)
        context.fill_preserve()
        if self.hover:
            context.set_source_rgba(1, 1, 1, 0.16)
            context.fill_preserve()
        context.new_path()

        points = infinity_points(size / 2, size / 2, size * 0.6)
        context.move_to(*points[0])
        for point in points[1:]:
            context.line_to(*point)
        context.close_path()
        context.set_line_width(3.6)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        context.set_source_rgb(0x0B / 255, 0x0F / 255, 0x19 / 255)
        context.stroke()

        if self.attention and not self.panel.get_visible():
            context.arc(size * 0.73, size * 0.27, 7, 0, 2 * math.pi)
            context.set_source_rgb(1, 1, 1)
            context.fill_preserve()
            context.arc(size * 0.73, size * 0.27, 5.5, 0, 2 * math.pi)
            context.set_source_rgb(0xF5 / 255, 0x9E / 255, 0x0B / 255)
            context.fill()
        return True

    def on_hover(self, window: Any, event: Any, inside: bool) -> bool:
        self.hover = inside
        window.queue_draw()
        return False

    # --- Mouse: click toggles the panel, drag moves the bubble, right click = menu ----

    def on_press(self, window: Any, event: Any) -> bool:
        if event.button == 1:
            x, y = window.get_position()
            self.press = (event.x_root, event.y_root, x, y)
            self.dragged = False
        elif event.button == 3:
            self.show_menu(event)
        return True

    def on_motion(self, window: Any, event: Any) -> bool:
        if self.press is None:
            return False
        start_x, start_y, window_x, window_y = self.press
        dx, dy = event.x_root - start_x, event.y_root - start_y
        if self.dragged or math.hypot(dx, dy) > DRAG_THRESHOLD:
            self.dragged = True
            window.move(int(window_x + dx), int(window_y + dy))
        return True

    def on_release(self, window: Any, event: Any) -> bool:
        if event.button == 1 and self.press is not None:
            if not self.dragged:
                self.toggle_panel()
            self.press = None
        return True

    def show_menu(self, event: Any) -> None:
        Gtk = self.m["Gtk"]
        menu = Gtk.Menu()
        open_full = Gtk.MenuItem(label=self.texts["open_full"])
        open_full.connect("activate", lambda *_: self.open_full())
        quit_item = Gtk.MenuItem(label=self.texts["quit"])
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        menu.append(open_full)
        menu.append(quit_item)
        menu.show_all()
        menu.attach_to_widget(self.window, None)
        menu.popup_at_pointer(event)

    # --- Panel ------------------------------------------------------------------------------

    def toggle_panel(self) -> None:
        if self.panel.get_visible():
            self.hide_panel()
            return
        x, y = self.window.get_position()
        self.panel.move(*panel_position((x, y, BUBBLE_SIZE), PANEL_SIZE, self.workarea()))
        self.panel.show_all()
        self.panel.present()
        self.view.grab_focus()
        self.window.queue_draw()

    def hide_panel(self, *_: Any) -> bool:
        self.panel.hide()
        self.window.queue_draw()
        return True  # keep the window (and the conversation) for next time

    def on_panel_key(self, panel: Any, event: Any) -> bool:
        if event.keyval == self.m["Gdk"].KEY_Escape:
            self.hide_panel()
            return True
        return False

    def open_full(self) -> None:
        self.m["Gio"].AppInfo.launch_default_for_uri(f"{self.url}/", None)
        self.hide_panel()

    # --- Messages from the page (only our own interface is ever loaded) ------------------------

    def on_message(self, manager: Any, result: Any) -> None:
        message = parse_message(result.get_js_value().to_string())
        kind = message.get("type")
        if kind == "attention":
            count = message.get("count")
            self.attention = count if isinstance(count, int) and count > 0 else 0
            self.window.queue_draw()
        elif kind == "close":
            self.hide_panel()
        elif kind == "expand":
            self.open_full()

    def on_policy(self, view: Any, decision: Any, decision_type: Any) -> bool:
        WebKit2 = self.m["WebKit2"]
        types = WebKit2.PolicyDecisionType
        if decision_type not in (types.NAVIGATION_ACTION, types.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        if decision_type == types.NAVIGATION_ACTION and is_internal(uri, self.url):
            return False  # our interface: let it load
        decision.ignore()
        if uri.startswith(("http://", "https://")):
            self.m["Gio"].AppInfo.launch_default_for_uri(uri, None)  # in the browser
        return True

    def on_load_failed(self, view: Any, event: Any, uri: str, error: Any) -> bool:
        # The daemon may be restarting: try again shortly.
        self.m["GLib"].timeout_add_seconds(3, self._reload)
        return False

    def _reload(self) -> bool:
        self.view.load_uri(f"{self.url}/?compact=1")
        return False


def load_modules() -> dict[str, Any]:
    """Import GTK 3 and WebKitGTK (4.1, or 4.0 on older systems)."""
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    import cairo
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2

    return {"Gtk": Gtk, "Gdk": Gdk, "Gio": Gio, "GLib": GLib, "WebKit2": WebKit2, "cairo": cairo}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bulle de l'assistant Kreafinity.")
    parser.add_argument("--url", required=True, help="adresse du démon (http://127.0.0.1:27494)")
    parser.add_argument("--lang", default="fr", help="langue des menus (fr, en)")
    args = parser.parse_args(argv)
    if not is_local_daemon(args.url):
        parser.error("the daemon must be local (http://127.0.0.1:<port>)")

    lock = acquire_single_instance(data_dir() / "bubble.lock")
    if lock is None:
        print("La bulle Kreafinity est déjà affichée.", file=sys.stderr)
        return 0
    # On Wayland, applications cannot place their windows nor stay on top:
    # through XWayland they can, as on X11.
    if os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ["GDK_BACKEND"] = "x11"

    modules = load_modules()
    Gtk, GLib = modules["Gtk"], modules["GLib"]
    Bubble(modules, args.url, TEXTS.get(args.lang, TEXTS["fr"]))
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, Gtk.main_quit)
    Gtk.main()
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
