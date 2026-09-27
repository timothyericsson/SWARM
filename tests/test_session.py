"""Run with xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -v."""

import json
import os
from dataclasses import replace
from pathlib import Path
import shlex
import tempfile
import time
import unittest
from unittest.mock import patch

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from swarm_app.session import MAX_MESSAGE_BYTES, TerminalSession, normalize_message
from swarm_app.codex_detection import DetectedCodex


FIXTURE = Path(__file__).parent / "fixtures" / "fake_agent.py"
GTK_AVAILABLE = Gtk.init_check([])[0]


def pump_until(condition, timeout=4):
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not condition():
        while context.pending():
            context.iteration(False)
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for terminal state")
        time.sleep(0.005)
    while context.pending():
        context.iteration(False)


def process_alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


class MessageTests(unittest.TestCase):
    def test_normalize_preserves_unicode_and_indentation(self):
        self.assertEqual(normalize_message("  café\r\n\t日本語\rthird\n"), "  café\n\t日本語\nthird\n")

    def test_empty_and_terminal_controls_rejected(self):
        for message in ["", " \t\r\n", "before\0after", "\x1b[200~", "\x03stop", "\x7f", "a\x85b"]:
            with self.subTest(message=message), self.assertRaises(ValueError):
                normalize_message(message)

    def test_limit_counts_utf8_bytes(self):
        self.assertEqual(len(normalize_message("a" * MAX_MESSAGE_BYTES)), MAX_MESSAGE_BYTES)
        with self.assertRaises(ValueError):
            normalize_message("é" * (MAX_MESSAGE_BYTES // 2 + 1))


@unittest.skipUnless(GTK_AVAILABLE, "A display or xvfb-run is required")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.sessions = []
        self.windows = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for window in self.windows:
            window.destroy()
        pump_until(lambda: all(s.pid is None or not process_alive(s.pid) for s in self.sessions))
        self.temp.cleanup()

    def new_session(self, kind="agent", mode="record", start=True):
        directory = self.folder / str(len(self.sessions))
        directory.mkdir()
        session = TerminalSession("Test", kind, str(directory), lambda _session: None)
        window = Gtk.Window()
        window.add(session)
        window.show_all()
        self.sessions.append(session)
        self.windows.append(window)
        if start:
            session.start(["/usr/bin/python3", str(FIXTURE), str(directory), mode])
        return session, directory

    def ready(self, session, directory):
        pump_until(lambda: session.state == "running" and (directory / "ready").exists())
        # Parsing the output escape sequence happens in VTE's next frame.
        started = time.monotonic()
        pump_until(lambda: time.monotonic() - started > 0.12)

    def real_shell(self):
        session, directory = self.new_session(kind="shell", start=False)
        session.start(["/bin/bash", "--noprofile", "--norc", "-i"])
        pump_until(lambda: session.state == "running" and session.working_directory() == str(directory))
        return session, directory

    @staticmethod
    def shell_command(session, command):
        session.terminal.feed_child((command + "\r").encode("utf-8"))

    def test_multiline_paste_unicode_and_immediate_enter(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        results = []
        self.assertTrue(session.broadcast("café\r\n日本語\n\tend", results.append))
        pump_until(lambda: bool(results))
        expected = b"\x1b[200~" + "café\r日本語\r\tend".encode() + b"\x1b[201~\r"
        pump_until(lambda: (directory / "input").exists() and (directory / "input").read_bytes().endswith(b"\r"))
        self.assertEqual((directory / "input").read_bytes(), expected)
        self.assertEqual(results, [True])

    def test_back_to_back_broadcasts_are_serialized(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        results = []
        for message in ["one", "two", "three"]:
            self.assertTrue(session.broadcast(message, results.append))
        pump_until(lambda: len(results) == 3)
        expected = b"".join(b"\x1b[200~" + word + b"\x1b[201~\r" for word in [b"one", b"two", b"three"])
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        self.assertEqual(results, [True, True, True])

    def test_login_session_rejects_broadcast(self):
        session, directory = self.new_session(kind="login")
        self.ready(session, directory)
        results = []
        self.assertFalse(session.broadcast("never send this", results.append))
        self.assertEqual(results, [False])
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_canonical_startup_input_rejects_broadcast(self):
        session, directory = self.new_session(mode="canonical")
        self.ready(session, directory)
        results = []
        self.assertFalse(session.broadcast("never send before the TUI", results.append))
        self.assertEqual(results, [False])
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_agent_identity_is_independent_of_raw_input(self):
        session, directory = self.new_session(mode="canonical")
        self.ready(session, directory)
        process = session._process
        self.assertEqual(session.agent_identity, (process.pid, process.start, process.group))
        self.assertIsNone(session.broadcast_identity)

    def test_agent_identity_rejects_inactive_replaced_and_closed_processes(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        process = session._process
        for state in ("Z", "X", "x", "T", "t"):
            with self.subTest(state=state), patch("swarm_app.session._read_process",
                                                return_value=replace(process, state=state)):
                self.assertIsNone(session.agent_identity)
        with patch("swarm_app.session._read_process", return_value=replace(process, start=process.start + 1)):
            self.assertIsNone(session.agent_identity)
        with patch("swarm_app.session._read_process", return_value=None):
            self.assertIsNone(session.agent_identity)
        session.close()
        self.assertIsNone(session.agent_identity)

    def test_manual_completion_requires_original_foreground_agent(self):
        session, directory = self.new_session(kind="shell")
        self.ready(session, directory)
        process = session._process
        detected = DetectedCodex(process.pid, process.start, process.session, process.group, str(directory))
        session.kind = "agent"
        session._detected_codex = detected
        session.terminal.feed(b"\x1b]0;Task | project\x07")
        pump_until(lambda: session.terminal.get_window_title() == "Task | project")
        with patch.object(session, "_foreground_codex", return_value=detected):
            self.assertEqual(session.agent_identity, (process.pid, process.start, process.group))
            self.assertTrue(session.activity_finished("⠋ Task | project"))
            self.assertFalse(session.activity_finished("Task | project"))
            self.assertFalse(session.agent_idle)
            self.assertFalse(session.can_receive_sleeper_broadcast)
        for current in (None, replace(detected, start=detected.start + 1)):
            with self.subTest(current=current), patch.object(session, "_foreground_codex", return_value=current):
                self.assertIsNone(session.agent_identity)
                self.assertFalse(session.activity_finished("⠋ Task | project"))

    def test_managed_completion_needs_live_ready_status(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        session.managed_activity_title = True
        session.terminal.feed(b"\x1b]0;Ready\x07")
        pump_until(lambda: session.terminal.get_window_title() == "Ready")
        self.assertTrue(session.activity_finished("Working"))
        with patch.object(session, "_read_activity", return_value=None):
            self.assertFalse(session.activity_finished("Working"))
        session.close()
        self.assertFalse(session.activity_finished("Working"))

    def test_shell_rejects_broadcast_even_with_raw_input(self):
        session, directory = self.new_session(kind="shell")
        self.ready(session, directory)
        results = []
        self.assertFalse(session.can_broadcast)
        self.assertFalse(session.broadcast("do not execute in the shell", results.append))
        self.assertEqual(results, [False])
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_real_shell_cwd_tracks_cd_with_spaces_and_unicode(self):
        session, directory = self.real_shell()
        target = directory / "my café 日本語 project"
        target.mkdir()
        self.shell_command(session, "cd -- " + shlex.quote(str(target)))
        pump_until(lambda: session.working_directory() == str(target))
        self.assertEqual(session.directory, str(directory))
        self.shell_command(session, "cd ..")
        pump_until(lambda: session.working_directory() == str(directory))

    def test_nested_foreground_shell_supplies_its_own_cwd(self):
        session, directory = self.real_shell()
        target = directory / "nested project"
        target.mkdir()
        self.shell_command(session, "/bin/bash --noprofile --norc -i")
        pump_until(lambda: os.tcgetpgrp(session.terminal.get_pty().get_fd()) != session.pid)
        self.shell_command(session, "cd -- " + shlex.quote(str(target)))
        pump_until(lambda: session.working_directory() == str(target))
        self.assertEqual(os.readlink(f"/proc/{session.pid}/cwd"), str(directory))
        self.shell_command(session, "exit")
        pump_until(lambda: session.working_directory() == str(directory))

    def test_unverifiable_foreground_never_falls_back_to_parent_shell_folder(self):
        session, directory = self.real_shell()
        self.assertEqual(session.working_directory(), str(directory))
        # Cover an inaccessible/missing job, a real group outside this terminal
        # session, and a transient absence of any foreground process group.
        for foreground in (2147483646, os.getpgrp(), 0, -1):
            with self.subTest(foreground=foreground):
                with patch("swarm_app.session.os.tcgetpgrp", return_value=foreground):
                    self.assertIsNone(session.working_directory())
        self.assertEqual(session.working_directory(), str(directory))

    def test_deleted_shell_cwd_is_unavailable(self):
        session, directory = self.real_shell()
        target = directory / "removed project"
        target.mkdir()
        self.shell_command(session, "cd -- " + shlex.quote(str(target)))
        pump_until(lambda: session.working_directory() == str(target))
        target.rmdir()
        self.assertIsNone(session.working_directory())

    def test_exited_and_closed_shells_have_no_current_directory(self):
        session, _directory = self.real_shell()
        self.shell_command(session, "exit")
        pump_until(lambda: session.state == "exited")
        self.assertIsNone(session.working_directory())
        session.close()
        self.assertIsNone(session.working_directory())

    def test_shell_starting_uses_valid_initial_directory(self):
        session, directory = self.new_session(kind="shell", start=False)
        self.assertEqual(session.working_directory(), str(directory))
        directory.rmdir()
        self.assertIsNone(session.working_directory())

    def test_close_cancels_inflight_and_queued_broadcasts_once(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        results = []
        session.broadcast("one", results.append)
        session.broadcast("two", results.append)
        session.close()
        session.close()
        self.assertEqual(results, [False, False])
        self.assertEqual(session.state, "closed")
        self.assertFalse(session.broadcast("three", results.append))
        self.assertEqual(results, [False, False, False])

    def test_rapid_close_during_spawn_remains_closed(self):
        session, directory = self.new_session()
        session.close()
        started = time.monotonic()
        pump_until(lambda: time.monotonic() - started > 0.4)
        self.assertEqual(session.state, "closed")
        if (directory / "ready").exists():
            child = json.loads((directory / "ready").read_text())["pid"]
            pump_until(lambda: not process_alive(child))

    def test_missing_executable_marks_failed(self):
        session, _directory = self.new_session(start=False)
        session.start(["/no-such-swarm-test-program"])
        pump_until(lambda: session.state == "failed")
        self.assertTrue(session.error)
        self.assertFalse(session.can_broadcast)

    def test_exit_status_and_no_send_to_exited_process(self):
        session, _directory = self.new_session(mode="exit")
        pump_until(lambda: session.state == "exited")
        self.assertEqual(session.exit_code, 7)
        results = []
        self.assertFalse(session.broadcast("do not send", results.append))
        self.assertEqual(results, [False])

    def test_close_kills_descendant_in_its_own_process_group(self):
        session, directory = self.new_session(mode="stubborn")
        self.ready(session, directory)
        descendant = int((directory / "descendant").read_text())
        self.assertTrue(process_alive(descendant))
        self.assertEqual(os.getpgid(descendant), descendant)
        session.close()
        pump_until(lambda: not process_alive(descendant))
        self.assertTrue(process_alive(os.getpid()))


if __name__ == "__main__":
    unittest.main()
