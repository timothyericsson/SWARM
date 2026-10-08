"""Edit the ordered startup commands launched by Open Swarm."""

from uuid import uuid4

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .linked_agents import StartupAgent


class StartupAgentRow(Gtk.Box):
    def __init__(self, agent, remove):
        super().__init__(spacing=10)
        self.agent_id = agent.id
        self.enabled = Gtk.CheckButton()
        self.enabled.set_active(agent.enabled)
        self.enabled.set_tooltip_text("Include this agent when opening a swarm")
        self.enabled.get_accessible().set_name("Enable agent")
        self.pack_start(self.enabled, False, False, 0)

        self.name_entry = Gtk.Entry()
        self.name_entry.set_text(agent.name)
        self.name_entry.set_placeholder_text("Agent name")
        self.name_entry.set_width_chars(20)
        self.name_entry.get_accessible().set_name("Agent name")
        self.pack_start(self.name_entry, False, False, 0)

        self.command_entry = Gtk.Entry()
        self.command_entry.set_text(agent.command)
        self.command_entry.set_placeholder_text("Startup command")
        self.command_entry.set_hexpand(True)
        self.command_entry.get_accessible().set_name("Startup command")
        self.pack_start(self.command_entry, True, True, 0)

        self.remove_button = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
        self.remove_button.set_tooltip_text("Remove agent")
        self.remove_button.get_accessible().set_name("Remove agent")
        self.remove_button.connect("clicked", lambda *_: remove(self))
        self.pack_start(self.remove_button, False, False, 0)

    def get_agent(self):
        return StartupAgent(id=self.agent_id, name=self.name_entry.get_text(),
                            command=self.command_entry.get_text(),
                            enabled=self.enabled.get_active())


class LinkedAgentsDialog(Gtk.Dialog):
    def __init__(self, window):
        super().__init__(title="Startup Swarm", transient_for=window,
                         modal=True, destroy_with_parent=True)
        self.owner = window
        self.rows = []
        self._baseline = None
        self._saving = False
        self._disposed = False
        self.set_default_size(900, 390)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)
        note = Gtk.Label(label="Choose the agents and startup commands for Open Swarm.\n"
                               "Agents open in the order shown below.", xalign=0)
        note.set_line_wrap(True)
        content.pack_start(note, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(140)
        self.row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.row_box.set_valign(Gtk.Align.START)
        scroll.add(self.row_box)
        content.pack_start(scroll, True, True, 0)

        self.add_button_widget = Gtk.Button.new_with_label("+ Add agent")
        self.add_button_widget.set_halign(Gtk.Align.START)
        self.add_button_widget.connect("clicked", lambda *_: self.add_agent())
        content.pack_start(self.add_button_widget, False, False, 0)
        hint = Gtk.Label(label="Commands run in the current folder. Quote arguments containing spaces.\n"
                               "For shell expansion or pipelines, use bash -lc 'your command'.", xalign=0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        content.pack_start(hint, False, False, 0)

        self.dont_show_again = Gtk.CheckButton.new_with_label("Don't show again at startup")
        content.pack_start(self.dont_show_again, False, False, 0)
        self.feedback = Gtk.Label(xalign=0)
        self.feedback.set_line_wrap(True)
        self.feedback.set_max_width_chars(90)
        self.feedback.get_style_context().add_class("error")
        content.pack_start(self.feedback, False, False, 0)

        self.connect("response", self._response)
        self.connect("destroy", self._destroyed)
        self.refresh()
        self.show_all()

    def _saved_state(self):
        settings = self.owner.app.linked_agents
        return tuple(settings.agents), settings.dont_show_again

    def _draft_state(self):
        return tuple(row.get_agent() for row in self.rows), self.dont_show_again.get_active()

    def _show_conflict(self):
        self.feedback.set_text("Startup Swarm changed in another window. Your edits are still here. "
                               "Close and reopen this window to load the saved settings before editing again.")

    def refresh(self):
        if self._disposed or self._saving:
            return
        saved = self._saved_state()
        if self._baseline is not None and self._draft_state() != self._baseline:
            if saved != self._baseline:
                self._show_conflict()
            else:
                self.feedback.set_text(self.owner.app.linked_agents.load_error or "")
            return

        for row in self.rows:
            row.destroy()
        self.rows.clear()
        for agent in saved[0]:
            self._append_row(agent)
        self.dont_show_again.set_active(saved[1])
        self._baseline = saved
        self.feedback.set_text(self.owner.app.linked_agents.load_error or "")

    def _append_row(self, agent):
        row = StartupAgentRow(agent, self._remove_row)
        self.rows.append(row)
        self.row_box.pack_start(row, False, False, 0)
        row.show_all()
        return row

    def add_agent(self):
        row = self._append_row(StartupAgent(id=uuid4().hex, name="", command=""))
        row.name_entry.grab_focus()
        return row

    def _remove_row(self, row):
        self.rows.remove(row)
        row.destroy()

    def _response(self, _dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        if self._saved_state() != self._baseline:
            self._show_conflict()
            return
        agents, dont_show_again = self._draft_state()
        self._saving = True
        try:
            saved = self.owner.app.save_startup_swarm(list(agents), dont_show_again)
        finally:
            self._saving = False
        if saved:
            self.destroy()
        else:
            self.feedback.set_text(self.owner.app.linked_agents.load_error or
                                   "Could not save startup swarm settings.")

    def _destroyed(self, *_):
        self._disposed = True
        if self.owner.linked_agents_dialog is self:
            self.owner.linked_agents_dialog = None
