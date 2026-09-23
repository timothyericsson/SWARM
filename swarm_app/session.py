"""A real VTE terminal and its directly spawned child process.

Public widget methods must be called on the GTK main thread. A delivery result
means that input reached the terminal, not that Codex accepted or completed it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import termios
import threading
import time
from typing import Callable
import unicodedata

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gio, GLib, Gtk, Vte

from .codex_detection import DetectedCodex, find_foreground_codex
from .activity import title_activity, title_is_ready


MAX_MESSAGE_BYTES = 64 * 1024
PASTE_SETTLE_MS = 80
SLEEPER_SUBMIT_COOLDOWN_SECONDS = 1.0
_LOG = logging.getLogger(__name__)
Completion = Callable[[bool], None]


def normalize_message(message: str) -> str:
    """Preserve message formatting while rejecting terminal control sequences."""
    if not isinstance(message, str):
        raise ValueError("Enter a text message.")
    message = message.replace("\r\n", "\n").replace("\r", "\n")
    if not message.strip():
        raise ValueError("Enter a message to broadcast.")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in message):
        raise ValueError("Messages cannot contain terminal control characters.")
    try:
        length = len(message.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("The message contains invalid Unicode.") from exc
    if length > MAX_MESSAGE_BYTES:
        raise ValueError("Keep broadcasts under 64 KiB of text.")
    return message


@dataclass(frozen=True)
class _Process:
    pid: int
    session: int
    start: int
    state: str
    group: int


def _read_process(pid: int) -> _Process | None:
    """Read Linux identity data without trusting a reusable PID alone."""
    try:
        directory = Path("/proc") / str(pid)
        if directory.stat().st_uid != os.getuid():
            return None
        # A process name may contain spaces and parentheses.
        fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        return _Process(pid, int(fields[3]), int(fields[19]), fields[0], int(fields[2]))
    except (OSError, ValueError, IndexError):
        return None


def _same_process(process: _Process) -> bool:
    current = _read_process(process.pid)
    return current is not None and current.start == process.start and current.state != "Z"


def _process_directory(process: _Process) -> str | None:
    """Resolve a live process's real cwd, rejecting removed or replaced paths."""
    try:
        before = _read_process(process.pid)
        if before is None or before.start != process.start or before.session != process.session or before.state == "Z":
            return None
        proc_cwd = Path("/proc") / str(process.pid) / "cwd"
        directory = os.readlink(proc_cwd)
        # /proc can still resolve an unlinked cwd. Comparing the actual inode to
        # its pathname rejects that case, including a replacement at that name.
        actual = proc_cwd.stat()
        named = Path(directory).stat()
        after = _read_process(process.pid)
        if (
            after is None or after.start != process.start
            or after.session != process.session or after.state == "Z"
            or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
            or not Path(directory).is_dir()
        ):
            return None
        return directory
    except (OSError, ValueError):
        return None


def _session_processes(leader: _Process) -> list[_Process]:
    if leader.session != leader.pid:
        # VTE creates a private session. Fail closed if that assumption changes.
        return [leader] if _same_process(leader) else []
    processes = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            process = _read_process(int(entry.name))
            if process is not None and process.session == leader.session and process.state != "Z":
                processes.append(process)
    return processes


def _signal_process(process: _Process, signum: int) -> None:
    """Use a Linux pidfd so PID reuse cannot signal an unrelated process."""
    descriptor = None
    try:
        descriptor = os.pidfd_open(process.pid)
        if _same_process(process):
            signal.pidfd_send_signal(descriptor, signum)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _terminate_processes(leader: _Process | None) -> None:
    if leader is None or not _same_process(leader):
        return
    owned = _session_processes(leader)
    for process in owned:
        _signal_process(process, signal.SIGTERM)

    def finish() -> None:
        # Only discover new descendants if an original member still proves that
        # this is our session. The PID and its start time must both match.
        remaining = [process for process in owned if _same_process(process)]
        if remaining:
            remaining = _session_processes(leader)
        for process in remaining:
            _signal_process(process, signal.SIGKILL)

    # No GTK calls happen here. A non-daemon timer also finishes cleanup when the
    # final window closes and the GTK main loop has already stopped.
    cleanup = threading.Timer(0.6, finish)
    cleanup.daemon = False
    cleanup.start()


