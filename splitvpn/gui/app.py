"""GTK application entry point."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .main_window import MainWindow


class SplitVpnApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.splitvpn.gui")
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(application=self)
        self.window.present()


def main() -> int:
    app = SplitVpnApp()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
