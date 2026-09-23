"""Select particular agents and immediately submit one message to them."""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango

from .session import normalize_message


def _label(text="", *, muted=False):
    widget = Gtk.Label(label=text, xalign=0)
    if muted:
        widget.get_style_context().add_class("muted")
    return widget


def _agent_status(session):
    if session.state == "starting":
        return "Starting"
    if session.agent_busy:
        return "Working"
    if session.agent_idle:
        return "Idle"
    if session.activity is False:
        return "Not ready"
    return "Status unavailable"


class _AgentRow:
    def __init__(self):
        # Keep the displayed identity until the next refresh. A click validates
        # this identity with the owner rather than selecting a replacement CLI.
        self.identity = None
        self.widget = Gtk.ListBoxRow()
        self.widget.set_activatable(False)
        self.widget.set_selectable(False)
        self.checkbox = Gtk.CheckButton()
        self.checkbox.set_border_width(8)
        self.checkbox.set_hexpand(True)
        layout = Gtk.Box(spacing=12)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        details.set_hexpand(True)
        self.title = _label()
        self.title.set_single_line_mode(True)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_width_chars(14)
        self.title.set_max_width_chars(30)
        self.folder = _label(muted=True)
        self.folder.set_single_line_mode(True)
        self.folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.folder.set_max_width_chars(42)
        details.pack_start(self.title, False, False, 0)
        details.pack_start(self.folder, False, False, 0)
        self.status = _label()
        self.status.set_xalign(1)
        self.status.set_width_chars(18)
        layout.pack_start(details, True, True, 0)
        layout.pack_end(self.status, False, False, 0)
        self.checkbox.add(layout)
        self.widget.add(self.checkbox)


