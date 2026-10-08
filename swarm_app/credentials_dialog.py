"""Add provider or startup-agent API keys without displaying saved secrets."""

import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from .credentials import PROVIDERS, parse_key, provider_for_agent
from .linked_agents import command_harness, parse_startup_command


class CredentialsDialog(Gtk.Dialog):
    def __init__(self, window):
        super().__init__(title="Add Key", transient_for=window,
                         modal=True, destroy_with_parent=True)
        self.owner = window
        self._disposed = False
        self._updating = False
        self._agents = {}
        self.saved_rows = []
        self.set_default_size(660, 580)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.save_button = self.add_button("Save Key", Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)
        intro = Gtk.Label(label="Paste an API key or an API_KEY=value assignment. "
                                "Choose a provider or startup agent when the key is ambiguous.", xalign=0)
        intro.set_line_wrap(True)
        intro.set_max_width_chars(76)
        content.pack_start(intro, False, False, 0)

        form = Gtk.Grid(column_spacing=12, row_spacing=10)
        form.attach(Gtk.Label(label="API key", xalign=0), 0, 0, 1, 1)
        self.key_entry = Gtk.Entry()
        self.key_entry.set_visibility(False)
        self.key_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.key_entry.set_placeholder_text("Paste your key")
        self.key_entry.set_hexpand(True)
        self.key_entry.get_accessible().set_name("API key")
        form.attach(self.key_entry, 1, 0, 1, 1)
        self.show_key = Gtk.CheckButton.new_with_label("Show key")
        self.show_key.connect("toggled", lambda button: self.key_entry.set_visibility(button.get_active()))
        form.attach(self.show_key, 1, 1, 1, 1)

        form.attach(Gtk.Label(label="Provider", xalign=0), 0, 2, 1, 1)
        self.provider = Gtk.ComboBoxText()
        self.provider.append("auto", "Auto detect")
        for provider in PROVIDERS.values():
            self.provider.append(provider.id, provider.name)
        self.provider.set_active_id("auto")
        self.provider.get_accessible().set_name("API key provider")
        form.attach(self.provider, 1, 2, 1, 1)

        form.attach(Gtk.Label(label="Use for", xalign=0), 0, 3, 1, 1)
        self.target = Gtk.ComboBoxText()
        self.target.get_accessible().set_name("API key target")
        form.attach(self.target, 1, 3, 1, 1)

        self.variable_label = Gtk.Label(label="Environment variable", xalign=0)
        self.variable_label.set_no_show_all(True)
        self.variable = Gtk.Entry()
        self.variable.set_placeholder_text("MY_SERVICE_API_KEY")
        self.variable.get_accessible().set_name("API key environment variable")
        self.variable.set_no_show_all(True)
        form.attach(self.variable_label, 0, 4, 1, 1)
        form.attach(self.variable, 1, 4, 1, 1)
        content.pack_start(form, False, False, 0)

        self.detection = Gtk.Label(xalign=0)
        self.detection.set_line_wrap(True)
        self.detection.set_max_width_chars(76)
        content.pack_start(self.detection, False, False, 0)
        note = Gtk.Label(label="Saved keys apply to newly opened or restarted agents. "
                               "Running agents keep their current key.", xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(76)
        note.get_style_context().add_class("dim-label")
        content.pack_start(note, False, False, 0)

        content.pack_start(Gtk.Separator(), False, False, 0)
        content.pack_start(Gtk.Label(label="Saved keys", xalign=0), False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(100)
        self.saved_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.saved_box.set_valign(Gtk.Align.START)
        scroll.add(self.saved_box)
        content.pack_start(scroll, True, True, 0)

        self.feedback = Gtk.Label(xalign=0)
        self.feedback.set_line_wrap(True)
        self.feedback.set_max_width_chars(76)
        content.pack_start(self.feedback, False, False, 0)
        for widget, signal in ((self.key_entry, "changed"), (self.provider, "changed"),
                               (self.target, "changed"), (self.variable, "changed")):
            widget.connect(signal, self._draft_changed)
        self.connect("response", self._response)
        self.connect("destroy", self._destroyed)
        self.refresh()
        if self.owner.app.credential_error:
            self._feedback(self.owner.app.credential_error, error=True)
        self.show_all()
        self._update_detection()
        self.key_entry.grab_focus()

    def refresh(self):
        """Update metadata while preserving the key and selected target draft."""
        if self._disposed:
            return
        target = self.target.get_active_id() or "default"
        self._updating = True
        try:
            self._agents = {agent.id: agent for agent in self.owner.app.linked_agents.agents}
            self.target.remove_all()
            self.target.append("default", "Provider default (all matching agents)")
            for agent in self._agents.values():
                self.target.append("agent:" + agent.id, agent.name)
            if target.startswith("agent:") and target[6:] not in self._agents:
                self.target.append(target, "Startup agent no longer exists")
            self.target.set_active_id(target)
            for child in self.saved_box.get_children():
                child.destroy()
            self.saved_rows.clear()
            for record in self.owner.app.credentials.records:
                row = Gtk.Box(spacing=8)
                provider_name = PROVIDERS[record.provider].name
                agent = self._agents.get(record.agent_id)
                scope = agent.name if agent else "Removed startup agent" if record.agent_id else "Provider default"
                detail = f"{provider_name} · {scope}"
                if record.provider == "custom":
                    detail += f" · {record.variable}"
                row.label = Gtk.Label(label=detail, xalign=0)
                row.label.set_line_wrap(True)
                row.label.set_max_width_chars(46)
                row.pack_start(row.label, True, True, 0)
                row.replace_button = Gtk.Button(label="Replace")
                row.replace_button.set_sensitive(record.agent_id is None or agent is not None)
                row.replace_button.connect("clicked", self._replace, record)
                row.pack_start(row.replace_button, False, False, 0)
                row.remove_button = Gtk.Button(label="Remove")
                row.remove_button.connect("clicked", self._remove, record)
                row.pack_start(row.remove_button, False, False, 0)
                self.saved_box.pack_start(row, False, False, 0)
                self.saved_rows.append(row)
            if not self.saved_rows:
                self.saved_box.pack_start(Gtk.Label(label="No keys saved in SWARM.", xalign=0), False, False, 0)
            self.saved_box.show_all()
        finally:
            self._updating = False
        self._update_detection()

    def _selected_agent(self):
        target = self.target.get_active_id() or "default"
        if target == "default":
            return None
        agent = next((agent for agent in self.owner.app.linked_agents.agents
                      if agent.id == target[6:]), None)
        if agent is None:
            raise ValueError("The selected startup agent no longer exists. Choose another target.")
        return agent

    def _resolved_input(self):
        parsed = parse_key(self.key_entry.get_text())
        agent = self._selected_agent()
        target_provider = provider_for_agent(agent) if agent is not None else None
        choice = self.provider.get_active_id() or "auto"
        provider = choice if choice != "auto" else parsed.provider or target_provider
        if provider is None:
            raise ValueError("This key does not identify its provider. Choose a provider or a startup agent.")
        if parsed.provider is not None and parsed.provider != provider and provider != "custom":
            raise ValueError(f"This key identifies {PROVIDERS[parsed.provider].name}. "
                             "Choose that provider or Auto detect.")
        if agent is not None:
            harness = command_harness(parse_startup_command(agent.command),
                                      codex=getattr(self.owner.app, "codex", "codex"),
                                      hermes=getattr(self.owner.app, "hermes", "hermes"))
            if harness == "codex" and provider != "openai":
                raise ValueError("Codex startup agents use OpenAI keys. Choose a matching startup agent.")
            if target_provider is not None and target_provider != provider and provider != "custom":
                raise ValueError(f"The selected startup agent uses {PROVIDERS[target_provider].name}. "
                                 "Choose a matching provider or a different target.")
        variable = None
        if provider == "custom":
            if agent is None:
                raise ValueError("Choose a startup agent for a custom environment variable.")
            variable = self.variable.get_text().strip() or parsed.variable
            if not variable or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                raise ValueError("Enter the environment variable used by this startup command.")
        return parsed, provider, agent, variable

    def _draft_changed(self, *_):
        if self._updating or self._disposed:
            return
        self._feedback("")
        self._update_detection()

    def _update_detection(self):
        custom = self.provider.get_active_id() == "custom"
        try:
            parsed = parse_key(self.key_entry.get_text())
            custom = custom or (self.provider.get_active_id() == "auto" and parsed.provider == "custom")
            if parsed.variable and custom:
                self.variable.set_placeholder_text(parsed.variable)
        except ValueError:
            pass
        self.variable.set_visible(custom)
        self.variable_label.set_visible(custom)
        try:
            _, provider, agent, variable = self._resolved_input()
        except ValueError as error:
            self.save_button.set_sensitive(False)
            self.detection.set_text(str(error) if self.key_entry.get_text() else
                                    "Keys are identified locally when their format is unambiguous.")
            return
        scope = f"startup agent {agent.name}" if agent is not None else "all matching startup agents"
        detail = f"Save {PROVIDERS[provider].name} key for {scope}."
        if variable:
            detail += f" Environment variable: {variable}."
        existing = next((record for record in self.owner.app.credentials.records
                         if record.agent_id == (agent.id if agent is not None else None)
                         and (agent is not None or record.provider == provider)), None)
        if existing is not None:
            detail += " This replaces the saved key for this target."
        self.detection.set_text(detail)
        self.save_button.set_sensitive(True)

    def _feedback(self, text, *, error=False):
        self.feedback.set_text(text)
        style = self.feedback.get_style_context()
        style.add_class("error") if error else style.remove_class("error")

    def _response(self, _dialog, response):
        if response != Gtk.ResponseType.OK:
            self.destroy()
            return
        try:
            parsed, provider, agent, variable = self._resolved_input()
        except ValueError as error:
            self._feedback(str(error), error=True)
            return
        if not self.owner.app.save_api_key(provider, parsed.secret,
                                           agent_id=agent.id if agent is not None else None, variable=variable):
            self._feedback(self.owner.app.credential_error or "Could not save the key.", error=True)
            return
        self.key_entry.set_text("")
        self.show_key.set_active(False)
        self.refresh()
        self._feedback("Key saved. Open or restart an agent to use it.")

    def _replace(self, _button, record):
        self.provider.set_active_id(record.provider)
        self.target.set_active_id("agent:" + record.agent_id if record.agent_id else "default")
        self.variable.set_text(record.variable or "")
        self.key_entry.set_text("")
        self.show_key.set_active(False)
        self._feedback("Paste the replacement key, then choose Save Key.")
        self.key_entry.grab_focus()

    def _remove(self, _button, record):
        if self.owner.app.delete_api_key(record.provider, agent_id=record.agent_id):
            self.refresh()
            self._feedback("Saved key removed. Running agents keep their current key.")
        else:
            self._feedback(self.owner.app.credential_error or "Could not remove the key.", error=True)

    def _destroyed(self, *_):
        self._disposed = True
        self.key_entry.set_text("")
        if self.owner.credentials_dialog is self:
            self.owner.credentials_dialog = None
