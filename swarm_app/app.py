"""GTK interface. Each window owns its own set of terminal sessions."""

from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
import math
import os
import pwd
import shutil
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, Vte

from . import __version__
from .custom_broadcast import CustomBroadcastDialog
from .harnesses import DEFAULT_HARNESS, HARNESS_ORDER, HARNESSES
from .linked_agents import HARNESS_NAMES, LinkedAgentsSettings
from .linked_agents_dialog import LinkedAgentsDialog
from .notifications import notify_agents_finished
from .session import TerminalSession, normalize_message
from .usage import UsageUnavailable, fetch_usage


CSS = b"""
window.swarm { background-color: #17191f; }
menubar { padding: 3px 6px; background-color: #252830; }
menubar > menuitem { padding: 5px 10px; }
#workspace { background-color: #14161b; }
notebook > header { background-color: #20232b; padding: 0; }
notebook > header > tabs > tab { padding: 6px 10px; min-height: 24px; }
notebook > header > tabs > tab:checked { border-top: 2px solid #88c5ad; }
.tab-close { padding: 1px; min-height: 18px; min-width: 18px; border: none; }
.agent-spinner { color: #9ed8bd; }
.status { padding: 5px 12px; background-color: #20232b; font-size: 12px; }
.status label { color: #b4bac8; }
.broadcast-editor text { background-color: #14161b; color: #e8edf5; }
.broadcast-editor { padding: 10px; }
.muted { color: #a6adba; }
.error { color: #f2a0a0; }
.killed-tab { color: #ff6b6b; }
.swarm-header { min-height: 28px; padding: 3px 8px; background-color: #252830; }
.usage-badge { padding: 2px 5px; min-height: 20px; min-width: 36px; font-weight: bold; }
.usage-badge label { color: #9ed8bd; }
.usage-badge.usage-unknown label { color: #a6adba; }
.usage-badge.usage-low label { color: #ff6b6b; }
"""

BROADCAST_READY_SECONDS = 1.0


@dataclass(eq=False)
class BroadcastCompletion:
    # Bind each recipient to its original process, including agents launched
    # manually inside a shell that can later host a different Codex process.
    recipients: frozenset[TerminalSession]
    identities: dict[TerminalSession, tuple[int, int, int]]
    submitted: set[TerminalSession] = field(default_factory=set)
    started: set[TerminalSession] = field(default_factory=set)
    busy_titles: dict[TerminalSession, str | None] = field(default_factory=dict)
    ready_since: float | None = None


def color(value):
    result = Gdk.RGBA()
    result.parse(value)
    return result


def label(text, style=None):
    widget = Gtk.Label(label=text, xalign=0)
    if style:
        widget.get_style_context().add_class(style)
    return widget


def usage_window_name(minutes):
    if minutes == 10080:
        return "Weekly"
    if minutes is None:
        return "Current limit"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}-day"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour"
    return f"{minutes}-minute"


