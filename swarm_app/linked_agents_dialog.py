"""Choose which installed agent harnesses Open Swarm launches."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .linked_agents import HARNESS_NAMES


class LinkedAgentsDialog(Gtk.Dialog):
    def __init__(self, window):
        super().__init__(title="Linked Agents", transient_for=window,
                         modal=True, destroy_with_parent=True)
        self.owner = window
        self.switches = {}
        self._updating = False
        self._disposed = False
        self.set_default_size(420, -1)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Close", Gtk.ResponseType.CLOSE)

        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(16)
        note = Gtk.Label(label="Choose the agents to start when you click Open Swarm.\n"
                               "Changes apply next time; open tabs stay as they are.", xalign=0)
        note.set_line_wrap(True)
        content.pack_start(note, False, False, 0)
        for harness, name in HARNESS_NAMES.items():
            row = Gtk.Box(spacing=24)
            label = Gtk.Label(label=name, xalign=0)
            row.pack_start(label, True, True, 0)
            switch = Gtk.Switch()
            switch.set_valign(Gtk.Align.CENTER)
            switch.get_accessible().set_name(f"Enable {name}")
            switch.connect("notify::active", self._toggled, harness)
            self.switches[harness] = switch
            row.pack_end(switch, False, False, 0)
            content.pack_start(row, False, False, 0)

        self.feedback = Gtk.Label(xalign=0)
        self.feedback.set_line_wrap(True)
        self.feedback.set_max_width_chars(56)
        self.feedback.get_style_context().add_class("error")
        content.pack_start(self.feedback, False, False, 0)
        self.connect("response", lambda *_: self.destroy())
        self.connect("destroy", self._destroyed)
        self.refresh()
        self.show_all()

    def refresh(self):
        if self._disposed or self._updating:
            return
        self._updating = True
        try:
            settings = self.owner.app.linked_agents
            for harness, switch in self.switches.items():
                switch.set_active(settings.enabled[harness])
            self.feedback.set_text(
                "Could not save or load linked agent settings.\n"
                + settings.load_error if settings.load_error else "")
        finally:
            self._updating = False

    def _toggled(self, switch, _parameter, harness):
        if self._updating or self._disposed:
            return
        self.owner.app.set_linked_agent_enabled(harness, switch.get_active())
        self.refresh()

    def _destroyed(self, *_):
        self._disposed = True
        if self.owner.linked_agents_dialog is self:
            self.owner.linked_agents_dialog = None
