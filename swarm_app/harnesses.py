"""Registry of the agent harnesses SWARM can swarm.

Everything that differs between Codex and Hermes lives here as a ``Harness``
attribute or method. The rest of the application asks the registry
(``HARNESSES`` by key, ``HARNESS_ORDER`` for display and launch order) and
never branches on a harness name, so adding a harness means writing one
subclass and appending it to ``HARNESS_ORDER`` — no call-site edits elsewhere:

- ``swarm_app/__main__.py`` builds one ``--<key>`` CLI flag per harness,
  defaulting to ``SWARM_<KEY>`` from the environment.
- ``SwarmApplication`` stores one ``<key>`` executable attribute per harness.
- ``LinkedAgentsSettings`` persists and offers one switch per harness.
- Tab titles, launch argv, activity reading, and capability flags all come
  from the registry entry.
"""

from __future__ import annotations

from pathlib import Path
import termios

import gi

gi.require_version("Vte", "2.91")
from gi.repository import Vte

from .activity import (
    codex_agent_command,
    hermes_agent_command,
    hermes_prompt_activity,
    hermes_title_activity,
    title_activity,
    title_finished,
)
from .usage_types import UsageSnapshot


class Harness:
    """Per-harness metadata and behavior; subclasses override what differs."""

    # Canonical identifier: settings key, CLI flag, and application attribute.
    key = ""
    # Display name used in tab titles, dialogs, and messages.
    name = ""
    # Default executable name used when no flag or environment override is set.
    default_executable = ""

    # SWARM controls this harness's terminal title, so readiness and
    # completion can be read affirmatively from it.
    managed_activity_title = False
    # Broadcast finish notifications are supported for this harness.
    finish_notifications = False
    # Custom Broadcast status label when the agent is not working.
    not_ready_status = "Not ready"
    # Usage badge (titlebar): label, fetcher, and accent color. None = no badge.
    usage_label = None
    usage_accent = "#9ed8bd"
    usage_classes = ()

    def executable_environment(self) -> str:
        """Environment variable overriding this harness's executable."""
        return f"SWARM_{self.key.upper()}"

    def agent_command(self, executable: str) -> list[str]:
        """Argv for one agent tab; executable is a resolved absolute path."""
        return [executable]

    def resolve(self, window) -> str | None:
        """Locate this harness's executable, reporting failures to the user.

        Returns the resolved absolute path, or None after the window has
        explained the problem. Subclasses may override to reuse a window
        resolver so tests and callers can intercept the lookup.
        """
        return window._resolve_executable(self)

    def read_activity(self, session) -> bool | None:
        """True means working, False means not working, None is unavailable."""
        return None

    def activity_finished(self, session, busy_title: str | None) -> bool:
        """Recognize completion after a broadcast already observed work."""
        return False

    def logout_blocked(self, app) -> bool:
        """Whether opening this harness must wait for Codex sign-out."""
        return False

    def missing_cli_dialog(self) -> tuple[str, str]:
        """(Title, detail) explaining a missing executable and its flag."""
        return (f"{self.name} CLI was not found",
                f"Install {self.name} CLI and make sure {self.key} is on your PATH, or start "
                f"SWARM with --{self.key} /path/to/{self.key}. You can also turn "
                f"{self.name} off in Actions → Linked Agents.")


class CodexHarness(Harness):
    key = "codex"
    name = "Codex"
    default_executable = "codex"
    managed_activity_title = True
    finish_notifications = True
    usage_label = "Codex usage"
    usage_classes = ("usage-codex",)

    def fetch_usage(self, executable: str, cancel) -> "UsageSnapshot":
        from .usage import fetch_usage
        return fetch_usage(executable, cancel=cancel)

    def resolve(self, window) -> str | None:
        # Reuse the window's Codex resolver: it is also the seam for the
        # Session sign-in flows and for tests that intercept Codex lookups.
        return window.resolve_codex()

    def agent_command(self, executable: str) -> list[str]:
        return codex_agent_command(executable)

    def read_activity(self, session) -> bool | None:
        return title_activity(session.terminal.get_window_title(),
                              managed=session.managed_activity_title)

    def activity_finished(self, session, busy_title: str | None) -> bool:
        # Read completion without relaxing idle-only broadcast eligibility.
        if session.agent_identity is None:
            return False
        if not title_finished(session.terminal.get_window_title(), busy_title,
                              managed=session.managed_activity_title):
            return False
        return not session.managed_activity_title or session._read_activity() is False

    def logout_blocked(self, app) -> bool:
        return app.logout_pending


class HermesHarness(Harness):
    key = "hermes"
    name = "Hermes"
    default_executable = "hermes"
    not_ready_status = "Not working"
    usage_label = "Hermes usage"
    usage_accent = "#e8a35c"
    usage_classes = ("usage-hermes",)

    def fetch_usage(self, executable: str, cancel) -> "UsageSnapshot":
        from .hermes_usage import fetch_hermes_usage
        return fetch_hermes_usage(executable, cancel=cancel)

    def agent_command(self, executable: str) -> list[str]:
        return hermes_agent_command(executable)

    def read_activity(self, session) -> bool | None:
        activity = hermes_title_activity(session.terminal.get_window_title())
        if activity is not None:
            return activity
        # Classic Hermes keeps its composer at the live cursor, including
        # while tools run. Use absolute terminal rows so scrolling back does
        # not revive an old busy prompt. Its input area is at most eight rows.
        pty = session.terminal.get_pty()
        if pty is None:
            return None
        try:
            if termios.tcgetattr(pty.get_fd())[3] & (termios.ICANON | termios.ECHO):
                return None
        except (OSError, termios.error):
            return None
        _column, row = session.terminal.get_cursor_position()
        columns = session.terminal.get_column_count()
        screen_start = max(0, int(session.terminal.get_vadjustment().get_upper()) - session.terminal.get_row_count())
        read_range = getattr(session.terminal, "get_text_range_format", None)
        lines = []
        for line in range(max(screen_start, row - 9), row + 1):
            if read_range is not None:
                text, _length = read_range(Vte.Format.TEXT, line, 0, line, columns)
            else:
                # Debian 12's VTE 0.70 predates get_text_range_format.
                text, _attributes = session.terminal.get_text_range(line, 0, line, columns, None, None)
            lines.append((text or "").rstrip("\r\n"))
        return hermes_prompt_activity(lines, columns=columns)


HARNESS_ORDER = (CodexHarness(), HermesHarness())
HARNESSES = {spec.key: spec for spec in HARNESS_ORDER}
# New agent tabs from a shell or a non-agent tab use this harness.
DEFAULT_HARNESS = "codex"