class TerminalSession(Gtk.Box):
    def __init__(self, title: str, kind: str, directory: str, on_change: Callable,
                 *, managed_activity_title: bool = False):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        if kind not in {"agent", "login", "shell"}:
            raise ValueError("Session kind must be agent, login, or shell.")
        self.title = title
        self.kind = kind
        self.origin_kind = kind
        self.managed_activity_title = managed_activity_title
        self.activity: bool | None = None
        self._last_agent_idle = False
        self._sleeper_title_ready = False
        self._sleeper_blocked_until = 0.0
        self.shell_title = title if kind == "shell" else None
        self.detected_agent_title: str | None = None
        self._detected_codex: DetectedCodex | None = None
        self._configured_codex = "codex"
        self._shell_executable: tuple[int, int] | None = None
        self.directory = directory
        self.state = "starting"
        self.error = ""
        self.pid: int | None = None
        self.exit_code: int | None = None
        self._process: _Process | None = None
        self._on_change = on_change
        self._started = False
        self._child_exited = False
        self._cancellable = Gio.Cancellable()
        self._pending: deque[tuple[str, Completion, tuple[int, int, int], bool]] = deque()
        self._active: tuple[str, Completion, tuple[int, int, int], bool] | None = None
        self._submit_source = 0

        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(10000)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_audible_bell(False)
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        self.terminal.connect("child-exited", self._child_exit)
        self.terminal.connect("window-title-changed", self._activity_title_changed)
        scrollbar = Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL,
            adjustment=self.terminal.get_vadjustment(),
        )
        self.pack_start(self.terminal, True, True, 0)
        self.pack_start(scrollbar, False, False, 0)
        self.connect("destroy", lambda _widget: self.close())

    @property
    def is_detected_agent(self) -> bool:
        return self.origin_kind == "shell" and self.kind == "agent"

    @property
    def agent_busy(self) -> bool:
        return self.kind == "agent" and self.state == "running" and self.activity is True

    @property
    def agent_idle(self) -> bool:
        """Affirmative live Ready state, independent of broadcast delivery gates."""
        return (
            self.kind == "agent"
            and self.state == "running"
            and title_is_ready(self.terminal.get_window_title(), managed=self.managed_activity_title)
            and self._read_activity() is False
        )

    def _read_activity(self) -> bool | None:
        if self.kind != "agent" or self.state != "running" or self._process is None:
            return None
        if self.origin_kind == "shell" and self._detected_codex is None:
            return None
        current = _read_process(self._process.pid)
        if (current is None or current.start != self._process.start
                or current.state in {"Z", "X", "x", "T", "t"}):
            return None
        return title_activity(self.terminal.get_window_title(), managed=self.managed_activity_title)

    def refresh_activity(self) -> None:
        activity = self._read_activity()
        if activity is True:
            # The title has acknowledged work, so a later explicit Ready can
            # qualify immediately even when the operation finishes quickly.
            self._sleeper_blocked_until = 0.0
        idle = self.agent_idle
        ready = self._has_ready_title()
        if (activity is not self.activity or idle != self._last_agent_idle
                or ready != self._sleeper_title_ready):
            self.activity = activity
            self._last_agent_idle = idle
            self._sleeper_title_ready = ready
            self._changed()

    def _has_ready_title(self) -> bool:
        return (
            time.monotonic() >= self._sleeper_blocked_until
            and self.agent_idle
        )

    def _activity_title_changed(self, *_args) -> None:
        self.refresh_activity()

    def _root_executable(self) -> tuple[int, int] | None:
        try:
            info = (Path("/proc") / str(self.pid) / "exe").stat()
            return info.st_dev, info.st_ino
        except OSError:
            return None

    def _shell_is_alive(self) -> bool:
        leader = self._process
        if leader is None or not _same_process(leader):
            return False
        try:
            link = Path("/proc") / str(leader.pid) / "exe"
            executable = os.readlink(link)
            identity = self._root_executable()
            if identity is None or os.readlink(link) != executable or not _same_process(leader):
                return False
            # Users can replace their initial shell with `exec bash`, `exec
            # zsh`, etc. That still leaves a terminal to return to after Codex.
            if Path(executable).name in {"sh", "bash", "dash", "zsh", "fish", "ksh", "ksh93", "csh", "tcsh", "nu", "elvish"}:
                self._shell_executable = identity
            return identity == self._shell_executable
        except OSError:
            return False

    @staticmethod
    def _agent_identity(agent: DetectedCodex) -> tuple[int, int, int]:
        return agent.pid, agent.start, agent.group

    def _foreground_codex(self) -> DetectedCodex | None:
        leader = self._process
        if self.origin_kind != "shell" or self.state != "running" or leader is None:
            return None
        pty = self.terminal.get_pty()
        if pty is None:
            return None
        try:
            foreground = os.tcgetpgrp(pty.get_fd())
            agent = find_foreground_codex(leader.pid, leader.start, foreground, self._configured_codex)
            if agent is None or os.tcgetpgrp(pty.get_fd()) != foreground or not _same_process(leader):
                return None
            return agent
        except OSError:
            return None

    def refresh_agent(self, codex: str) -> None:
        """Temporarily classify an interactive foreground Codex as an agent."""
        self._configured_codex = codex
        if self.origin_kind != "shell" or self.state != "running":
            return
        previous = self._detected_codex
        detected = self._foreground_codex()
        if detected is None:
            if previous is None and self.kind != "agent":
                return
            self._detected_codex = None
            self._cancel_deliveries()
        elif previous is not None and self._agent_identity(detected) == self._agent_identity(previous):
            self._detected_codex = detected
            if detected.directory is not None:
                self.directory = detected.directory
            return
        else:
            self._detected_codex = None
            self._cancel_deliveries()
            self._detected_codex = detected
            self.kind = "agent"
            if self.detected_agent_title is not None:
                self.title = self.detected_agent_title
            if detected.directory is not None:
                self.directory = detected.directory
            self._changed()
            return
        # With `exec codex` there is no shell to return to. Keep its agent
        # classification until child-exited supplies the final killed state.
        if self._shell_is_alive():
            self.detected_agent_title = self.title
            self.kind = "shell"
            self.title = self.shell_title
            self._changed()

    def working_directory(self) -> str | None:
        """Get the shell's current folder without executing anything in it.

        A foreground nested shell or command may have its own cwd. Only members
        of this terminal's private process session can supply that directory.
        Agent and login tabs retain their launch folder.
        """
        if self.is_detected_agent and self._detected_codex is not None:
            return self._detected_codex.directory
        if self.kind != "shell":
            return self.directory
        if self.state == "starting":
            return self.directory if Path(self.directory).is_dir() else None
        leader = self._process
        if self.state != "running" or leader is None or not _same_process(leader):
            return None
        pty = self.terminal.get_pty()
        if pty is None:
            return None
        try:
            foreground = os.tcgetpgrp(pty.get_fd())
        except OSError:
            return None
        if foreground <= 0 or leader.session != leader.pid:
            return None
        target = leader
        if foreground != leader.pid:
            candidate = _read_process(foreground)
            if candidate is None or candidate.group != foreground or candidate.session != leader.session or candidate.state == "Z":
                # A pipeline's process-group leader may already have exited.
                members = [p for p in _session_processes(leader) if p.group == foreground]
                candidate = min(members, key=lambda p: (p.start, p.pid), default=None)
            if candidate is None or candidate.session != leader.session:
                # A privileged or inaccessible foreground shell may have cd'ed
                # elsewhere. Its parent's cwd must not masquerade as its own.
                return None
            target = candidate
        directory = _process_directory(target)
        # A foreground job can finish between the reads above. Avoid returning
        # its old cwd after the terminal has switched back to the parent shell.
        try:
            if os.tcgetpgrp(pty.get_fd()) != foreground or not _same_process(leader):
                return None
        except OSError:
            return None
        return directory

    @property
    def can_broadcast(self) -> bool:
        return self._broadcast_target() is not None

    @property
    def broadcast_identity(self) -> tuple[int, int, int] | None:
        """The currently validated agent identity for a bound user selection."""
        return self._broadcast_target()

    @property
    def can_receive_sleeper_broadcast(self) -> bool:
        """Ready now, with no other message already being delivered to the TUI."""
        return (
            self._active is None
            and not self._pending
            and self._has_ready_title()
            and self._broadcast_target() is not None
        )

    def _broadcast_target(self) -> tuple[int, int, int] | None:
        live = (
            self.kind == "agent"
            and self.state == "running"
            and self._process is not None
            and _same_process(self._process)
        )
        if not live:
            return None
        if self.origin_kind == "shell":
            detected = self._foreground_codex()
            if (detected is None or self._detected_codex is None
                    or self._agent_identity(detected) != self._agent_identity(self._detected_codex)):
                return None
            target = self._agent_identity(detected)
        else:
            target = self._process.pid, self._process.start, self._process.group
        pty = self.terminal.get_pty()
        if pty is None:
            return None
        try:
            flags = termios.tcgetattr(pty.get_fd())[3]
            if self.origin_kind == "shell" and os.tcgetpgrp(pty.get_fd()) != target[2]:
                return None
        except (OSError, termios.error):
            return None
        # Codex's terminal UI uses raw input. Do not paste into canonical startup
        # input before that UI takes control. Raw mode alone cannot distinguish
        # a ready composer from an onboarding dialog; callers document that.
        return target if not flags & (termios.ICANON | termios.ECHO) else None

    def _changed(self) -> None:
        # Promotion may follow the first OSC title update; re-read the current
        # title here. Demotion and child exit also clear stale busy state here.
        self.activity = self._read_activity()
        self._last_agent_idle = self.agent_idle
        self._sleeper_title_ready = self._has_ready_title()
        try:
            self._on_change(self)
        except Exception:
            _LOG.exception("Session change callback failed")

    @staticmethod
    def _complete(callback: Completion, success: bool) -> None:
        try:
            callback(success)
        except Exception:
            _LOG.exception("Broadcast completion callback failed")

    def start(self, argv: list[str]) -> None:
        """Spawn one program directly; shell expansion is deliberately absent."""
        if self._started or self.state == "closed":
            raise RuntimeError("This terminal session has already started or closed.")
        if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise ValueError("Provide a valid command and arguments.")
        self._started = True
        try:
            self.terminal.spawn_async(
                pty_flags=Vte.PtyFlags.DEFAULT,
                working_directory=self.directory,
                argv=argv,
                envv=None,
                spawn_flags=GLib.SpawnFlags.SEARCH_PATH,
                child_setup=None,
                timeout=-1,
                cancellable=self._cancellable,
                callback=self._spawned,
                user_data=None,
            )
        except (GLib.Error, OSError, ValueError) as exc:
            self.state = "failed"
            self.error = str(exc)
            self._changed()

    def _spawned(self, _terminal, pid: int, error, _user_data=None) -> None:
        if error is not None or pid <= 0:
            if self.state != "closed":
                self.state = "failed"
                self.error = error.message if error is not None else "Could not start the process."
                self._changed()
            return
        self.pid = pid
        self._process = _read_process(pid)
        if self.origin_kind == "shell":
            self._shell_executable = self._root_executable()
        if self.state == "closed":
            _terminate_processes(self._process)
            return
        # A short-lived program can exit before the asynchronous spawn callback.
        self.state = "exited" if self._child_exited else "running"
        self._changed()

    def _child_exit(self, _terminal, status: int) -> None:
        self._child_exited = True
        try:
            self.exit_code = os.waitstatus_to_exitcode(status)
        except ValueError:
            self.exit_code = status
        if self.state != "closed":
            self.state = "exited"
        self._cancel_deliveries()
        self.terminal.set_input_enabled(False)
        self._changed()

    def broadcast(
        self,
        message: str,
        on_complete: Completion,
        *,
        idle_only: bool = False,
        expected_target: tuple[int, int, int] | None = None,
    ) -> bool:
        """Deliver a paste and Enter, reporting completion once on every path.

        Idle-only delivery requires affirmative readiness now and at each input
        step. It never waits for a busy agent or an existing delivery to finish.
        A supplied expected_target binds the send to the selected live agent,
        rejecting a replacement process that later occupies the same tab.
        """
        message = normalize_message(message)
        target = self._broadcast_target()
        if target is None or (expected_target is not None and target != expected_target) or (
            idle_only and (self._active is not None or self._pending or not self._has_ready_title())
        ):
            self._complete(on_complete, False)
            return False
        delivery = (message, on_complete, target, idle_only)
        self._pending.append(delivery)
        if self._active is None:
            self._begin_delivery()
        # Starting the paste can synchronously reject a changed process or
        # fail. Report that refusal so a custom dialog keeps its draft open.
        return self._active is delivery or any(item is delivery for item in self._pending)

    def _begin_delivery(self) -> None:
        if self._active is not None or not self._pending:
            return
        if self._broadcast_target() != self._pending[0][2] or (
            self._pending[0][3] and not self._has_ready_title()
        ):
            self._cancel_deliveries()
            return
        self._active = self._pending.popleft()
        try:
            # VTE observes the child's bracketed-paste mode and preserves a
            # multiline prompt as a paste instead of synthetic Enter presses.
            self.terminal.paste_text(self._active[0])
            self._submit_source = GLib.timeout_add(PASTE_SETTLE_MS, self._submit)
        except (GLib.Error, RuntimeError):
            self._cancel_deliveries()

    def _submit(self) -> bool:
        self._submit_source = 0
        if self._active is None:
            return GLib.SOURCE_REMOVE
        if self._broadcast_target() != self._active[2] or (
            self._active[3] and not self._has_ready_title()
        ):
            self._cancel_deliveries()
            return GLib.SOURCE_REMOVE
        _message, callback, _target, _idle_only = self._active
        self._active = None
        try:
            self.terminal.feed_child(b"\r")
        except (GLib.Error, RuntimeError):
            self._complete(callback, False)
            self._cancel_deliveries()
            return GLib.SOURCE_REMOVE
        # Enter can reach Codex before its next OSC title update. Suppress a
        # second sleeper send during that gap after any kind of broadcast.
        # Slash commands that never become busy recover after this deadline,
        # provided a fresh eligibility check still finds an explicit Ready.
        self._sleeper_blocked_until = time.monotonic() + SLEEPER_SUBMIT_COOLDOWN_SECONDS
        self._complete(callback, True)
        self._begin_delivery()
        return GLib.SOURCE_REMOVE

    def _cancel_deliveries(self) -> None:
        if self._submit_source:
            GLib.source_remove(self._submit_source)
            self._submit_source = 0
        cancelled = list(self._pending)
        self._pending.clear()
        if self._active is not None:
            cancelled.insert(0, self._active)
            self._active = None
        for _message, callback, _target, _idle_only in cancelled:
            self._complete(callback, False)

    def close(self) -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        self._cancellable.cancel()
        self._cancel_deliveries()
        _terminate_processes(self._process)
        self.terminal.set_input_enabled(False)
        self._changed()
