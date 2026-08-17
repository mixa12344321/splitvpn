"""Main application window."""
from __future__ import annotations

import ipaddress
import shlex
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .. import client, profiles
from ..ovpn_parser import parse_ovpn
from .dialogs import ask_credentials, error_dialog, prompt_add_app


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application, title="Split VPN")
        self.set_default_size(960, 640)

        self.current_profile: profiles.Profile | None = None
        self.current_session: str | None = None

        self._build_ui()
        self._refresh_profile_list()
        self.show_all()
        GLib.timeout_add(1000, self._tick)

    # ------------------------------------------------------------- build --

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar(show_close_button=True)
        header.set_title("Split VPN")
        self.set_titlebar(header)

        import_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        import_btn.set_tooltip_text("Import .ovpn profile")
        import_btn.connect("clicked", self._on_import_clicked)
        header.pack_start(import_btn)

        self.delete_btn = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON)
        self.delete_btn.set_tooltip_text("Delete profile")
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        header.pack_start(self.delete_btn)

        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.get_style_context().add_class("suggested-action")
        self.connect_btn.connect("clicked", self._on_connect_clicked)
        header.pack_end(self.connect_btn)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(paned)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_size_request(260, -1)
        self.profile_list = Gtk.ListBox()
        self.profile_list.connect("row-selected", self._on_profile_selected)
        left_scroll.add(self.profile_list)
        paned.pack1(left_scroll, False, False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_top(12)
        right.set_margin_bottom(12)
        right.set_margin_start(12)
        right.set_margin_end(12)
        paned.pack2(right, True, False)

        self.title_label = Gtk.Label(xalign=0)
        right.pack_start(self.title_label, False, False, 0)

        self.status_label = Gtk.Label(xalign=0)
        right.pack_start(self.status_label, False, False, 0)

        self.info_label = Gtk.Label(xalign=0)
        self.info_label.set_line_wrap(True)
        right.pack_start(self.info_label, False, False, 0)

        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        right.pack_start(mode_box, False, False, 6)
        mode_box.pack_start(Gtk.Label(label="Split-tunnel mode:"), False, False, 0)
        self.mode_none = Gtk.RadioButton.new_with_label(None, "Full tunnel (no split)")
        self.mode_routes = Gtk.RadioButton.new_with_label_from_widget(self.mode_none, "Split by IP / subnet")
        self.mode_netns = Gtk.RadioButton.new_with_label_from_widget(self.mode_none, "Split by application")
        for b in (self.mode_none, self.mode_routes, self.mode_netns):
            mode_box.pack_start(b, False, False, 0)
        self._mode_signal_ids = [
            self.mode_none.connect("toggled", self._on_mode_toggled),
            self.mode_routes.connect("toggled", self._on_mode_toggled),
            self.mode_netns.connect("toggled", self._on_mode_toggled),
        ]

        self.notebook = Gtk.Notebook()
        right.pack_start(self.notebook, True, True, 0)
        self.notebook.append_page(self._build_routes_tab(), Gtk.Label(label="IP / Subnet rules"))
        self.notebook.append_page(self._build_apps_tab(), Gtk.Label(label="Applications"))
        self.notebook.append_page(self._build_log_tab(), Gtk.Label(label="Log"))

        self._set_detail_sensitive(False)

    def _build_routes_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        mode_row = Gtk.Box(spacing=6)
        mode_row.pack_start(Gtk.Label(label="Behaviour:"), False, False, 0)
        self.split_mode_combo = Gtk.ComboBoxText()
        self.split_mode_combo.append("include_only", "Only listed subnets go through the VPN")
        self.split_mode_combo.append("exclude_listed", "Everything through the VPN except listed subnets")
        self.split_mode_combo.set_active_id("include_only")
        self.split_mode_combo.connect("changed", self._on_routes_changed)
        mode_row.pack_start(self.split_mode_combo, True, True, 0)
        box.pack_start(mode_row, False, False, 0)

        self.cidr_list = Gtk.ListBox()
        cidr_scroll = Gtk.ScrolledWindow()
        cidr_scroll.add(self.cidr_list)
        box.pack_start(cidr_scroll, True, True, 0)

        add_box = Gtk.Box(spacing=6)
        self.cidr_entry = Gtk.Entry()
        self.cidr_entry.set_placeholder_text("e.g. 192.168.1.0/24")
        self.cidr_entry.connect("activate", self._on_add_cidr)
        add_btn = Gtk.Button(label="Add subnet")
        add_btn.connect("clicked", self._on_add_cidr)
        add_box.pack_start(self.cidr_entry, True, True, 0)
        add_box.pack_start(add_btn, False, False, 0)
        box.pack_start(add_box, False, False, 0)

        return box

    def _build_apps_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            "<small>Applications below run inside an isolated network namespace and "
            "only they use the VPN. Use “Launch” once connected.</small>"
        )
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

        self.app_list = Gtk.ListBox()
        app_scroll = Gtk.ScrolledWindow()
        app_scroll.add(self.app_list)
        box.pack_start(app_scroll, True, True, 0)

        add_box = Gtk.Box(spacing=6)
        add_btn = Gtk.Button(label="Add application…")
        add_btn.connect("clicked", self._on_add_app)
        add_box.pack_start(add_btn, False, False, 0)
        box.pack_start(add_box, False, False, 0)

        return box

    def _build_log_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll = Gtk.ScrolledWindow()
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        scroll.add(self.log_view)
        box.pack_start(scroll, True, True, 0)
        return box

    # ---------------------------------------------------------- profiles --

    def _refresh_profile_list(self, select_id: str | None = None) -> None:
        for child in list(self.profile_list.get_children()):
            self.profile_list.remove(child)
        selected_row = None
        for prof in profiles.list_profiles():
            row = Gtk.ListBoxRow()
            row.profile_id = prof.id
            label = Gtk.Label(label=prof.name, xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            row.add(label)
            self.profile_list.add(row)
            if select_id and prof.id == select_id:
                selected_row = row
        self.profile_list.show_all()
        if selected_row:
            self.profile_list.select_row(selected_row)

    def _on_profile_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self.current_profile = None
            self._set_detail_sensitive(False)
            return
        self.current_profile = profiles.Profile.load(row.profile_id)
        self._load_profile_into_ui(self.current_profile)
        self._set_detail_sensitive(True)

    def _load_profile_into_ui(self, prof: profiles.Profile) -> None:
        self.title_label.set_markup(f"<b>{GLib.markup_escape_text(prof.name)}</b>")
        try:
            parsed = parse_ovpn(Path(prof.ovpn_file))
            remotes = ", ".join(f"{h}:{p}/{proto}" for h, p, proto in parsed.remotes) or "unknown"
            self.info_label.set_text(f"Remote: {remotes}")
        except OSError as exc:
            self.info_label.set_text(f"Could not read profile: {exc}")

        self.mode_none.handler_block(self._mode_signal_ids[0])
        self.mode_routes.handler_block(self._mode_signal_ids[1])
        self.mode_netns.handler_block(self._mode_signal_ids[2])
        {
            "none": self.mode_none,
            "routes": self.mode_routes,
            "netns": self.mode_netns,
        }.get(prof.split_type, self.mode_none).set_active(True)
        self.mode_none.handler_unblock(self._mode_signal_ids[0])
        self.mode_routes.handler_unblock(self._mode_signal_ids[1])
        self.mode_netns.handler_unblock(self._mode_signal_ids[2])

        self.split_mode_combo.set_active_id(prof.route_split_mode)
        self._populate_cidr_list(prof.cidrs)
        self._populate_app_list(prof.apps)
        self._refresh_status_for_current()

    def _set_detail_sensitive(self, sensitive: bool) -> None:
        for w in (self.title_label, self.status_label, self.info_label, self.notebook,
                  self.mode_none, self.mode_routes, self.mode_netns, self.connect_btn, self.delete_btn):
            w.set_sensitive(sensitive)
        if not sensitive:
            self.title_label.set_text("Select or import a profile")
            self.status_label.set_text("")
            self.info_label.set_text("")

    # ------------------------------------------------------------- rules --

    def _populate_cidr_list(self, cidrs: list[str]) -> None:
        for child in list(self.cidr_list.get_children()):
            self.cidr_list.remove(child)
        for cidr in cidrs:
            self.cidr_list.add(self._make_removable_row(cidr, self._on_remove_cidr))
        self.cidr_list.show_all()

    def _populate_app_list(self, apps: list[dict]) -> None:
        for child in list(self.app_list.get_children()):
            self.app_list.remove(child)
        for app in apps:
            self.app_list.add(self._make_app_row(app))
        self.app_list.show_all()

    def _make_removable_row(self, text: str, on_remove) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hbox.set_margin_top(4)
        hbox.set_margin_bottom(4)
        hbox.set_margin_start(8)
        hbox.set_margin_end(8)
        label = Gtk.Label(label=text, xalign=0)
        hbox.pack_start(label, True, True, 0)
        remove_btn = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON)
        remove_btn.connect("clicked", lambda _b, t=text: on_remove(t))
        hbox.pack_start(remove_btn, False, False, 0)
        row.add(hbox)
        return row

    def _make_app_row(self, app: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hbox.set_margin_top(4)
        hbox.set_margin_bottom(4)
        hbox.set_margin_start(8)
        hbox.set_margin_end(8)
        label = Gtk.Label(label=f"{app['label']}  —  {app['command']}", xalign=0)
        hbox.pack_start(label, True, True, 0)

        launch_btn = Gtk.Button(label="Launch")
        launch_btn.connect("clicked", lambda _b, a=app: self._on_launch_app(a))
        hbox.pack_start(launch_btn, False, False, 0)

        remove_btn = Gtk.Button.new_from_icon_name("edit-delete-symbolic", Gtk.IconSize.BUTTON)
        remove_btn.connect("clicked", lambda _b, a=app: self._on_remove_app(a))
        hbox.pack_start(remove_btn, False, False, 0)

        row.add(hbox)
        return row

    def _save_profile_rules(self) -> None:
        if self.current_profile:
            self.current_profile.save()

    def _on_mode_toggled(self, btn: Gtk.RadioButton) -> None:
        if not btn.get_active() or not self.current_profile:
            return
        if btn is self.mode_routes:
            self.current_profile.split_type = "routes"
        elif btn is self.mode_netns:
            self.current_profile.split_type = "netns"
        else:
            self.current_profile.split_type = "none"
        self._save_profile_rules()

    def _on_routes_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if not self.current_profile:
            return
        self.current_profile.route_split_mode = self.split_mode_combo.get_active_id() or "include_only"
        self._save_profile_rules()

    def _on_add_cidr(self, _widget) -> None:
        if not self.current_profile:
            return
        text = self.cidr_entry.get_text().strip()
        if not text:
            return
        try:
            normalized = str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            error_dialog(self, f"‘{text}’ is not a valid network: {exc}")
            return
        if normalized not in self.current_profile.cidrs:
            self.current_profile.cidrs.append(normalized)
            self._save_profile_rules()
            self._populate_cidr_list(self.current_profile.cidrs)
        self.cidr_entry.set_text("")

    def _on_remove_cidr(self, cidr: str) -> None:
        if not self.current_profile:
            return
        if cidr in self.current_profile.cidrs:
            self.current_profile.cidrs.remove(cidr)
            self._save_profile_rules()
            self._populate_cidr_list(self.current_profile.cidrs)

    def _on_add_app(self, _widget) -> None:
        if not self.current_profile:
            return
        result = prompt_add_app(self)
        if result is None:
            return
        label, command = result
        self.current_profile.apps.append({"label": label, "command": command})
        self._save_profile_rules()
        self._populate_app_list(self.current_profile.apps)

    def _on_remove_app(self, app: dict) -> None:
        if not self.current_profile:
            return
        if app in self.current_profile.apps:
            self.current_profile.apps.remove(app)
            self._save_profile_rules()
            self._populate_app_list(self.current_profile.apps)

    def _on_launch_app(self, app: dict) -> None:
        if not self.current_session:
            error_dialog(self, "Connect first, then launch applications into the tunnel.")
            return
        try:
            command = shlex.split(app["command"])
            client.run_app(self.current_session, command)
        except client.HelperError as exc:
            error_dialog(self, str(exc))

    # ------------------------------------------------------------ import --

    def _on_import_clicked(self, _widget) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Import OpenVPN profile", transient_for=self, action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Open", Gtk.ResponseType.OK)
        filt = Gtk.FileFilter()
        filt.set_name("OpenVPN config (*.ovpn)")
        filt.add_pattern("*.ovpn")
        dialog.add_filter(filt)

        if dialog.run() == Gtk.ResponseType.OK:
            path = Path(dialog.get_filename())
            try:
                prof = profiles.import_ovpn(path)
            except OSError as exc:
                error_dialog(self, f"Could not import {path.name}: {exc}")
            else:
                self._refresh_profile_list(select_id=prof.id)
        dialog.destroy()

    def _on_delete_clicked(self, _widget) -> None:
        if not self.current_profile:
            return
        confirm = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text=f"Delete profile “{self.current_profile.name}”?",
        )
        response = confirm.run()
        confirm.destroy()
        if response == Gtk.ResponseType.YES:
            profiles.delete_profile(self.current_profile)
            self.current_profile = None
            self._refresh_profile_list()

    # ---------------------------------------------------------- connect --

    def _on_connect_clicked(self, _widget) -> None:
        if self.current_session:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        prof = self.current_profile
        if not prof:
            return

        parsed = parse_ovpn(Path(prof.ovpn_file))
        username = password = None
        if parsed.requires_auth_user_pass and not parsed.auth_user_pass_file:
            creds = ask_credentials(self)
            if creds is None:
                return
            username, password = creds

        rules = {
            "split_type": prof.split_type if prof.split_type in ("routes", "netns") else "none",
            "split_mode": prof.route_split_mode,
            "cidrs": prof.cidrs,
            "apps": prof.apps,
        }

        self.connect_btn.set_sensitive(False)
        self.status_label.set_text("Connecting…")
        try:
            result = client.connect(Path(prof.ovpn_file), prof.name, rules, username, password)
        except client.HelperError as exc:
            error_dialog(self, str(exc))
            self.status_label.set_text("")
        else:
            self.current_session = result.get("session")
        finally:
            self.connect_btn.set_sensitive(True)

    def _disconnect(self) -> None:
        if not self.current_session:
            return
        self.connect_btn.set_sensitive(False)
        try:
            client.disconnect(self.current_session)
        except client.HelperError as exc:
            error_dialog(self, str(exc))
        finally:
            self.current_session = None
            self.connect_btn.set_sensitive(True)
            self.status_label.set_text("Disconnected")

    # --------------------------------------------------------------- tick --

    def _tick(self) -> bool:
        self._refresh_status_for_current()
        return True

    def _refresh_status_for_current(self) -> None:
        if not self.current_session:
            self.connect_btn.set_label("Connect")
            return

        state = client.session_status(self.current_session)
        if state is None:
            self.current_session = None
            self.status_label.set_text("Disconnected")
            self.connect_btn.set_label("Connect")
            return

        status = state.get("status", "unknown")
        pid = state.get("pid")
        if status in ("connecting", "connected") and pid and not Path(f"/proc/{pid}").exists():
            status = "error"

        text = f"Status: {status}"
        if state.get("error"):
            text += f" — {state['error']}"
        self.status_label.set_text(text)
        self.connect_btn.set_label("Disconnect" if status in ("connecting", "connected") else "Connect")

        log_text = client.tail_log(self.current_session)
        buf = self.log_view.get_buffer()
        current = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if current != log_text:
            buf.set_text(log_text)
            self.log_view.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0.0, 0.0)