class CustomBroadcastDialog(Gtk.Dialog):
    def __init__(self, window):
        super().__init__(title="Custom Broadcast", transient_for=window,
                         modal=True, destroy_with_parent=True)
        self.owner = window
        self.rows = {}
        self.refresh_source = 0
        self._updating = False
        self._disposed = False
        self._send_error = ""
        self.set_default_size(640, 560)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.send_button = self.add_button("Send to 0 agents", Gtk.ResponseType.OK)
        self.send_button.get_style_context().add_class("suggested-action")

        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)
        note = _label("Send to checked agents, including working agents.\n"
                      "Selections are remembered within this window.")
        note.set_line_wrap(True)
        content.pack_start(note, False, False, 0)

        controls = Gtk.Box(spacing=8)
        self.select_all_button = Gtk.Button(label="Select all")
        self.select_idle_button = Gtk.Button(label="Select idle")
        self.clear_button = Gtk.Button(label="Clear selection")
        for button, mode in ((self.select_all_button, "all"),
                             (self.select_idle_button, "idle"),
                             (self.clear_button, "none")):
            button.connect("clicked", lambda _button, mode=mode: self._select(mode))
            controls.pack_start(button, False, False, 0)
        self.summary = _label("0 selected", muted=True)
        controls.pack_end(self.summary, False, False, 0)
        content.pack_start(controls, False, False, 0)

        self.agent_list = Gtk.ListBox()
        self.agent_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.agent_list.get_accessible().set_name("Agents for custom broadcast")
        empty = _label("No agents are open in this window.\nOpen one with Swarm → New Agent.",
                       muted=True)
        empty.set_line_wrap(True)
        empty.set_justify(Gtk.Justification.CENTER)
        empty.set_xalign(0.5)
        empty.set_margin_top(16)
        empty.set_margin_bottom(16)
        self.agent_list.set_placeholder(empty)
        empty.show()
        agents_scroll = Gtk.ScrolledWindow()
        agents_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        agents_scroll.set_shadow_type(Gtk.ShadowType.IN)
        agents_scroll.set_min_content_height(120)
        agents_scroll.add(self.agent_list)
        content.pack_start(agents_scroll, True, True, 0)

        self.editor = Gtk.TextView()
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.set_accepts_tab(True)
        self.editor.set_left_margin(10)
        self.editor.set_right_margin(10)
        self.editor.set_top_margin(10)
        self.editor.set_bottom_margin(10)
        self.editor.get_style_context().add_class("broadcast-editor")
        self.editor.get_accessible().set_name("Custom broadcast message")
        editor_scroll = Gtk.ScrolledWindow()
        editor_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        editor_scroll.set_shadow_type(Gtk.ShadowType.IN)
        editor_scroll.set_min_content_height(90)
        editor_scroll.add(self.editor)
        content.pack_start(editor_scroll, True, True, 0)
        self.feedback = _label("Ctrl+Enter to send · Enter for a new line", muted=True)
        self.feedback.set_line_wrap(True)
        content.pack_start(self.feedback, False, False, 0)

        self.editor.get_buffer().connect("changed", self._message_changed)
        self.editor.connect("key-press-event", self._key_press)
        self.connect("response", self._response)
        self.connect("destroy", self._destroyed)
        self.refresh_source = GLib.timeout_add(250, self._refresh)
        self._refresh()
        self.show_all()
        self.editor.grab_focus()

    def text(self):
        buffer = self.editor.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def _message_changed(self, *_):
        self._send_error = ""
        self._refresh()

    def _select(self, mode):
        if self._disposed:
            return
        self._send_error = ""
        self.owner.select_custom_agents(mode)
        self._refresh()

    def _toggled(self, checkbox, session):
        if self._updating or self._disposed:
            return
        row = self.rows.get(session)
        if row is not None and row.identity is not None:
            self._send_error = ""
            self.owner.set_custom_selected(session, row.identity, checkbox.get_active())
        self._refresh()

    def _refresh(self):
        if self._disposed:
            return GLib.SOURCE_REMOVE
        if self._updating:
            return GLib.SOURCE_CONTINUE
        self._updating = True
        try:
            sessions = list(self.owner.custom_agents())
            live = set(sessions)
            for session in tuple(self.rows):
                if session not in live:
                    self.rows.pop(session).widget.destroy()
            selected = dict(self.owner.custom_selection)
            count = 0
            available = False
            idle_available = False
            for session in sessions:
                row = self.rows.get(session)
                if row is None:
                    row = _AgentRow()
                    self.rows[session] = row
                    row.checkbox.connect("toggled", self._toggled, session)
                    self.agent_list.add(row.widget)
                    row.widget.show_all()
                # Existing rows retain their positions even when agent tabs
                # move, their names change, or an agent begins/finishes work.
                row.identity = session.broadcast_identity
                usable = row.identity is not None
                checked = usable and selected.get(session) == row.identity
                row.checkbox.set_sensitive(usable)
                row.checkbox.set_active(checked)
                row.title.set_text(session.title)
                row.title.set_tooltip_text(session.title)
                row.folder.set_text(session.directory)
                row.folder.set_tooltip_text(session.directory)
                status = _agent_status(session)
                row.status.set_text(status)
                row.checkbox.get_accessible().set_name(f"Select {session.title}: {status}")
                count += bool(checked)
                available = available or usable
                idle_available = idle_available or (usable and session.agent_idle)
            self.select_all_button.set_sensitive(available)
            self.select_idle_button.set_sensitive(idle_available)
            self.clear_button.set_sensitive(bool(self.owner.custom_selection))
            self.summary.set_text(f"{count} selected")
            self.send_button.set_label(f"Send to {count} agent{'s' if count != 1 else ''}")
            valid = False
            feedback = self._send_error or "Ctrl+Enter to send · Enter for a new line"
            message = self.text()
            if message.strip():
                try:
                    normalize_message(message)
                    valid = True
                except ValueError as error:
                    feedback = str(error)
            self.feedback.set_text(feedback)
            self.send_button.set_sensitive(count > 0 and valid)
        finally:
            self._updating = False
        return GLib.SOURCE_CONTINUE

    def _key_press(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.CONTROL_MASK:
            self._refresh()
            if self.send_button.get_sensitive():
                self.response(Gtk.ResponseType.OK)
            return True
        return False

    def _response(self, _dialog, response):
        if response == Gtk.ResponseType.OK:
            self._refresh()
            if not self.send_button.get_sensitive():
                return
            if not self.owner.broadcast(self.text(), recipients=dict(self.owner.custom_selection)):
                self._send_error = "Could not send. Review the selection and try again."
                self._refresh()
                return
        self.destroy()

    def _destroyed(self, *_):
        self._disposed = True
        if self.refresh_source:
            GLib.source_remove(self.refresh_source)
            self.refresh_source = 0
        if self.owner.custom_broadcast_dialog is self:
            self.owner.custom_broadcast_dialog = None
