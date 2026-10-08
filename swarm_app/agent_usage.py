"""Usage badges for editable startup agents, with cancellable account-bound reads."""

from dataclasses import dataclass, field
import hashlib
import math
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from .linked_agents import uses_default_usage_account
from .provider_usage import SUPPORTED_PROVIDERS, ProviderUsageSnapshot, fetch_provider_usage
from .usage import UsageUnavailable


LIVE_PROVIDERS = SUPPORTED_PROVIDERS


@dataclass
class AgentUsageState:
    agent: object
    provider: str | None
    fingerprint: str | None
    api_key: str | None = field(repr=False)
    credential_error: str | None = None
    snapshot: object = None
    detail: str = "Usage has not been checked."
    pending: bool = False
    cancel: object = None


class AgentUsageManager:
    def __init__(self, on_change):
        self.on_change = on_change
        self.states = {}

    def configure(self, agents, *, errors=None):
        """Replace changed credentials before accepting any new worker results."""
        states = {}
        changed = []
        for agent, provider, credential in agents:
            error = (errors or {}).get(agent.id)
            secret = credential.secret if credential is not None else None
            fingerprint = hashlib.sha256(secret.encode()).hexdigest() if secret else None
            state = self.states.get(agent.id)
            if state is None or (state.provider, state.fingerprint, state.credential_error) != (provider, fingerprint, error):
                if state is not None and state.cancel is not None:
                    state.cancel.set()
                state = AgentUsageState(agent, provider, fingerprint, secret, credential_error=error)
                changed.append(agent.id)
            else:
                state.agent = agent
            states[agent.id] = state
        for identity, state in self.states.items():
            if identity not in states and state.cancel is not None:
                state.cancel.set()
        self.states = states
        self.on_change()
        for identity in changed:
            self.refresh(identity)

    def refresh(self, identity=None):
        if identity is None:
            for key in tuple(self.states):
                self.refresh(key)
            return
        state = self.states.get(identity)
        if state is None or state.pending or state.provider == "codex":
            return
        if state.credential_error:
            state.snapshot = ProviderUsageSnapshot(None, state.credential_error +
                                                   " Update this agent's API key.", "Update key")
            self.on_change()
            return
        if state.provider not in LIVE_PROVIDERS:
            state.snapshot = ProviderUsageSnapshot(
                None, "Automatic remaining-usage tracking is not available for this provider in SWARM. "
                "Check its billing dashboard.", "N/A")
            self.on_change()
            return
        state.pending = True
        state.cancel = cancel = threading.Event()
        self.on_change()

        def fetch():
            snapshot, error = None, None
            try:
                snapshot = fetch_provider_usage(state.provider, state.api_key, cancel=cancel)
            except UsageUnavailable as exc:
                error = str(exc)
            except Exception:
                error = "Usage unavailable. Click to try again."
            GLib.idle_add(self._finished, identity, state, cancel, snapshot, error)

        threading.Thread(target=fetch, daemon=False).start()

    def invalidate(self, provider, *, implicit_only=False):
        """Discard work that may have read an externally updated fallback key."""
        identities = []
        for identity, state in self.states.items():
            if state.provider != provider or (implicit_only and state.api_key is not None):
                continue
            if state.cancel is not None:
                state.cancel.set()
            state.pending = False
            state.cancel = None
            state.snapshot = None
            state.detail = "Credentials changed. Checking usage…"
            identities.append(identity)
        self.on_change()
        for identity in identities:
            self.refresh(identity)

    def _finished(self, identity, state, cancel, snapshot, error):
        if self.states.get(identity) is not state or state.cancel is not cancel or cancel.is_set():
            return GLib.SOURCE_REMOVE
        state.pending = False
        state.cancel = None
        state.snapshot = snapshot if error is None else None
        state.detail = error or "Usage unavailable."
        self.on_change()
        return GLib.SOURCE_REMOVE

    def close(self):
        for state in self.states.values():
            if state.cancel is not None:
                state.cancel.set()
        self.states.clear()


class AgentUsageBadge(Gtk.Button):
    def __init__(self, app, identity):
        super().__init__()
        self.app = app
        self.identity = identity
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_focus_on_click(False)
        self.get_style_context().add_class("usage-badge")
        self.panel = Gtk.Popover.new(self)
        self.panel.set_position(Gtk.PositionType.BOTTOM)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        self.details = Gtk.Label(xalign=0)
        self.details.set_line_wrap(True)
        self.details.set_max_width_chars(48)
        content.pack_start(self.details, False, False, 0)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.connect("clicked", self._refresh)
        content.pack_start(self.refresh_button, False, False, 0)
        self.panel.add(content)
        content.show_all()
        self.connect("clicked", self._clicked)
        self.connect("destroy", lambda *_: self.panel.destroy())

    def _refresh(self, *_):
        state = self.app.agent_usage.states.get(self.identity)
        if state is not None and state.provider == "codex":
            self.app.refresh_usage()
        else:
            self.app.agent_usage.refresh(self.identity)

    def _clicked(self, *_):
        if self.panel.get_visible():
            self.panel.popdown()
            return
        self.panel.popup()
        self._refresh()

    def update(self, state):
        if state.provider == "codex":
            snapshot = self.app.usage_snapshot
            primary = snapshot.primary if snapshot is not None else None
            percent = primary.remaining if primary is not None else None
            value = f"{math.floor(percent)}%" if percent is not None else "—%"
            detail = "Shared Codex ChatGPT plan usage. " + (self.app.usage_status if percent is None else value + " remaining.")
            pending = self.app.usage_pending
            can_refresh = not self.app.auth_busy() and self.app.auth_state in {"chatgpt", "other"}
        else:
            snapshot = state.snapshot
            percent = snapshot.percent_remaining if snapshot is not None else None
            value = f"{math.floor(percent)}%" if percent is not None else (
                snapshot.label if snapshot is not None and snapshot.label else "—")
            detail = snapshot.detail if snapshot is not None else state.detail
            detail += "\nUsage is shared by agents using this provider account/key."
            pending = state.pending
            can_refresh = state.provider in LIVE_PROVIDERS
        short_name = state.agent.name if len(state.agent.name) <= 14 else state.agent.name[:13] + "…"
        self.set_label(f"{short_name} {value}")
        description = state.agent.name + "\n\n" + detail + ("\nRefreshing…" if pending else "")
        self.details.set_text(description)
        self.set_tooltip_text(description)
        self.get_accessible().set_name(f"{state.agent.name} usage: {value}")
        self.get_accessible().set_description(detail)
        self.refresh_button.set_sensitive(can_refresh and not pending)
