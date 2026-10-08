"""Run with xvfb-run -a /usr/bin/python3 -m unittest discover -s tests -v."""

import json
import os
from dataclasses import replace
from pathlib import Path
import shlex
import signal
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

    def new_session(self, kind="agent", mode="record", start=True, harness="codex"):
        directory = self.folder / str(len(self.sessions))
        directory.mkdir()
        session = TerminalSession("Test", kind, str(directory), lambda _session: None, harness=harness)
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

    def test_image_broadcasts_keep_distinct_paths_when_queued(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        first = self.folder / "first image.png"
        second = self.folder / "second's image.png"
        first.write_bytes(b"fixture")
        second.write_bytes(b"fixture")
        results = []
        self.assertTrue(session.broadcast("one", results.append, images=(str(first), str(second))))
        queued = [str(second), str(first)]
        self.assertTrue(session.broadcast("", results.append, images=queued))
        queued.clear()
        pump_until(lambda: len(results) == 2)
        paste = lambda text: b"\x1b[200~" + text.encode() + b"\x1b[201~"
        expected = (paste(shlex.quote(str(first))) + paste(shlex.quote(str(second))) + paste("one") + b"\r"
                    + paste(shlex.quote(str(second))) + paste(shlex.quote(str(first))) + b"\r")
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        self.assertEqual(results, [True, True])
        self.assertNotIn(b"\x16", expected)

    def test_hermes_image_path_stays_separate_from_message_paste(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        path = self.folder / "image with spaces and 'apostrophes'.png"
        path.write_bytes(b"fixture")
        results = []
        self.assertTrue(session.broadcast("describe this", results.append, images=(str(path),)))
        pump_until(lambda: bool(results))
        expected = (b"\x1b[200~" + (path.as_uri() + " ").encode()
                    + b"\x1b[201~\x1b[200~describe this"
                    + b"\x1b[201~\r")
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        self.assertEqual(results, [True])

    def test_hermes_single_image_without_text_submits_native_file_uri(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        path = self.folder / "image's path.png"
        path.write_bytes(b"fixture")
        results = []
        self.assertTrue(session.broadcast("", results.append, images=(str(path),)))
        pump_until(lambda: bool(results))
        expected = b"\x1b[200~" + path.as_uri().encode() + b"\x1b[201~\r"
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        self.assertEqual(results, [True])

    def test_hermes_multi_image_broadcast_keeps_ordered_file_references_in_one_turn(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        images = (self.folder / "first.png", self.folder / "second's image.png",
                  self.folder / 'third "image".png')
        for path in images:
            path.write_bytes(b"fixture")
        results = []
        expected = b""
        for text in ("", "compare these"):
            self.assertTrue(session.broadcast(text, results.append,
                                              images=tuple(str(path) for path in images)))
            note = ("Additional image attachments (local files, in paste order; inspect each with image tools):\n"
                    f"Image 2: {images[1].as_uri()}\nImage 3: {images[2].as_uri()}")
            payload = (text + "\n\n" if text else "") + note
            expected += (b"\x1b[200~" + (images[0].as_uri() + " ").encode()
                         + b"\x1b[201~\x1b[200~" + payload.replace("\n", "\r").encode()
                         + b"\x1b[201~\r")
        pump_until(lambda: len(results) == 2)
        self.assertEqual(results, [True, True])
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        # Each complete set uses a native path and message paste, then one submit key. The
        # references remain inside the paste, never extra submitted turns.
        self.assertEqual(expected.count(b"\x1b[200~"), 4)
        self.assertEqual(expected.count(b"\x1b[201~\r"), 2)

    def test_custom_harness_accepts_text_without_inferred_activity_or_images(self):
        session, directory = self.new_session(harness="custom")
        self.ready(session, directory)
        session.terminal.feed(b"\x1b]0;Ready\x07")
        pump_until(lambda: session.terminal.get_window_title() == "Ready")
        self.assertIsNone(session._read_activity())
        self.assertFalse(session.agent_idle)
        self.assertFalse(session.can_receive_sleeper_broadcast)
        self.assertFalse(session.supports_image_broadcast)
        results = []
        path = self.folder / "image.png"
        path.write_bytes(b"fixture")
        self.assertFalse(session.broadcast("", results.append, images=(str(path),)))
        self.assertTrue(session.broadcast("hello", results.append))
        pump_until(lambda: len(results) == 2)
        self.assertEqual(results, [False, True])

    def test_close_cancels_queued_image_before_submission(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        path = self.folder / "image.png"
        path.write_bytes(b"fixture")
        results = []
        self.assertTrue(session.broadcast("one", results.append))
        self.assertTrue(session.broadcast("", results.append, images=(str(path),)))
        session.close()
        self.assertEqual(results, [False, False])

    def test_invalid_later_image_sends_no_partial_broadcast(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        image = self.folder / "valid.png"
        image.write_bytes(b"fixture")
        results = []
        for invalid in (str(self.folder / "missing.png"), 42, "\x1b[201~", str(directory)):
            with self.subTest(invalid=invalid):
                self.assertFalse(session.broadcast("don't send any of this", results.append,
                                                   images=(str(image), invalid)))
        self.assertEqual(results, [False] * 4)
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_image_removed_while_queued_rejects_entire_attachment_set(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        first = self.folder / "first.png"
        second = self.folder / "second.png"
        first.write_bytes(b"fixture")
        second.write_bytes(b"fixture")
        results = []
        self.assertTrue(session.broadcast("initial", results.append))
        self.assertTrue(session.broadcast("queued", results.append, images=(str(first), str(second))))
        second.unlink()
        pump_until(lambda: len(results) == 2)
        self.assertEqual(results, [True, False])
        expected = b"\x1b[200~initial\x1b[201~\r"
        pump_until(lambda: (directory / "input").read_bytes() == expected)

    def test_image_collection_rejects_string_instead_of_treating_it_as_paths(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        results = []
        self.assertFalse(session.broadcast("hello", results.append, images="/tmp/image.png"))
        self.assertFalse(session.broadcast("hello", results.append, images=None))
        self.assertEqual(results, [False, False])
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_interrupt_sends_only_escape_and_keeps_session_running(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        self.assertTrue(session.interrupt())
        pump_until(lambda: (directory / "input").read_bytes() == b"\x1b")
        self.assertEqual(session.state, "running")
        self.assertTrue(process_alive(session.pid))

    def test_interrupt_cancels_inflight_and_queued_broadcasts_without_enter(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        results = []
        self.assertTrue(session.broadcast("one", results.append))
        self.assertTrue(session.broadcast("two", results.append))
        self.assertTrue(session.interrupt())
        self.assertEqual(results, [False, False])
        started = time.monotonic()
        pump_until(lambda: time.monotonic() - started > 0.2)
        self.assertEqual((directory / "input").read_bytes(), b"\x1b[200~one\x1b[201~\x1b")
        self.assertEqual(results, [False, False])

    def test_interrupt_rejects_shell_login_and_canonical_startup(self):
        for kind, mode in (("shell", "record"), ("login", "record"), ("agent", "canonical")):
            with self.subTest(kind=kind, mode=mode):
                session, directory = self.new_session(kind=kind, mode=mode)
                self.ready(session, directory)
                self.assertFalse(session.interrupt())
                self.assertEqual((directory / "input").read_bytes(), b"")

    def test_interrupt_detected_agent_requires_original_foreground_process(self):
        session, directory = self.new_session(kind="shell")
        self.ready(session, directory)
        process = session._process
        detected = DetectedCodex(process.pid, process.start, process.session, process.group, str(directory))
        session.kind = "agent"
        session._detected_codex = detected
        for current in (None, replace(detected, start=detected.start + 1)):
            with self.subTest(current=current), patch.object(session, "_foreground_codex", return_value=current):
                self.assertFalse(session.interrupt())
                self.assertEqual((directory / "input").read_bytes(), b"")
        with patch.object(session, "_foreground_codex", return_value=detected):
            self.assertTrue(session.interrupt())
        pump_until(lambda: (directory / "input").read_bytes() == b"\x1b")

    def test_interrupt_rechecks_agent_after_cancelling_deliveries(self):
        session, directory = self.new_session(kind="shell")
        self.ready(session, directory)
        process = session._process
        detected = DetectedCodex(process.pid, process.start, process.session, process.group, str(directory))
        session.kind = "agent"
        session._detected_codex = detected
        results = []

        def demote_on_cancel(success):
            results.append(success)
            session.kind = "shell"
            session._detected_codex = None

        with patch.object(session, "_foreground_codex", return_value=detected):
            self.assertTrue(session.broadcast("one", demote_on_cancel))
            self.assertFalse(session.interrupt())
        self.assertEqual(results, [False])
        started = time.monotonic()
        pump_until(lambda: time.monotonic() - started > 0.2)
        self.assertEqual((directory / "input").read_bytes(), b"\x1b[200~one\x1b[201~")

    def test_interrupt_reports_failed_terminal_write(self):
        session, directory = self.new_session()
        self.ready(session, directory)
        for error in (GLib.Error("terminal unavailable"), RuntimeError("terminal unavailable")):
            with self.subTest(error=type(error).__name__), patch.object(session.terminal, "feed_child", side_effect=error):
                self.assertFalse(session.interrupt())
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_login_session_rejects_broadcast(self):
        session, directory = self.new_session(kind="login")
        self.ready(session, directory)
        results = []
        self.assertFalse(session.broadcast("never send this", results.append))
        self.assertEqual(results, [False])
        self.assertEqual((directory / "input").read_bytes(), b"")

    def test_hermes_receives_broadcasts_without_codex_activity_inference(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        for title in ("Working", "Ready", "⠋ Task | project", "Task | project"):
            with self.subTest(title=title):
                session.terminal.feed(f"\x1b]0;{title}\x07".encode())
                pump_until(lambda: session.terminal.get_window_title() == title)
                session.refresh_activity()
                self.assertIsNone(session.activity)
                self.assertFalse(session.agent_busy)
                self.assertFalse(session.agent_idle)
                self.assertFalse(session.can_receive_sleeper_broadcast)
                self.assertFalse(session.activity_finished("⠋ Task | project"))
        results = []
        self.assertTrue(session.broadcast("Hello Hermes", results.append))
        pump_until(lambda: bool(results))
        expected = b"\x1b[200~Hello Hermes\x1b[201~\r"
        pump_until(lambda: (directory / "input").read_bytes() == expected)
        self.assertEqual(results, [True])

    def test_hermes_native_titles_track_work_without_enabling_codex_readiness(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        for title, activity in (("⏳ Hermes", True), ("✓ Hermes", False),
                                ("⏳️ Hermes", True), ("⚠ Awaiting input", False),
                                ("Ready", None)):
            with self.subTest(title=title):
                session.terminal.feed(f"\x1b]0;{title}\x07".encode())
                pump_until(lambda: session.terminal.get_window_title() == title
                           and session.activity is activity)
                self.assertEqual(session.agent_busy, activity is True)
                self.assertFalse(session.agent_idle)
                self.assertFalse(session.can_receive_sleeper_broadcast)
                self.assertFalse(session.activity_finished("⏳ Hermes"))

    def test_hermes_classic_prompt_transitions_follow_terminal_output(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        rule = "─" * session.terminal.get_column_count()
        for index, (prompt, activity) in enumerate((("❯ ", False), ("⚕ Thinking...", True),
                                                  ("⚠ Confirm command", False), ("🔐 Password: ", False),
                                                  ("❯ ", False))):
            with self.subTest(prompt=prompt):
                title = f"Classic prompt {index}"
                session.terminal.feed(f"\x1b[2J\x1b[H{rule}\r\n{prompt}\x1b]0;{title}\x07".encode())
                pump_until(lambda: session.terminal.get_window_title() == title)
                session.refresh_activity()
                self.assertIs(session.activity, activity)
                self.assertEqual(session.agent_busy, activity is True)
                self.assertFalse(session.agent_idle)
                self.assertFalse(session.can_receive_sleeper_broadcast)
        session.terminal.feed(f"\x1b[2J\x1b[H{rule}\r\n⚕ \x1b]0;Compact prompt\x07".encode())
        pump_until(lambda: session.terminal.get_window_title() == "Compact prompt")
        session.refresh_activity()
        self.assertTrue(session.agent_busy)
        # An explicit native title takes precedence over a prompt still being redrawn.
        session.terminal.feed("\x1b]0;✓ Hermes\x07".encode())
        pump_until(lambda: session.activity is False)

    def test_hermes_working_indicator_does_not_change_direct_or_broadcast_input(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        session.terminal.feed("\x1b]0;⏳ Hermes\x07".encode())
        pump_until(lambda: session.agent_busy)
        session.terminal.feed_child(b"typed task\r")
        pump_until(lambda: (directory / "input").read_bytes() == b"typed task\r")
        self.assertTrue(session.agent_busy)
        results = []
        self.assertTrue(session.broadcast("Broadcast task", results.append))
        expected = b"typed task\r\x1b[200~Broadcast task\x1b[201~\r"
        pump_until(lambda: results == [True] and (directory / "input").read_bytes() == expected)
        self.assertTrue(session.agent_busy)
        session.terminal.feed("\x1b]0;✓ Hermes\x07".encode())
        pump_until(lambda: session.activity is False)

    def test_hermes_stopped_or_exited_process_clears_working_indicator(self):
        session, directory = self.new_session(harness="hermes")
        self.ready(session, directory)
        session.terminal.feed("\x1b]0;⏳ Hermes\x07".encode())
        pump_until(lambda: session.agent_busy)
        os.kill(session.pid, signal.SIGSTOP)
        try:
            pump_until(lambda: Path(f"/proc/{session.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T")
            session.refresh_activity()
            self.assertIsNone(session.activity)
            self.assertFalse(session.agent_busy)
        finally:
            os.kill(session.pid, signal.SIGCONT)
        pump_until(lambda: Path(f"/proc/{session.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T")
        session.refresh_activity()
        self.assertTrue(session.agent_busy)
        os.kill(session.pid, signal.SIGTERM)
        pump_until(lambda: session.state == "exited")
        self.assertIsNone(session.activity)
        self.assertFalse(session.agent_busy)

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

    def test_launch_environment_overrides_only_child_and_preserves_inherited_values(self):
        session, _directory = self.new_session(start=False)
        with patch.dict(os.environ, {"SWARM_TEST_INHERITED": "retained", "SWARM_TEST_KEY": "old"}), \
                patch.object(session.terminal, "spawn_async") as spawn:
            session.start(["/fake/agent"], environment={"SWARM_TEST_KEY": "new"})
            child = dict(value.split("=", 1) for value in spawn.call_args.kwargs["envv"])
            self.assertEqual(child["SWARM_TEST_INHERITED"], "retained")
            self.assertEqual(child["SWARM_TEST_KEY"], "new")
            self.assertEqual(os.environ["SWARM_TEST_KEY"], "old")
            self.assertEqual(spawn.call_args.kwargs["argv"], ["/fake/agent"])

    def test_invalid_launch_environment_never_spawns_or_displays_values(self):
        session, _directory = self.new_session(start=False)
        with patch.object(session.terminal, "spawn_async") as spawn:
            with self.assertRaises(ValueError) as caught:
                session.start(["/fake/agent"], environment={"KEY": "private-marker\0"})
            self.assertNotIn("private-marker", str(caught.exception))
            spawn.assert_not_called()

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
