"""Small modal dialogs used by the main window."""
from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from ..i18n import _

_PLACEHOLDER_RE = re.compile(r"%[fFuUdDnNickvm]")


def _clean_commandline(cmdline: str) -> str:
    return _PLACEHOLDER_RE.sub("", cmdline).strip()


def error_dialog(parent: Gtk.Window, message: str) -> None:
    dialog = Gtk.MessageDialog(
        transient_for=parent, modal=True, message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE, text=message,
    )
    dialog.run()
    dialog.destroy()


def ask_credentials(parent: Gtk.Window) -> tuple[str, str] | None:
    dialog = Gtk.Dialog(title=_("VPN credentials"), transient_for=parent, modal=True)
    dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL, _("_Connect"), Gtk.ResponseType.OK)

    grid = Gtk.Grid(column_spacing=8, row_spacing=8)
    grid.set_margin_top(12)
    grid.set_margin_bottom(12)
    grid.set_margin_start(12)
    grid.set_margin_end(12)
    dialog.get_content_area().add(grid)

    grid.attach(Gtk.Label(label=_("Username:"), xalign=0), 0, 0, 1, 1)
    user_entry = Gtk.Entry()
    grid.attach(user_entry, 1, 0, 1, 1)

    grid.attach(Gtk.Label(label=_("Password:"), xalign=0), 0, 1, 1, 1)
    pass_entry = Gtk.Entry(visibility=False)
    pass_entry.set_activates_default(True)
    grid.attach(pass_entry, 1, 1, 1, 1)

    ok_btn = dialog.get_widget_for_response(Gtk.ResponseType.OK)
    if ok_btn is not None:
        dialog.set_default(ok_btn)

    dialog.show_all()
    response = dialog.run()
    result = None
    if response == Gtk.ResponseType.OK:
        result = (user_entry.get_text(), pass_entry.get_text())
    dialog.destroy()
    return result


def prompt_add_app(parent: Gtk.Window) -> tuple[str, str] | None:
    dialog = Gtk.Dialog(title=_("Add application"), transient_for=parent, modal=True)
    dialog.add_buttons(_("_Cancel"), Gtk.ResponseType.CANCEL, _("_Add"), Gtk.ResponseType.OK)
    dialog.set_default_size(380, -1)

    grid = Gtk.Grid(column_spacing=8, row_spacing=8)
    grid.set_margin_top(12)
    grid.set_margin_bottom(12)
    grid.set_margin_start(12)
    grid.set_margin_end(12)
    dialog.get_content_area().add(grid)

    grid.attach(Gtk.Label(label=_("Name:"), xalign=0), 0, 0, 1, 1)
    name_entry = Gtk.Entry()
    name_entry.set_placeholder_text("Firefox")
    grid.attach(name_entry, 1, 0, 1, 1)

    grid.attach(Gtk.Label(label=_("Command:"), xalign=0), 0, 1, 1, 1)
    cmd_entry = Gtk.Entry()
    cmd_entry.set_placeholder_text("firefox")
    grid.attach(cmd_entry, 1, 1, 1, 1)

    def _pick(_btn):
        chooser = Gtk.AppChooserDialog.new_for_content_type(dialog, Gtk.DialogFlags.MODAL, "text/plain")
        if chooser.run() == Gtk.ResponseType.OK:
            info = chooser.get_app_info()
            if info:
                name_entry.set_text(info.get_display_name() or info.get_name() or "")
                cmd_entry.set_text(_clean_commandline(info.get_commandline() or ""))
        chooser.destroy()

    installed_btn = Gtk.Button(label=_("Pick installed app…"))
    installed_btn.connect("clicked", _pick)
    grid.attach(installed_btn, 0, 2, 2, 1)

    dialog.show_all()
    response = dialog.run()
    result = None
    if response == Gtk.ResponseType.OK:
        name = name_entry.get_text().strip()
        cmd = cmd_entry.get_text().strip()
        if name and cmd:
            result = (name, cmd)
    dialog.destroy()
    return result