def local_usage_time(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime("%a %d %b, %H:%M")
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def usage_reset_countdown(timestamp):
    if timestamp is None:
        return None
    seconds = timestamp - time.time()
    if seconds <= 0:
        return "Reset due"
    if seconds < 60:
        return "in less than a minute"
    minutes = math.ceil(seconds / 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and len(parts) < 2:
        parts.append(f"{minutes}m")
    return "in " + " ".join(parts)


class BroadcastDialog(Gtk.Dialog):
    def __init__(self, window, *, idle_only=False):
        super().__init__(title="Sleeper Broadcast" if idle_only else "Global Broadcast", transient_for=window,
                         modal=True, destroy_with_parent=True)
        self.owner = window
        self.idle_only = idle_only
        self.set_default_size(560, 390)
        self.set_resizable(True)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.send_button = self.add_button("Send to all agents", Gtk.ResponseType.OK)
        self.send_button.get_style_context().add_class("suggested-action")
        content = self.get_content_area()
        content.set_border_width(16)
        content.set_spacing(12)
        self.summary = label("")
        self.summary.set_line_wrap(True)
        content.pack_start(self.summary, False, False, 0)
        self.editor = Gtk.TextView()
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.get_style_context().add_class("broadcast-editor")
        self.editor.set_accepts_tab(True)
        self.editor.set_left_margin(10)
        self.editor.set_right_margin(10)
        self.editor.set_top_margin(10)
        self.editor.set_bottom_margin(10)
        self.editor.get_accessible().set_name("Broadcast message")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(self.editor)
        content.pack_start(scroll, True, True, 0)
        self.notification_options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.notify_checkbox = Gtk.CheckButton(label="Notify me when these agents finish")
        self.notification_options.pack_start(self.notify_checkbox, False, False, 0)
        self.notify_help = label("", "muted")
        self.notify_help.set_line_wrap(True)
        self.notification_options.pack_start(self.notify_help, False, False, 0)
        content.pack_start(self.notification_options, False, False, 0)
        self.feedback = label("Ctrl+Enter to send · Enter for a new line", "muted")
        self.feedback.set_line_wrap(True)
        content.pack_start(self.feedback, False, False, 0)
        self.editor.get_buffer().connect("changed", self._changed)
        self.editor.connect("key-press-event", self._key_press)
        self.connect("response", self._response)
        self.connect("destroy", self._destroyed)
        self.refresh_source = GLib.timeout_add(250, self._refresh)
        self.show_all()
        self.notification_options.set_no_show_all(True)
        self._refresh()
        self.editor.grab_focus()

    def text(self):
        buffer = self.editor.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def _changed(self, *_):
        self._refresh()

    def set_mode(self, *, idle_only):
        self.idle_only = idle_only
        self.set_title("Sleeper Broadcast" if idle_only else "Global Broadcast")
        self._refresh()

    def _refresh(self):
        targets = self.owner.ready_agents() if self.idle_only else self.owner.active_agents()
        count = len(targets)
        can_notify = not self.idle_only and bool(targets) and all(
            session.spec.finish_notifications for session in targets)
        self.notification_options.set_visible(not self.idle_only)
        self.notify_checkbox.set_sensitive(can_notify)
        if not can_notify:
            self.notify_checkbox.set_active(False)
        self.notify_help.set_text(
            "One desktop notification when all recipients finish working." if can_notify else
            "Completion notifications are available for Codex-only broadcasts." if targets else
            "No agents can receive a broadcast yet.")
        recipients = f"{count} {'idle ' if self.idle_only else ''}agent{'s' if count != 1 else ''}"
        summary = f"Send immediately to {recipients} in this window."
        if self.idle_only:
            summary += ("\nOnly agents ready for a new message receive it. "
                        "Idle detection requires Codex opened through SWARM. "
                        "Agents with unavailable status are skipped.")
        self.summary.set_text(summary)
        self.send_button.set_label(f"Send to {recipients}")
        valid = False
        message = self.text()
        if message.strip():
            try:
                normalize_message(message)
                valid = True
                self.feedback.set_text("Ctrl+Enter to send · Enter for a new line")
            except ValueError as error:
                self.feedback.set_text(str(error))
        else:
            self.feedback.set_text("Ctrl+Enter to send · Enter for a new line")
        self.send_button.set_sensitive(count > 0 and valid)
        return GLib.SOURCE_CONTINUE

    def _key_press(self, _widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.CONTROL_MASK:
            if self.send_button.get_sensitive():
                self.response(Gtk.ResponseType.OK)
            return True
        return False

    def _response(self, _dialog, response):
        if response == Gtk.ResponseType.OK:
            if not self.send_button.get_sensitive():
                return
            sent = self.owner.broadcast(
                self.text(), idle_only=self.idle_only,
                notify_when_done=not self.idle_only and self.notify_checkbox.get_active())
            if not sent:
                self._refresh()
                return
        self.destroy()

    def _destroyed(self, *_):
        if self.refresh_source:
            GLib.source_remove(self.refresh_source)
            self.refresh_source = 0
        if self.owner.broadcast_dialog is self:
            self.owner.broadcast_dialog = None


class SwarmWindow(Gtk.ApplicationWindow):
    def __init__(self, app, directory):
        super().__init__(application=app, title="SWARM")
        self.app = app
        self.directory = directory
        self.sessions = []
        self.tab_labels = {}
        self.tab_spinners = {}
        self.idle_agents = set()
        self.agent_number = 0
        self.terminal_number = 0
        self.workspace_session = None
        self.startup_terminal = None
        self._startup_swarm = []
        self.directory_poll = 0
        self.font_size = 11
        self.is_fullscreen = False
        self.closing = False
        self.broadcast_dialog = None
        self.custom_broadcast_dialog = None
        self.linked_agents_dialog = None
        self.custom_selection = {}
        self.broadcast_watches = []
        self.flash_source = 0
        self.last_states = {}
        self.usage_hover_source = 0
        self.usage_hide_source = 0
        self.usage_hovered = False
        self.usage_panel_hovered = False
        self.usage_pinned = False
        self.set_default_size(1040, 690)
        self.set_size_request(560, 340)
        self.set_icon_name("swarm-terminal")
        source_icon = Path(__file__).resolve().parent.parent / "assets" / "swarm-terminal.svg"
        if source_icon.is_file():
            self.set_icon_from_file(str(source_icon))
        self.header = self._title_bar()
        self.set_titlebar(self.header)
        self.connect("notify::title", lambda window, _param: self.header.set_title(window.get_title()))
        self.get_style_context().add_class("swarm")
        self.connect("delete-event", self._delete)
        self.connect("destroy", self._destroyed)
        self.connect("window-state-event", self._window_state)
        self.accels = Gtk.AccelGroup()
        self.add_accel_group(self.accels)
        for shortcut in ("<Primary>t", "<Primary><Shift>t"):
            key, mods = Gtk.accelerator_parse(shortcut)
            self.accels.connect(key, mods, Gtk.AccelFlags.VISIBLE, self._new_agent_shortcut)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(layout)
        layout.pack_start(self._menu_bar(), False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_name("workspace")
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        empty = Gtk.Box()
        empty.set_name("workspace")
        self.stack.add_named(empty, "empty")
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_show_border(False)
        self.notebook.connect("switch-page", self._switched)
        self.stack.add_named(self.notebook, "sessions")
        layout.pack_start(self.stack, True, True, 0)
        status = Gtk.Box(spacing=20)
        status.get_style_context().add_class("status")
        self.status_label = label("No agents")
        self.folder_label = label("")
        self.folder_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.folder_label.set_hexpand(True)
        self.folder_label.set_xalign(1)
        self.account_label = label(app.auth_status)
        self.account_label.set_tooltip_text("Codex account status. Hermes uses its own sign-in.")
        status.pack_start(self.status_label, False, False, 0)
        status.pack_start(self.folder_label, True, True, 0)
        status.pack_end(self.account_label, False, False, 0)
        layout.pack_end(status, False, False, 0)
        self.show_all()
        self.stack.set_visible_child_name("empty")
        self._update_status()
        self.update_usage()
        self.directory_poll = GLib.timeout_add(400, self._poll_directory)

    def _title_bar(self):
        header = Gtk.HeaderBar(title=self.get_title(), show_close_button=True)
        header.set_decoration_layout(":minimize,maximize,close")
        header.get_style_context().add_class("swarm-header")
        source_icon = Path(__file__).resolve().parent.parent / "assets" / "swarm-terminal.svg"
        if source_icon.is_file():
            icon = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(str(source_icon), 16, 16, True))
        else:
            icon = Gtk.Image.new_from_icon_name("swarm-terminal", Gtk.IconSize.MENU)
        icon.set_tooltip_text("SWARM")
        badge = Gtk.Box(spacing=4)
        badge.pack_start(icon, False, False, 0)
        self.usage_button = Gtk.Button(label="—%")
        self.usage_button.set_relief(Gtk.ReliefStyle.NONE)
        self.usage_button.set_focus_on_click(False)
        self.usage_button.get_style_context().add_class("usage-badge")
        self.usage_button.get_accessible().set_name("Codex usage remaining")
        self.usage_button.connect("clicked", self._usage_clicked)
        self.usage_button.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.usage_button.connect("enter-notify-event", self._usage_enter)
        self.usage_button.connect("leave-notify-event", self._usage_leave)
        badge.pack_start(self.usage_button, False, False, 0)
        header.pack_start(badge)
        self.open_swarm_button = Gtk.Button(label="Open Swarm")
        self.open_swarm_button.set_tooltip_text("Open one agent tab for each enabled harness in the current folder")
        self.open_swarm_button.connect("clicked", lambda *_: self.open_swarm())
        header.pack_end(self.open_swarm_button)
        self._create_usage_panel()
        header.show_all()
        return header

    def _create_usage_panel(self):
        self.usage_panel = Gtk.Popover.new(self.usage_button)
        self.usage_panel.set_position(Gtk.PositionType.BOTTOM)
        self.usage_panel.set_modal(False)
        self.usage_panel.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.usage_panel.connect("enter-notify-event", self._usage_enter)
        self.usage_panel.connect("leave-notify-event", self._usage_leave)
        self.usage_panel.connect("closed", self._usage_closed)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_border_width(16)
        heading = label("")
        heading.set_markup("<b>Codex usage</b>")
        content.pack_start(heading, False, False, 0)
        self.usage_details = label("")
        self.usage_details.set_line_wrap(True)
        self.usage_details.set_max_width_chars(48)
        self.usage_details.set_width_chars(36)
        self.usage_details.set_selectable(False)
        self.usage_details.set_can_focus(False)
        content.pack_start(self.usage_details, False, False, 0)
        self.usage_updated = label("", "muted")
        self.usage_updated.set_line_wrap(True)
        content.pack_start(self.usage_updated, False, False, 0)
        footer = Gtk.Box(spacing=12)
        cadence = label("Updates every minute", "muted")
        footer.pack_start(cadence, True, True, 0)
        self.usage_refresh_button = Gtk.Button(label="Refresh")
        self.usage_refresh_button.connect("clicked", lambda *_: self.app.refresh_usage())
        footer.pack_end(self.usage_refresh_button, False, False, 0)
        content.pack_start(footer, False, False, 0)
        self.usage_panel.add(content)
        content.show_all()

    def _cancel_usage_timers(self):
        for name in ("usage_hover_source", "usage_hide_source"):
            source = getattr(self, name)
            if source:
                GLib.source_remove(source)
                setattr(self, name, 0)

    def _usage_enter(self, widget, event):
        if self.closing or event.mode != Gdk.CrossingMode.NORMAL or event.detail == Gdk.NotifyType.INFERIOR:
            return False
        if widget is self.usage_button:
            self.usage_hovered = True
        else:
            self.usage_panel_hovered = True
        self._cancel_usage_timers()
        if self.usage_hovered and not self.usage_panel.get_visible():
            self.usage_hover_source = GLib.timeout_add(350, self._show_usage_hover)
        return False

    def _usage_leave(self, widget, event):
        if self.closing or event.mode != Gdk.CrossingMode.NORMAL or event.detail == Gdk.NotifyType.INFERIOR:
            return False
        if widget is self.usage_button:
            self.usage_hovered = False
        else:
            self.usage_panel_hovered = False
        self._cancel_usage_timers()
        if not self.usage_pinned:
            self.usage_hide_source = GLib.timeout_add(200, self._hide_usage_hover)
        return False

    def _show_usage_hover(self):
        self.usage_hover_source = 0
        if not self.closing and self.usage_hovered and not self.usage_pinned:
            self.update_usage()
            # Hover must not grab the keyboard from the terminal.
            self.usage_panel.set_modal(False)
            self.usage_panel.popup()
        return GLib.SOURCE_REMOVE

    def _hide_usage_hover(self):
        self.usage_hide_source = 0
        if not self.closing and not self.usage_pinned and not self.usage_hovered and not self.usage_panel_hovered:
            self.usage_panel.popdown()
        return GLib.SOURCE_REMOVE

    def _usage_clicked(self, *_):
        self._cancel_usage_timers()
        if self.usage_pinned and self.usage_panel.get_visible():
            self.usage_panel.popdown()
            return
        self.usage_pinned = True
        self.update_usage()
        self.usage_panel.set_modal(True)
        self.usage_panel.popup()
        # Refresh in the background while keeping existing details visible.
        self.app.refresh_usage()

    def _usage_closed(self, *_):
        self._cancel_usage_timers()
        self.usage_pinned = False
        self.usage_panel_hovered = False

    def update_usage(self):
        snapshot = self.app.usage_snapshot
        main = snapshot.primary if snapshot is not None else None
        style = self.usage_button.get_style_context()
        style.remove_class("usage-low")
        style.remove_class("usage-unknown")
        if main is None:
            self.usage_button.set_label("—%")
            style.add_class("usage-unknown")
        else:
            self.usage_button.set_label(f"{math.floor(main.remaining)}%")
            if main.remaining <= 10:
                style.add_class("usage-low")
        rows = []
        if snapshot is not None:
            for window in (snapshot.primary, snapshot.secondary):
                if window is None:
                    continue
                row = f"{usage_window_name(window.duration_minutes)}: {math.floor(window.remaining)}% left"
                reset = local_usage_time(window.resets_at)
                if reset:
                    row += f"\nResets {reset} (local time)\n{usage_reset_countdown(window.resets_at)}"
                else:
                    row += "\nReset time not reported"
                rows.append(row)
        if not rows:
            rows.append(self.app.usage_status)
        resets = snapshot.reset_credits_remaining if snapshot is not None else None
        rows.append(f"Usage resets remaining: {resets if resets is not None else 'Not reported'}")
        detail = "\n\n".join(rows)
        self.usage_details.set_text(detail)
        updated = local_usage_time(self.app.usage_updated_at)
        self.usage_updated.set_text("Refreshing…" if self.app.usage_pending else
                                    f"Updated {updated}" if updated else "")
        self.usage_refresh_button.set_sensitive(not self.app.usage_pending and not self.app.auth_busy()
                                               and self.app.auth_state in ("chatgpt", "other"))
        accessible = self.usage_button.get_accessible()
        accessible.set_name(f"Codex usage remaining: {self.usage_button.get_label()}")
        accessible.set_description(detail + "\nClick for usage details")

    def _menu_bar(self):
        bar = Gtk.MenuBar()

        def menu(title):
            item = Gtk.MenuItem.new_with_mnemonic(title)
            submenu = Gtk.Menu()
            item.set_submenu(submenu)
            bar.append(item)
            return submenu

        def item(parent, title, callback=None, shortcut=None):
            entry = Gtk.MenuItem.new_with_mnemonic(title)
            if callback:
                entry.connect("activate", lambda *_: callback())
            else:
                entry.set_sensitive(False)
            if shortcut:
                key, mods = Gtk.accelerator_parse(shortcut)
                entry.add_accelerator("activate", self.accels, key, mods, Gtk.AccelFlags.VISIBLE)
            parent.append(entry)
            return entry

        def separator(parent):
            parent.append(Gtk.SeparatorMenuItem())

        session = menu("_Session")
        item(session, "New _Terminal", self.new_terminal, "<Primary><Shift>Return")
        item(session, "New _Window", self.new_window, "<Primary><Shift>n")
        separator(session)
        self.account_item = item(session, "Sign in with _ChatGPT", self.account_action)
        self.device_login_item = item(session, "Sign in with _Device Code", lambda: self.login(True))
        self.device_login_item.set_no_show_all(True)
        self.refresh_login_item = item(session, "Refresh Login Status", self.app.refresh_auth)
        separator(session)
        item(session, "_Close Tab", self.close_current, "<Primary><Shift>w")
        item(session, "Close Window", self.close)

        swarm = menu("S_warm")
        self.open_swarm_item = item(swarm, "_Open Swarm", self.open_swarm)
        separator(swarm)
        self.global_item = item(swarm, "_Global Broadcast…", self.open_broadcast, "<Primary><Shift>b")
        self.sleeper_item = item(swarm, "_Sleeper Broadcast…", self.open_sleeper_broadcast)
        self.custom_item = item(swarm, "_Custom Broadcast…", self.open_custom_broadcast)
        separator(swarm)
        self.interrupt_all_agents_item = item(swarm, "_Interrupt All Agents", self.interrupt_all_agents)
        self.kill_all_agents_item = item(swarm, "_Kill All Agents", self.kill_all_agents)

        actions = menu("_Actions")
        self.linked_agents_item = item(actions, "_Linked Agents…", self.open_linked_agents)
        separator(actions)
        item(actions, "_Rename Agent…", self.rename_current)
        item(actions, "_Restart Exited Agent", self.restart_current)

        edit = menu("_Edit")
        item(edit, "_Copy", self.copy, "<Primary><Shift>c")
        item(edit, "_Paste", self.paste, "<Primary><Shift>v")
        item(edit, "Select _All", lambda: self.with_terminal(lambda terminal: terminal.select_all()))

        view = menu("_View")
        item(view, "_Previous Tab", lambda: self.change_tab(-1), "<Primary>Page_Up")
        item(view, "_Next Tab", lambda: self.change_tab(1), "<Primary>Page_Down")
        separator(view)
        item(view, "Zoom _In", lambda: self.zoom(1), "<Primary>plus")
        item(view, "Zoom _Out", lambda: self.zoom(-1), "<Primary>minus")
        item(view, "_Reset Zoom", lambda: self.zoom(0), "<Primary>0")
        separator(view)
        item(view, "_Fullscreen", self.toggle_fullscreen, "F11")

        help_menu = menu("_Help")
        item(help_menu, "_Quick Start", self.quick_start)
        item(help_menu, "_About SWARM", self.about)
        return bar

    def resolve_codex(self):
        executable = shutil.which(self.app.codex)
        if executable:
            return str(Path(executable).resolve())
        self.message("Codex CLI was not found",
                     "Install Codex CLI and make sure codex is on your PATH, or start SWARM "
                     "with --codex /path/to/codex. You can also turn Codex off in Actions → Linked Agents.")
        return None

    def _new_agent_shortcut(self, *_):
        self.new_agent()
        return True

    def new_agent(self):
        if self.closing:
            return
        source = self.current()
        harness = source.harness if source is not None and source.kind == "agent" else DEFAULT_HARNESS
        if HARNESSES[harness].logout_blocked(self.app):
            self.flash("Wait for sign-out to finish before opening an agent")
            return
        executable = self.resolve_harness(harness)
        if not executable:
            return
        directory = self._directory_for_new_session()
        if directory is None:
            return
        self.agent_number += 1
        argv = self._agent_argv(harness, executable)
        return self.add_session(f"{HARNESS_NAMES[harness]} {self.agent_number}", "agent", argv,
                                directory, managed_activity_title=HARNESSES[harness].managed_activity_title,
                                harness=harness)

    def open_linked_agents(self):
        if self.closing:
            return
        if self.linked_agents_dialog is None:
            self.linked_agents_dialog = LinkedAgentsDialog(self)
        self.linked_agents_dialog.present()

    def resolve_harness(self, harness):
        return HARNESSES[harness].resolve(self)

    def _resolve_executable(self, spec):
        executable = shutil.which(getattr(self.app, spec.key))
        if executable:
            return str(Path(executable).resolve())
        title, detail = spec.missing_cli_dialog()
        self.message(title, detail)
        return None

    def _agent_argv(self, harness, executable):
        return HARNESSES[harness].agent_command(executable)

    def open_swarm(self):
        if self.closing:
            return []
        enabled = [harness for harness in HARNESS_NAMES if self.app.linked_agents.enabled[harness]]
        if not enabled:
            self.message("No linked agents enabled", "Turn on Codex or Hermes in Actions → Linked Agents.")
            self.open_linked_agents()
            return []
        if any(HARNESSES[harness].logout_blocked(self.app) for harness in enabled):
            self.flash("Wait for Codex sign-out to finish before opening a swarm")
            return []
        directory = self._directory_for_new_session()
        if directory is None:
            return []
        # Resolve every enabled command before spawning, so a missing harness
        # does not leave a partially opened swarm or change the chosen folder.
        executables = {}
        for harness in enabled:
            executable = self.resolve_harness(harness)
            if executable is None:
                return []
            executables[harness] = executable
        sessions = []
        for harness, executable in executables.items():
            self.agent_number += 1
            argv = self._agent_argv(harness, executable)
            sessions.append(self.add_session(f"{HARNESS_NAMES[harness]} {self.agent_number}", "agent", argv,
                                             directory, managed_activity_title=HARNESSES[harness].managed_activity_title,
                                             harness=harness))
        if self.startup_terminal in self.sessions and self.startup_terminal.is_idle_shell:
            self._startup_swarm = sessions
            self._close_startup_terminal_after_swarm()
        return sessions

    def _close_startup_terminal_after_swarm(self):
        if not self._startup_swarm:
            return
        if any(session not in self.sessions or session.state in ("failed", "exited", "closed")
               for session in self._startup_swarm):
            self._startup_swarm = []
            return
        if any(session.state != "running" for session in self._startup_swarm):
            return
        self._startup_swarm = []
        terminal = self.startup_terminal
        if terminal in self.sessions and terminal.is_idle_shell:
            self.close_session(terminal, confirm=False)

    def new_terminal(self, directory=None):
        directory = self._directory_for_new_session(directory, recover=True)
        if directory is None:
            return
        self.terminal_number += 1
        return self.add_session(f"Terminal {self.terminal_number}", "shell", self.app.shell_command(), directory)

    def new_window(self):
        directory = self._directory_for_new_session(recover=True)
        if directory is not None:
            return self.app.new_window(directory)

    def working_directory(self, source=None):
        source = source if source is not None else self.current()
        if source is None or source.kind == "login":
            source = self.workspace_session if self.workspace_session in self.sessions else None
        if source is not None:
            return source.working_directory()
        return self.directory

    def _directory_for_new_session(self, directory=None, *, recover=False):
        explicit = directory is not None
        directory = self.working_directory() if directory is None else directory
        if directory is None or not Path(directory).is_dir():
            if recover and not explicit:
                for candidate in (self.directory, self.app.directory, str(Path.home())):
                    if Path(candidate).is_dir():
                        self.flash(f"Current folder unavailable; opening in {candidate}")
                        return candidate
            self.message("Working folder is unavailable",
                         "Use cd to enter an existing folder, or open a new shell with Session → New Terminal.")
            return None
        return directory

    def login(self, device=False):
        # A single login flow serves every window and agent in this application.
        for window in self.app.get_windows():
            if not isinstance(window, SwarmWindow):
                continue
            for session in window.sessions:
                if session.kind == "login" and session.state in ("starting", "running"):
                    window.notebook.set_current_page(window.notebook.page_num(session))
                    window.present()
                    return
        if self.app.auth_pending or self.app.logout_pending:
            return
        executable = self.resolve_codex()
        if executable:
            argv = [executable, "login"]
            if device:
                argv.append("--device-auth")
            self.add_session("ChatGPT Sign In", "login", argv, str(Path.home()))

    def update_auth_controls(self):
        signed_in = self.app.auth_state in ("chatgpt", "other")
        if signed_in:
            title = "Log out of _ChatGPT" if self.app.auth_state == "chatgpt" else "Log out of _Codex"
        else:
            title = "Sign in with _ChatGPT"
        self.account_item.set_label(title)
        self.account_item.set_sensitive(not self.app.auth_busy())
        self.device_login_item.set_visible(not signed_in)
        self.device_login_item.set_sensitive(not self.app.auth_busy())
        self.refresh_login_item.set_sensitive(not self.app.logout_pending)

    def account_action(self):
        if self.app.auth_busy():
            return
        if self.app.auth_state in ("chatgpt", "other"):
            self.logout()
        else:
            self.login()

    def logout(self):
        if self.app.auth_busy() or self.app.auth_state not in ("chatgpt", "other"):
            return
        name = "ChatGPT" if self.app.auth_state == "chatgpt" else "Codex"
        if not self.confirm(f"Log out of {name}?",
                            "This removes the saved Codex login for your user account, including "
                            "Codex used outside SWARM. Already-running agents may remain signed "
                            "in until their sessions end.", "Log Out"):
            return
        # The confirmation runs a nested event loop; another window may have
        # started an account operation while it was open.
        if self.closing or self.app.auth_busy():
            return
        executable = self.resolve_codex()
        if executable:
            self.app.logout(executable)

    def add_session(self, title, kind, argv, directory, *, managed_activity_title=False, harness="codex"):
        session = TerminalSession(title, kind, directory, self._session_changed,
                                  managed_activity_title=managed_activity_title, harness=harness)
        terminal = session.terminal
        terminal.set_font(Pango.FontDescription(f"Monospace {self.font_size}"))
        terminal.set_scrollback_lines(20000)
        terminal.set_scroll_on_output(False)
        terminal.set_scroll_on_keystroke(True)
        terminal.set_mouse_autohide(True)
        terminal.set_color_background(color("#14161b"))
        terminal.set_color_foreground(color("#e1e5ed"))
        terminal.set_color_cursor(color("#9ed8bd"))
        terminal.set_allow_hyperlink(True)
        terminal.connect("button-press-event", self._terminal_menu)
        terminal.connect("bell", lambda *_: self.set_urgency_hint(not self.is_active()))
        terminal.connect("focus-in-event", lambda *_: self.set_urgency_hint(False))
        self.sessions.append(session)
        tab = Gtk.Box(spacing=8)
        spinner = Gtk.Spinner()
        spinner.set_size_request(14, 14)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_no_show_all(True)
        spinner.get_style_context().add_class("agent-spinner")
        spinner.set_tooltip_text("Agent is working")
        spinner.get_accessible().set_name("Agent is working")
        tab.pack_start(spinner, False, False, 0)
        name = label(title)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_width_chars(12)
        name.set_max_width_chars(25)
        tab.pack_start(name, True, True, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Close tab")
        close.get_style_context().add_class("tab-close")
        close.connect("clicked", lambda *_: self.close_session(session))
        tab.pack_end(close, False, False, 0)
        tab.show_all()
        self.tab_labels[session] = name
        self.tab_spinners[session] = spinner
        self._update_agent_spinner(session)
        index = self.notebook.append_page(session, tab)
        self.notebook.set_tab_reorderable(session, True)
        session.show_all()
        self.stack.set_visible_child_name("sessions")
        self.notebook.set_current_page(index)
        terminal.grab_focus()
        session.start(argv)
        self._update_status()
        if kind == "login":
            self.app.sync_auth_controls()
        return session

    def _session_changed(self, session):
        if self.closing or session not in self.sessions:
            return
        if (session in self.custom_selection
                and session.broadcast_identity != self.custom_selection[session]):
            self.custom_selection.pop(session, None)
        self._update_agent_spinner(session)
        if session.is_detected_agent and session.detected_agent_title is None:
            self.agent_number += 1
            session.detected_agent_title = f"Agent {self.agent_number}"
            session.title = session.detected_agent_title
        previous = self.last_states.get(session)
        self.last_states[session] = session.state
        killed = session.kind == "agent" and session.state == "exited"
        display_state = "killed" if killed else session.state
        suffix = {"starting": " · starting", "exited": " · exited", "killed": " · killed 💀",
                  "failed": " · failed"}.get(display_state, "")
        tab_label = self.tab_labels[session]
        tab_label.set_text(session.title + suffix)
        tab_label.set_width_chars(22 if killed else 12)
        tab_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE if killed else Pango.EllipsizeMode.END)
        style = tab_label.get_style_context()
        style.add_class("killed-tab") if killed else style.remove_class("killed-tab")
        tab_label.set_tooltip_text(f"{session.title}\n{session.directory}\n{display_state}")
        if session.state == "failed" and previous != "failed":
            safe_error = "".join(char for char in session.error if char.isprintable() or char in "\n\t")
            session.terminal.feed(f"\r\nSWARM could not start this session.\r\n{safe_error}\r\n".encode("utf-8"))
        if session.kind == "login" and session.state in ("exited", "failed") and previous != session.state:
            self.app.refresh_auth()
        if session.kind == "login":
            self.app.sync_auth_controls()
        self._move_newly_idle_agent(session)
        if session is self.current():
            self.set_title(f"{session.title} — SWARM")
        self._update_status()
        self._check_broadcast_notifications()
        self._close_startup_terminal_after_swarm()

    def _move_newly_idle_agent(self, session):
        if not session.agent_idle:
            self.idle_agents.discard(session)
            return
        if session in self.idle_agents:
            return
        # Move once on entry to Ready, so polling and renaming cannot undo a
        # user's tab order. Broadcast eligibility/cooldowns are independent.
        self.idle_agents.add(session)
        if self.notebook.page_num(session) <= 0:
            return
        selected = self.current()
        self.notebook.reorder_child(session, 0)
        # Reordering keeps the same page selected; never switch to the agent
        # that just finished or grab focus away from another input widget.
        if selected is not None and self.current() is not selected:
            index = self.notebook.page_num(selected)
            if index >= 0:
                self.notebook.set_current_page(index)

    def _update_agent_spinner(self, session):
        spinner = self.tab_spinners.get(session)
        if spinner is None:
            return
        if session.agent_busy:
            spinner.show()
            spinner.start()
        else:
            spinner.stop()
            spinner.hide()

    def active_agents(self):
        self._refresh_detected_agents()
        return [session for session in self.sessions if session.can_broadcast]

    def ready_agents(self):
        self._refresh_detected_agents()
        return [session for session in self.sessions if session.can_receive_sleeper_broadcast]

    def custom_agents(self):
        self._refresh_detected_agents()
        agents = [session for session in self.sessions
                  if session.kind == "agent" and session.state in ("starting", "running")]
        # A remembered checkbox belongs to this specific agent process. Never
        # silently carry it over to a replacement launched in the same shell.
        for session, identity in list(self.custom_selection.items()):
            if session not in agents or session.broadcast_identity != identity:
                self.custom_selection.pop(session, None)
        return sorted(agents, key=self.notebook.page_num)

    def set_custom_selected(self, session, identity, selected):
        if not selected:
            self.custom_selection.pop(session, None)
        elif (session in self.sessions and identity is not None
              and session.broadcast_identity == identity):
            self.custom_selection[session] = identity

    def select_custom_agents(self, mode):
        agents = self.custom_agents()
        self.custom_selection.clear()
        if mode not in {"all", "idle"}:
            return
        for session in agents:
            identity = session.broadcast_identity
            if identity is not None and (mode == "all" or session.agent_idle):
                self.custom_selection[session] = identity

    def _refresh_detected_agents(self):
        for session in self.sessions:
            session.refresh_agent(self.app.codex)
            session.refresh_activity()

    def current(self):
        return self.notebook.get_nth_page(self.notebook.get_current_page())

    def _switched(self, _notebook, session, _index):
        if session.kind in ("shell", "agent"):
            self.workspace_session = session
        if hasattr(self, "folder_label"):
            self._sync_workspace_folder(session)
        self.set_title(f"{session.title} — SWARM")

    def _sync_workspace_folder(self, source=None):
        folder = self.working_directory(source)
        if folder is not None:
            self.directory = folder
        self.folder_label.set_text(folder if folder is not None else "Folder unavailable")
        self.folder_label.set_tooltip_text(f"Working folder: {folder}" if folder is not None else
                                          "Use cd to enter an existing folder before starting an agent.")

    def _poll_directory(self):
        if self.closing:
            return GLib.SOURCE_REMOVE
        self._refresh_detected_agents()
        self._sync_workspace_folder()
        self._check_broadcast_notifications()
        return GLib.SOURCE_CONTINUE

    def _update_status(self):
        count = sum(session.kind == "agent" and session.state == "running" for session in self.sessions)
        if not self.flash_source:
            busy = sum(session.agent_busy for session in self.sessions)
            self.status_label.set_text(
                f"{count} agent{'s' if count != 1 else ''} · {busy} running" if count else "No agents")
        self.global_item.set_sensitive(count > 0)
        self.sleeper_item.set_sensitive(count > 0)
        self.custom_item.set_sensitive(count > 0)
        self.interrupt_all_agents_item.set_sensitive(count > 0)
        self.kill_all_agents_item.set_sensitive(any(session.kind == "agent" for session in self.sessions))
        current = self.current()
        self._sync_workspace_folder()
        self.account_label.set_text(self.app.auth_status)
        self.update_auth_controls()
        if not current:
            self.set_title("SWARM")

    def flash(self, message):
        if self.closing:
            return
        if self.flash_source:
            GLib.source_remove(self.flash_source)
        self.status_label.set_text(message)
        self.flash_source = GLib.timeout_add_seconds(7, self._clear_flash)

    def _clear_flash(self):
        self.flash_source = 0
        self._update_status()
        return GLib.SOURCE_REMOVE

    def open_sleeper_broadcast(self):
        self.open_broadcast(idle_only=True)

    def open_custom_broadcast(self):
        if not self.active_agents():
            self.flash("No agents can receive a broadcast yet")
            return
        if self.broadcast_dialog:
            self.broadcast_dialog.present()
        elif self.custom_broadcast_dialog:
            self.custom_broadcast_dialog.present()
        else:
            self.custom_broadcast_dialog = CustomBroadcastDialog(self)

    def open_broadcast(self, *, idle_only=False):
        if not self.active_agents():
            self.flash("No agents can receive a broadcast yet")
            return
        if self.custom_broadcast_dialog:
            self.custom_broadcast_dialog.present()
        elif self.broadcast_dialog:
            self.broadcast_dialog.set_mode(idle_only=idle_only)
            self.broadcast_dialog.present()
        else:
            self.broadcast_dialog = BroadcastDialog(self, idle_only=idle_only)

    def broadcast(self, message, *, idle_only=False, recipients=None, notify_when_done=False):
        try:
            message = normalize_message(message)
        except ValueError as error:
            self.flash(str(error))
            return False
        # Take the target snapshot when Send is clicked, never when the dialog opens.
        targets = self.ready_agents() if idle_only else self.active_agents()
        custom = recipients is not None
        if custom:
            recipients = dict(recipients)
            targets = [session for session in targets if session in recipients
                       and session.broadcast_identity == recipients[session]]
        if not targets:
            if custom:
                self.flash("No selected agents can receive a message")
            else:
                self.flash("No idle agents ready to receive a message" if idle_only
                           else "No running agents in this window")
            return False
        if notify_when_done and (idle_only or custom):
            self.flash("Finish notifications are available in Global Broadcast")
            return False
        if notify_when_done and any(not session.spec.finish_notifications for session in targets):
            self.flash("Finish notifications are available for Codex-only broadcasts")
            return False
        identities = {session: session.agent_identity for session in targets} if notify_when_done else {}
        if any(identity is None for identity in identities.values()):
            self.flash("Agent sessions changed. Please try the broadcast again.")
            return False
        remaining = len(targets)
        requested = len(recipients) if custom else len(targets)
        delivered = 0
        # New input supersedes a pending alert for any overlapping recipient.
        self.broadcast_watches = [watch for watch in self.broadcast_watches
                                  if watch.recipients.isdisjoint(targets)]
        watch = None
        if notify_when_done:
            watch = BroadcastCompletion(frozenset(targets), identities)
            self.broadcast_watches.append(watch)

        def completed(session, success):
            nonlocal remaining, delivered
            remaining -= 1
            delivered += bool(success)
            if watch in self.broadcast_watches:
                if success:
                    watch.submitted.add(session)
                else:
                    self.broadcast_watches.remove(watch)
            if remaining == 0:
                if delivered == requested:
                    name = "Custom broadcast" if custom else "Sleeper broadcast" if idle_only else "Broadcast"
                    self.flash(f"{name} sent to {delivered} agent{'s' if delivered != 1 else ''}")
                else:
                    self.flash(f"Sent to {delivered} agents. Not submitted to {requested - delivered}; "
                               "review their inputs.")
            self._check_broadcast_notifications()

        self.flash(f"Sending to {len(targets)} {'idle ' if idle_only else ''}agents…")
        accepted = False
        for session in targets:
            # The session reports both accepted and rejected sends through completed.
            callback = lambda success, target=session: completed(target, success)
            if custom:
                sent = session.broadcast(message, callback, idle_only=idle_only,
                                         expected_target=recipients[session])
                accepted = sent or accepted
            elif idle_only:
                session.broadcast(message, callback, idle_only=True)
            elif notify_when_done:
                session.broadcast(message, callback, expected_target=watch.identities[session])
            else:
                session.broadcast(message, callback)
        return accepted if custom else True

    def _check_broadcast_notifications(self):
        if self.closing:
            return
        now = time.monotonic()
        for watch in list(self.broadcast_watches):
            if any(session not in self.sessions or session.kind != "agent" or session.state != "running"
                   or session.agent_identity != watch.identities[session]
                   for session in watch.recipients):
                self.broadcast_watches.remove(watch)
                continue
            for session in watch.submitted:
                if session.agent_busy:
                    watch.started.add(session)
                    watch.busy_titles[session] = session.terminal.get_window_title()
            if (watch.submitted != watch.recipients or watch.started != watch.recipients
                    or not all(session.activity_finished(watch.busy_titles[session]) for session in watch.recipients)):
                watch.ready_since = None
                continue
            if watch.ready_since is None:
                watch.ready_since = now
            # A brief Ready between queued turns should not trigger an alert.
            if now - watch.ready_since < BROADCAST_READY_SECONDS:
                continue
            self.broadcast_watches.remove(watch)
            count = len(watch.recipients)
            self.flash(f"Broadcast finished: {count} agent{'s' if count != 1 else ''} ready")
            notify_agents_finished(count, on_error=lambda _error: self.flash(
                "Agents finished, but the desktop notification could not be shown"))

    def rename_current(self):
        session = self.current()
        if not session or session.kind != "agent":
            return
        dialog = Gtk.Dialog(title="Rename Agent", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Rename", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=session.title, activates_default=True, max_length=60)
        entry.set_margin_top(16)
        entry.set_margin_bottom(16)
        entry.set_margin_start(16)
        entry.set_margin_end(16)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        entry.select_region(0, -1)
        if dialog.run() == Gtk.ResponseType.OK and entry.get_text().strip():
            session.title = entry.get_text().strip()
            if session.is_detected_agent:
                session.detected_agent_title = session.title
            self._session_changed(session)
            self.set_title(f"{session.title} — SWARM")
        dialog.destroy()

    def restart_current(self):
        session = self.current()
        if not session or session.kind != "agent" or session.state not in ("failed", "exited"):
            self.flash("Select an exited agent to restart")
            return
        if HARNESSES[session.harness].logout_blocked(self.app):
            self.flash("Wait for sign-out to finish before opening an agent")
            return
        executable = self.resolve_harness(session.harness)
        if executable:
            title, directory, harness = session.title, session.directory, session.harness
            self.close_session(session, confirm=False)
            argv = self._agent_argv(harness, executable)
            self.add_session(title, "agent", argv, directory,
                             managed_activity_title=HARNESSES[harness].managed_activity_title,
                             harness=harness)

    def close_current(self):
        if self.current():
            self.close_session(self.current())

    def interrupt_all_agents(self):
        if self.closing:
            return
        agents = self.active_agents()
        if not agents:
            self.flash("No agents ready to receive Escape in this window")
            return
        # Interrupted work must not later produce a broadcast-finished alert.
        self.broadcast_watches = [watch for watch in self.broadcast_watches
                                  if watch.recipients.isdisjoint(agents)]
        sent = sum(session.interrupt() for session in agents)
        self.flash(f"Escape sent to {sent} agent{'s' if sent != 1 else ''}")

    def kill_all_agents(self):
        if self.closing:
            return
        self._refresh_detected_agents()
        agents = [session for session in self.sessions if session.kind == "agent"]
        busy = sum(session.agent_busy for session in agents)
        if busy:
            if not self.confirm("Kill all agents?",
                                f"{busy} agent{' is' if busy == 1 else 's are'} still running. "
                                f"This will stop their work and close all {len(agents)} "
                                f"agent tab{'s' if len(agents) != 1 else ''} in this window.",
                                "Kill All Agents"):
                return
            if self.closing:
                return
            self._refresh_detected_agents()
        # A detected agent may return to its shell while the confirmation is open.
        for session in agents:
            if session.kind == "agent":
                self.close_session(session, confirm=False)

    def close_session(self, session, confirm=True):
        if session not in self.sessions:
            return
        if confirm and session.state in ("starting", "running"):
            if not self.confirm(f"Close {session.title}?", "This will stop the session running in this tab.", "Close Tab"):
                return
        if session is self.startup_terminal:
            self.startup_terminal = None
            self._startup_swarm = []
        elif session in self._startup_swarm:
            self._startup_swarm = []
        self.sessions.remove(session)
        self.broadcast_watches = [watch for watch in self.broadcast_watches if session not in watch.recipients]
        self.last_states.pop(session, None)
        self.idle_agents.discard(session)
        self.custom_selection.pop(session, None)
        self.tab_labels.pop(session, None)
        spinner = self.tab_spinners.pop(session, None)
        if spinner is not None:
            spinner.stop()
        session.close()
        self.notebook.remove_page(self.notebook.page_num(session))
        session.destroy()
        if not self.sessions:
            self.stack.set_visible_child_name("empty")
        self._update_status()
        if session.kind == "login":
            self.app.refresh_auth()
            self.app.sync_auth_controls()

    def with_terminal(self, callback):
        if self.current():
            callback(self.current().terminal)

    def copy(self):
        self.with_terminal(lambda terminal: terminal.copy_clipboard_format(Vte.Format.TEXT))

    def paste(self):
        self.with_terminal(lambda terminal: terminal.paste_clipboard())

    def _terminal_menu(self, terminal, event):
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        for title, callback in (("Copy", self.copy), ("Paste", self.paste), ("Global Broadcast…", self.open_broadcast)):
            item = Gtk.MenuItem(label=title)
            item.connect("activate", lambda _item, fn=callback: fn())
            if title == "Copy":
                item.set_sensitive(terminal.get_has_selection())
            elif title.startswith("Global"):
                item.set_sensitive(bool(self.active_agents()))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def change_tab(self, delta):
        total = self.notebook.get_n_pages()
        if total:
            self.notebook.set_current_page((self.notebook.get_current_page() + delta) % total)
            self.current().terminal.grab_focus()

    def zoom(self, change):
        self.font_size = min(28, max(7, self.font_size + change)) if change else 11
        for session in self.sessions:
            session.terminal.set_font(Pango.FontDescription(f"Monospace {self.font_size}"))

    def toggle_fullscreen(self):
        self.unfullscreen() if self.is_fullscreen else self.fullscreen()

    def _window_state(self, _window, event):
        self.is_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        return False

    def message(self, title, detail):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=title)
        dialog.format_secondary_text(detail)
        dialog.run()
        dialog.destroy()

    def confirm(self, title, detail, accept):
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=title)
        dialog.format_secondary_text(detail)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, accept, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        accepted = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        return accepted

    def quick_start(self):
        self.message("Using SWARM",
                     "1. Actions → Linked Agents: choose Codex and/or Hermes.\n"
                     "2. In the terminal, cd to your project folder.\n"
                     "3. Click Open Swarm to open each enabled harness.\n"
                     "4. Swarm → Global Broadcast (Ctrl+Shift+B).\n\n"
                     "Install and sign in to each CLI separately. Session sign-in and the usage badge "
                     "are for Codex. Ctrl+T opens a new agent using the selected agent tab's harness "
                     "and folder: Hermes from Hermes, Codex from Codex. From a shell or with no agent "
                     "selected, it opens Codex. Ctrl+Shift+T does the same.\n\n"
                     "Open another shell with "
                     "Session → New Terminal (Ctrl+Shift+Enter).\n\n"
                     "Codex started in a terminal automatically becomes an agent tab while it "
                     "runs in the foreground, then returns to a terminal when the shell returns. "
                     "Broadcast submits to all running agent tabs in this window. Shell prompts and sign-in "
                     "tabs are excluded. Complete Codex’s "
                     "startup prompts first and leave each harness’s message input empty. Busy sessions handle "
                     "the submitted input according to that harness’s normal behavior.\n\n"
                     "New Codex tabs run codex --yolo, with approvals and sandboxing disabled. "
                     "New Hermes tabs run hermes --yolo, bypassing its dangerous-command approval prompts. "
                     "Manually started Codex keeps the options you supplied. "
                     "Sleeper Broadcast submits only to agents ready for a new message, skipping "
                     "working agents and those with unavailable status. Codex opened with Ctrl+T "
                     "or Open Swarm supplies this status; "
                     "manually launched agents without it are skipped. Custom Broadcast lets you "
                     "choose individual agents, with Select all, Select idle, and Clear selection. "
                     "It sends to the checked agents, including working agents, and remembers your "
                     "selection within this window.")

    def about(self):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name="SWARM", version=__version__,
                                 comments="A terminal workspace for Codex and Hermes agents.",
                                 logo_icon_name="swarm-terminal")
        dialog.run()
        dialog.destroy()

    def _delete(self, *_):
        active = sum(session.state in ("starting", "running") for session in self.sessions)
        if active and not self.confirm("Close this SWARM window?",
                                       f"This will stop {active} running session{'s' if active != 1 else ''}.",
                                       "Close Window"):
            return True
        return False

    def _destroyed(self, *_):
        self.closing = True
        self.startup_terminal = None
        self._startup_swarm = []
        self.broadcast_watches.clear()
        self._cancel_usage_timers()
        self.usage_panel.destroy()
        if self.linked_agents_dialog is not None:
            self.linked_agents_dialog.destroy()
        had_login = any(session.kind == "login" for session in self.sessions)
        if self.directory_poll:
            GLib.source_remove(self.directory_poll)
            self.directory_poll = 0
        if self.flash_source:
            GLib.source_remove(self.flash_source)
            self.flash_source = 0
        for session in self.sessions:
            session.close()
        self.sessions.clear()
        self.idle_agents.clear()
        self.custom_selection.clear()
        for spinner in self.tab_spinners.values():
            spinner.stop()
        self.tab_spinners.clear()
        self.app.sync_auth_controls()
        if had_login and any(isinstance(window, SwarmWindow) and not window.closing
                             for window in self.app.get_windows()):
            self.app.refresh_auth()


class SwarmApplication(Gtk.Application):
    def __init__(self, directory, *harness_executables, linked_agents_path=None):
        super().__init__(application_id="io.swarm.Terminal", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.directory = directory
        # One executable attribute per harness, in registry order; harnesses
        # without an explicit argument fall back to SWARM_<KEY>, then name.
        for spec, executable in zip(HARNESS_ORDER, harness_executables):
            setattr(self, spec.key, executable)
        for spec in HARNESS_ORDER[len(harness_executables):]:
            setattr(self, spec.key, os.environ.get(spec.executable_environment(),
                                                   spec.default_executable))
        self.linked_agents = LinkedAgentsSettings(linked_agents_path)
        self.auth_status = "Checking login…"
        self.auth_state = "unknown"
        self.auth_pending = False
        self.auth_refresh_requested = False
        self.logout_pending = False
        self.usage_snapshot = None
        self.usage_status = "Checking Codex login…"
        self.usage_updated_at = None
        self.usage_pending = False
        self.usage_generation = 0
        self.usage_cancel = None
        self.usage_poll = 0

    def set_linked_agent_enabled(self, harness, enabled):
        try:
            self.linked_agents.set_enabled(harness, enabled)
        except OSError as error:
            self.linked_agents.load_error = f"Could not save Linked Agents settings: {error}"
            saved = False
        else:
            saved = True
        for window in self.get_windows():
            if isinstance(window, SwarmWindow) and not window.closing and window.linked_agents_dialog is not None:
                window.linked_agents_dialog.refresh()
        return saved

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.usage_poll = GLib.timeout_add_seconds(60, self._poll_usage)

    def do_shutdown(self):
        if self.usage_poll:
            GLib.source_remove(self.usage_poll)
            self.usage_poll = 0
        self.invalidate_usage("SWARM closed")
        Gtk.Application.do_shutdown(self)

    def do_activate(self):
        self.new_window(self.directory)
        self.refresh_auth()

    def shell_command(self):
        candidates = [os.environ.get("SHELL")]
        try:
            candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
        except (KeyError, OSError):
            pass
        candidates.append("/bin/sh")
        for candidate in candidates:
            executable = shutil.which(candidate) if candidate else None
            if executable:
                # Preserve shell symlink names such as sh, which can select a
                # different startup mode, while making the path absolute.
                return [os.path.abspath(executable), "-i"]
        return ["/bin/sh", "-i"]

    def new_window(self, directory=None, *, start_terminal=True):
        window = SwarmWindow(self, directory or self.directory)
        if start_terminal:
            window.startup_terminal = window.new_terminal(window.directory)
        window.present()
        return window

    def refresh_auth(self):
        if self.auth_pending or self.logout_pending:
            self.auth_refresh_requested = True
            return
        self.auth_pending = True
        self.invalidate_usage("Checking Codex login…")
        self._set_auth_status("Checking login…")

        def check():
            state = None
            executable = shutil.which(self.codex)
            if not executable:
                status = "Codex not found"
                state = "missing"
            else:
                try:
                    result = subprocess.run([executable, "login", "status"],
                                            capture_output=True, text=True, timeout=12, check=False)
                    output = (result.stdout + result.stderr).lower()
                    if result.returncode == 0:
                        state = "chatgpt" if "chatgpt" in output else "other"
                        status = "Signed in with ChatGPT" if state == "chatgpt" else "Signed in to Codex"
                    elif "not logged in" in output or "not signed in" in output:
                        status = "Signed out"
                        state = "signed_out"
                    else:
                        status = "Login status unavailable"
                except (OSError, subprocess.TimeoutExpired):
                    status = "Login status unavailable"
            GLib.idle_add(self._auth_finished, status, state)

        threading.Thread(target=check, daemon=True).start()

    def has_login_flow(self):
        return any(session.kind == "login" and session.state in ("starting", "running")
                   for window in self.get_windows() if isinstance(window, SwarmWindow) and not window.closing
                   for session in window.sessions)

    def auth_busy(self):
        return self.auth_pending or self.logout_pending or self.has_login_flow()

    def sync_auth_controls(self):
        for window in self.get_windows():
            if isinstance(window, SwarmWindow) and not window.closing:
                window.update_auth_controls()

    def logout(self, executable):
        if self.auth_busy() or self.auth_state not in ("chatgpt", "other"):
            return False
        self.logout_pending = True
        self.invalidate_usage("Signing out…")
        self.hold()
        self._set_auth_status("Signing out…")

        def sign_out():
            try:
                result = subprocess.run([executable, "logout"], capture_output=True,
                                        text=True, timeout=12, check=False)
                success = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                success = False
            GLib.idle_add(self._logout_finished, success)

        threading.Thread(target=sign_out, daemon=True).start()
        return True

    def _logout_finished(self, success):
        self.logout_pending = False
        if success:
            self._set_auth_status("Signed out", "signed_out")
        else:
            self._set_auth_status("Could not sign out")
        for window in self.get_windows():
            if isinstance(window, SwarmWindow) and not window.closing:
                window.flash("Signed out of Codex" if success else "Could not sign out; check your login status and try again")
        self.auth_refresh_requested = False
        self.refresh_auth()
        self.release()
        return GLib.SOURCE_REMOVE

    def _set_auth_status(self, status, state=None):
        self.auth_status = status
        if state is not None:
            self.auth_state = state
            if state not in ("chatgpt", "other"):
                self.invalidate_usage("Sign in with ChatGPT to see usage" if state == "signed_out" else
                                      "Codex usage unavailable")
        for window in self.get_windows():
            if isinstance(window, SwarmWindow) and not window.closing:
                window.account_label.set_text(status)
                window.update_auth_controls()

    def _auth_finished(self, status, state=None):
        self.auth_pending = False
        self._set_auth_status(status, state)
        if self.auth_refresh_requested:
            self.auth_refresh_requested = False
            self.refresh_auth()
        elif self.auth_state in ("chatgpt", "other"):
            self.refresh_usage()
        return GLib.SOURCE_REMOVE

    def _poll_usage(self):
        self.refresh_usage()
        return GLib.SOURCE_CONTINUE

    def sync_usage(self):
        for window in self.get_windows():
            if isinstance(window, SwarmWindow) and not window.closing:
                window.update_usage()

    def invalidate_usage(self, status):
        self.usage_generation += 1
        if self.usage_cancel is not None:
            self.usage_cancel.set()
            self.usage_cancel = None
        self.usage_pending = False
        self.usage_snapshot = None
        self.usage_updated_at = None
        self.usage_status = status
        self.sync_usage()

    def refresh_usage(self):
        if self.usage_pending or self.auth_busy() or self.auth_state not in ("chatgpt", "other"):
            return False
        if not any(isinstance(window, SwarmWindow) and not window.closing for window in self.get_windows()):
            return False
        self.usage_pending = True
        self.usage_status = "Checking Codex usage…"
        self.usage_generation += 1
        generation = self.usage_generation
        cancel = threading.Event()
        self.usage_cancel = cancel
        self.sync_usage()

        def check():
            snapshot = None
            error = None
            try:
                snapshot = fetch_usage(self.codex, cancel=cancel)
            except UsageUnavailable as exc:
                error = str(exc)
            except Exception:
                error = "Codex usage unavailable. Click to try again."
            GLib.idle_add(self._usage_finished, generation, snapshot, error)

        # Allow the cancelled worker to close and reap its helper at shutdown.
        threading.Thread(target=check, daemon=False).start()
        return True

    def _usage_finished(self, generation, snapshot, error):
        # Results from a request started before logout must not restore the
        # previous account's numbers after the account changes.
        if generation != self.usage_generation:
            return GLib.SOURCE_REMOVE
        self.usage_pending = False
        self.usage_cancel = None
        self.usage_snapshot = snapshot if error is None else None
        self.usage_updated_at = time.time() if self.usage_snapshot is not None else None
        self.usage_status = error or "Codex did not report a usage limit"
        self.sync_usage()
        return GLib.SOURCE_REMOVE
