"""Window and broadcast integration checks using local fake terminal processes."""

import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import shlex
import signal
import shutil
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from swarm_app.app import BroadcastDialog, Gdk, Gtk, SwarmApplication
from swarm_app.codex_detection import DetectedCodex
from swarm_app.session import TerminalSession
from gtk_test_support import shutdown_application
from test_session import FIXTURE, process_alive, pump_until


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deepseek_patch = patch("swarm_app.app.fetch_deepseek_usage", return_value=None)
        deepseek_patch.start()
        cls.addClassCleanup(deepseek_patch.stop)
        usage_patch = patch("swarm_app.app.fetch_zai_usage", return_value=None)
        usage_patch.start()
        cls.addClassCleanup(usage_patch.stop)
        cls.app = SwarmApplication("/tmp", "/no-such-swarm-codex",
                                   deepseek_key_path="/no-such-swarm-deepseek.txt")
        cls.app.register(None)
        cls.app.hold()

    @classmethod
    def tearDownClass(cls):
        cls.app.release()
        shutdown_application(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.app.directory = str(self.directory)
        self.app.auth_status = "Offline test"
        self.app.auth_state = "unknown"
        self.app.auth_pending = False
        self.app.auth_refresh_requested = False
        self.app.logout_pending = False
        self.window = self.app.new_window(start_terminal=False)
        self.windows = [self.window]
        self.sessions = []

    def tearDown(self):
        for window in self.windows:
            window.destroy()
        pump_until(lambda: all(s.pid is None or not process_alive(s.pid) for s in self.sessions))
        pump_until(lambda: not self.app.auth_pending and not self.app.logout_pending)
        self.temp.cleanup()

    def add(self, window=None, kind="agent", mode="record"):
        directory = self.directory / str(len(self.sessions))
        directory.mkdir()
        window = window or self.window
        session = window.add_session(f"Agent {len(self.sessions) + 1}", kind,
                                     ["/usr/bin/python3", str(FIXTURE), str(directory), mode], str(directory))
        self.sessions.append(session)
        if mode == "exit":
            pump_until(lambda: session.state == "exited")
        else:
            pump_until(lambda: session.state == "running" and (directory / "ready").exists())
            started = time.monotonic()
            pump_until(lambda: time.monotonic() - started > 0.12)
        return session, directory

    def test_no_tabs_keeps_workspace_empty(self):
        self.assertEqual(self.window.sessions, [])
        self.assertEqual(self.window.notebook.get_n_pages(), 0)
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertFalse(self.window.global_item.get_sensitive())

    def shell(self, window=None, directory=None):
        window = window or self.window
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]), \
                patch.dict(os.environ, {"HISTFILE": "/dev/null", "INPUTRC": "/dev/null"}):
            session = window.new_terminal(directory)
        self.sessions.append(session)
        pump_until(lambda: session.state == "running")
        return session

    def cd(self, session, target):
        session.terminal.feed_child(("cd -- " + shlex.quote(str(target)) + "\n").encode())
        pump_until(lambda: session.working_directory() == str(target))

    def test_default_new_window_starts_interactive_shell_without_agent(self):
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]), \
                patch.dict(os.environ, {"HISTFILE": "/dev/null", "INPUTRC": "/dev/null"}):
            window = self.app.new_window()
        self.windows.append(window)
        self.sessions.extend(window.sessions)
        self.assertEqual(len(window.sessions), 1)
        terminal = window.current()
        pump_until(lambda: terminal.state == "running")
        self.assertEqual(terminal.kind, "shell")
        self.assertEqual(terminal.title, "Terminal 1")
        self.assertEqual(terminal.working_directory(), str(self.directory))
        self.assertEqual(window.active_agents(), [])
        self.assertFalse(window.global_item.get_sensitive())

    def test_cd_then_new_agent_really_spawns_in_navigated_folder(self):
        shell = self.shell()
        project = self.directory / "project with spaces 日本語"
        project.mkdir()
        self.cd(shell, project)
        fake = self.directory / "codex-test"
        fake.write_text("#!/usr/bin/python3\nfrom pathlib import Path\nimport sys\n"
                        "(Path(__file__).parent / 'agent-started').write_text(str(Path.cwd()) + '\\n' + ' '.join(sys.argv[1:]))\n")
        fake.chmod(0o755)
        with patch.object(self.app, "codex", str(fake)):
            agent = self.window.new_agent()
        self.sessions.append(agent)
        marker = self.directory / "agent-started"
        pump_until(marker.exists)
        self.assertEqual(marker.read_text(), str(project) + '\n--yolo -c tui.terminal_title=["spinner","status"]')
        self.assertEqual(agent.directory, str(project))
        self.assertIn(shell, self.window.sessions)
        self.assertEqual(shell.state, "running")

    def test_new_agent_uses_selected_shell_not_another_tabs_folder(self):
        first = self.shell()
        first_folder = self.directory / "first"
        second_folder = self.directory / "second"
        first_folder.mkdir()
        second_folder.mkdir()
        self.cd(first, first_folder)
        second = self.shell(directory=str(second_folder))
        self.window.notebook.set_current_page(self.window.notebook.page_num(first))
        with patch.object(self.window, "resolve_codex", return_value="/test/codex"), \
                patch.object(TerminalSession, "start"):
            agent = self.window.new_agent()
        self.sessions.append(agent)
        self.assertEqual(agent.directory, str(first_folder))
        self.assertEqual(second.working_directory(), str(second_folder))

    def test_agent_tab_keeps_its_project_when_shell_navigates_elsewhere(self):
        shell = self.shell()
        first = self.directory / "agent-project"
        second = self.directory / "shell-project"
        first.mkdir()
        second.mkdir()
        self.cd(shell, first)
        with patch.object(self.window, "resolve_codex", return_value="/test/codex"), \
                patch.object(TerminalSession, "start"):
            agent = self.window.new_agent()
        self.sessions.append(agent)
        self.cd(shell, second)
        self.assertEqual(self.window.working_directory(), str(first))

    def test_login_tab_keeps_last_workspace_directory(self):
        shell = self.shell()
        project = self.directory / "workspace"
        project.mkdir()
        self.cd(shell, project)
        self.add(kind="login")
        with patch.object(self.window, "resolve_codex", return_value="/test/codex"), \
                patch.object(TerminalSession, "start"):
            agent = self.window.new_agent()
        self.sessions.append(agent)
        self.assertEqual(agent.directory, str(project))

    def test_folder_status_follows_cd(self):
        shell = self.shell()
        project = self.directory / "status-folder"
        project.mkdir()
        self.cd(shell, project)
        pump_until(lambda: self.window.folder_label.get_text() == str(project))

    def test_new_terminal_and_window_inherit_navigated_folder(self):
        shell = self.shell()
        project = self.directory / "next-terminal"
        project.mkdir()
        self.cd(shell, project)
        second = self.shell()
        self.assertEqual(second.working_directory(), str(project))
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]), \
                patch.dict(os.environ, {"HISTFILE": "/dev/null", "INPUTRC": "/dev/null"}):
            other = self.window.new_window()
        self.windows.append(other)
        self.sessions.extend(other.sessions)
        pump_until(lambda: other.current().state == "running")
        self.assertEqual(other.current().working_directory(), str(project))

    def test_deleted_shell_folder_does_not_launch_agent_in_wrong_folder(self):
        shell = self.shell()
        project = self.directory / "deleted"
        project.mkdir()
        self.cd(shell, project)
        project.rmdir()
        with patch.object(self.window, "resolve_codex", return_value="/test/codex"), \
                patch.object(self.window, "message") as message, patch.object(TerminalSession, "start") as start:
            self.assertIsNone(self.window.new_agent())
        message.assert_called_once()
        start.assert_not_called()

    def test_shell_selection_uses_environment_and_falls_back_to_account_shell(self):
        with patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            self.assertEqual(self.app.shell_command(), ["/bin/bash", "-i"])
        with patch.dict(os.environ, {"SHELL": "/no-such-swarm-shell"}), \
                patch("swarm_app.app.pwd.getpwuid") as user:
            user.return_value.pw_shell = "/bin/sh"
            self.assertEqual(self.app.shell_command(), ["/bin/sh", "-i"])

    def test_new_terminal_recovers_after_exit(self):
        shell = self.shell()
        project = self.directory / "last-folder"
        project.mkdir()
        self.cd(shell, project)
        self.window._poll_directory()
        shell.terminal.feed_child(b"exit\n")
        pump_until(lambda: shell.state == "exited")
        replacement = self.shell()
        self.assertEqual(replacement.working_directory(), str(project))

    def test_new_agent_uses_exact_yolo_command_and_folder(self):
        with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                patch.object(TerminalSession, "start") as start:
            session = self.window.new_agent()
        self.sessions.append(session)
        start.assert_called_once_with(["/test/codex", "--yolo", "-c", 'tui.terminal_title=["spinner","status"]'])
        self.assertEqual(session.directory, str(self.directory))
        self.assertEqual(session.kind, "agent")
        self.assertEqual(self.window.stack.get_visible_child_name(), "sessions")

    def shortcut_agent(self):
        folder = self.directory / str(len(self.sessions))
        folder.mkdir()
        argv = ["/usr/bin/python3", str(FIXTURE), str(folder), "record"]
        with patch.object(self.window, "_resolve_startup_command", return_value=(argv, "codex", {})) as command:
            activated = Gtk.accel_groups_activate(
                self.window, Gdk.KEY_t, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertTrue(activated)
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0].command, "codex --yolo")
        agent = self.window.current()
        self.sessions.append(agent)
        pump_until(lambda: agent.state == "running" and (folder / "ready").exists())
        return agent

    def test_ctrl_shift_t_launches_agent_with_managed_activity_title(self):
        agent = self.shortcut_agent()
        self.assertEqual(self.window.sessions, [agent])
        self.assertEqual(agent.kind, "agent")
        self.assertEqual(agent.origin_kind, "agent")
        self.assertTrue(agent.managed_activity_title)

    def test_broadcast_reaches_all_agents_only_in_current_window(self):
        first, folder1 = self.add()
        second, folder2 = self.add()
        login, login_folder = self.add(kind="login")
        shell, shell_folder = self.add(kind="shell")
        ended, ended_folder = self.add(mode="exit")
        other_window = self.app.new_window(start_terminal=False)
        self.windows.append(other_window)
        other, other_folder = self.add(other_window)
        self.assertEqual(set(self.window.active_agents()), {first, second})
        self.assertTrue(self.window.broadcast("Same message\n日本語"))
        expected = b"\x1b[200~Same message\r" + "日本語".encode() + b"\x1b[201~\r"
        pump_until(lambda: (folder1 / "input").read_bytes() == expected
                   and (folder2 / "input").read_bytes() == expected)
        self.assertEqual((login_folder / "input").read_bytes(), b"")
        self.assertEqual((shell_folder / "input").read_bytes(), b"")
        self.assertEqual((other_folder / "input").read_bytes(), b"")
        self.assertIn("sent to 2 agents", self.window.status_label.get_text())

    def test_dialog_targets_are_chosen_at_send_and_submits_without_confirmation(self):
        first, first_folder = self.add()
        self.window.open_broadcast()
        dialog = self.window.broadcast_dialog
        self.assertIsInstance(dialog, BroadcastDialog)
        self.assertFalse(dialog.send_button.get_sensitive())
        second, second_folder = self.add()
        dialog.editor.get_buffer().set_text("Broadcast now")
        self.assertTrue(dialog.send_button.get_sensitive())
        with patch.object(self.window, "confirm", side_effect=AssertionError("Unexpected confirmation")):
            dialog.response(Gtk.ResponseType.OK)
        self.assertIsNone(self.window.broadcast_dialog)
        pump_until(lambda: (first_folder / "input").read_bytes().endswith(b"\r")
                   and (second_folder / "input").read_bytes().endswith(b"\r"))

    def test_empty_invalid_and_no_agent_messages_are_rejected(self):
        self.assertFalse(self.window.broadcast("hello"))
        _session, folder = self.add()
        self.assertFalse(self.window.broadcast("  \n"))
        self.assertFalse(self.window.broadcast("bad\x1b[201~"))
        self.assertEqual((folder / "input").read_bytes(), b"")

    def notifying_broadcast(self):
        self.assertTrue(self.window.broadcast("Do the task", notify_when_done=True))
        watch = self.window.broadcast_watches[-1]
        pump_until(lambda: watch.submitted == set(watch.recipients))
        return watch

    def check_after_ready_delay(self, watch):
        self.assertIsNotNone(watch.ready_since)
        with patch("swarm_app.app.time.monotonic", return_value=watch.ready_since + 2):
            self.window._check_broadcast_notifications()

    def test_global_broadcast_notification_checkbox_is_opt_in_and_global_only(self):
        agent, _folder = self.add()
        self.activity_title(agent, "Ready")
        self.window.open_broadcast()
        dialog = self.window.broadcast_dialog
        self.assertTrue(dialog.notification_options.get_visible())
        self.assertTrue(dialog.notify_checkbox.get_sensitive())
        self.assertFalse(dialog.notify_checkbox.get_active())
        dialog.notify_checkbox.set_active(True)
        dialog.set_mode(idle_only=True)
        self.assertFalse(dialog.notification_options.get_visible())
        dialog.editor.get_buffer().set_text("Sleeper task")
        with patch.object(self.window, "broadcast", return_value=True) as broadcast:
            dialog.response(Gtk.ResponseType.OK)
        self.assertTrue(broadcast.call_args.kwargs.get("idle_only"))
        self.assertFalse(broadcast.call_args.kwargs.get("notify_when_done", False))

        self.window.open_broadcast()
        dialog = self.window.broadcast_dialog
        dialog.notify_checkbox.set_active(True)
        dialog.editor.get_buffer().set_text("Global task")
        with patch.object(self.window, "broadcast", return_value=True) as broadcast:
            dialog.response(Gtk.ResponseType.OK)
        broadcast.assert_called_once()
        self.assertEqual(broadcast.call_args.args, ("Global task",))
        self.assertTrue(broadcast.call_args.kwargs.get("notify_when_done"))
        self.assertFalse(broadcast.call_args.kwargs.get("idle_only", False))

        self.window.open_broadcast()
        dialog = self.window.broadcast_dialog
        manual, _folder = self.add()
        dialog._refresh()
        self.assertFalse(manual.managed_activity_title)
        self.assertTrue(dialog.notify_checkbox.get_sensitive())
        manual.origin_kind = "shell"
        with patch.object(self.window, "active_agents", return_value=[agent, manual]):
            dialog._refresh()
            self.assertTrue(dialog.notify_checkbox.get_sensitive())
            dialog.notify_checkbox.set_active(True)
        with patch.object(self.window, "active_agents", return_value=[]):
            dialog._refresh()
            self.assertFalse(dialog.notify_checkbox.get_sensitive())
            self.assertFalse(dialog.notify_checkbox.get_active())
        dialog.response(Gtk.ResponseType.CANCEL)

    def test_broadcast_without_notification_option_never_arms_alert(self):
        agent, folder = self.add()
        self.activity_title(agent, "Ready")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            self.assertTrue(self.window.broadcast("Regular broadcast"))
            pump_until(lambda: (folder / "input").read_bytes().endswith(b"\r"))
            self.activity_title(agent, "Working")
            self.activity_title(agent, "Ready")
            self.window._check_broadcast_notifications()
        self.assertEqual(self.window.broadcast_watches, [])
        notify.assert_not_called()

    def test_broadcast_notification_waits_for_all_recipients_and_fires_once(self):
        first, _folder = self.add()
        second, _folder = self.add()
        for session in (first, second):
            self.activity_title(session, "Ready")
        other_window = self.app.new_window(start_terminal=False)
        self.windows.append(other_window)
        other, _folder = self.add(other_window)
        self.activity_title(other, "Working")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            watch = self.notifying_broadcast()
            self.assertEqual(watch.recipients, frozenset((first, second)))
            unrelated, _folder = self.add()
            self.activity_title(unrelated, "Working")
            for session in (first, second):
                self.activity_title(session, "Working")
            self.activity_title(first, "Ready")
            self.assertIsNone(watch.ready_since)
            notify.assert_not_called()
            self.activity_title(second, "Ready")
            self.assertIsNotNone(watch.ready_since)
            notify.assert_not_called()
            self.check_after_ready_delay(watch)
            self.window._check_broadcast_notifications()
            notify.assert_called_once()
            self.assertEqual(notify.call_args.args, (2,))
            self.assertTrue(callable(notify.call_args.kwargs["on_error"]))
        self.assertEqual(self.window.broadcast_watches, [])
        self.assertTrue(unrelated.agent_busy)
        self.assertTrue(other.agent_busy)

    def test_broadcast_notification_tracks_mixed_manual_and_managed_agents(self):
        manual, folder = self.add(kind="shell")
        process = manual._process
        detected = DetectedCodex(process.pid, process.start, process.session, process.group, str(folder))
        with patch.object(manual, "_foreground_codex", return_value=detected):
            manual.refresh_agent(self.app.codex)
            self.assertTrue(manual.is_detected_agent)
            managed = self.shortcut_agent()
            self.activity_title(managed, "Ready")
            manual.terminal.feed(b"\x1b]0;My project\x07")
            pump_until(lambda: manual.terminal.get_window_title() == "My project")
            self.assertFalse(manual.managed_activity_title)
            with patch("swarm_app.app.notify_agents_finished") as notify:
                watch = self.notifying_broadcast()
                self.assertEqual(watch.recipients, frozenset((manual, managed)))
                self.activity_title(managed, "Working")
                self.activity_title(managed, "Ready")
                self.assertEqual(watch.started, {managed})
                self.assertIsNone(watch.ready_since)
                notify.assert_not_called()
                for title in ("⠋ My project", "[ ! ] Action Required", "Starting", "My project"):
                    manual.terminal.feed(f"\x1b]0;{title}\x07".encode())
                    pump_until(lambda: manual.terminal.get_window_title() == title)
                    manual.refresh_activity()
                    self.window._check_broadcast_notifications()
                    if title != "My project":
                        self.assertIsNone(watch.ready_since)
                        notify.assert_not_called()
                self.assertEqual(watch.started, {manual, managed})
                self.assertEqual(watch.busy_titles[manual], "⠋ My project")
                self.check_after_ready_delay(watch)
                notify.assert_called_once()
                self.assertEqual(notify.call_args.args, (2,))
        self.assertEqual(self.window.broadcast_watches, [])
        self.assertFalse(manual.managed_activity_title)

    def test_broadcast_notification_requires_work_and_stable_ready(self):
        agent, _folder = self.add()
        self.activity_title(agent, "Ready")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            watch = self.notifying_broadcast()
            self.window._check_broadcast_notifications()
            self.assertEqual(watch.started, set())
            self.assertIsNone(watch.ready_since)
            self.activity_title(agent, "Working")
            self.assertEqual(watch.started, {agent})
            for title in ("[ ! ] Action Required", "Unknown status"):
                self.activity_title(agent, title)
                self.window._check_broadcast_notifications()
                self.assertIsNone(watch.ready_since)
            self.activity_title(agent, "Ready")
            self.assertIsNotNone(watch.ready_since)
            with patch("swarm_app.app.time.monotonic", return_value=watch.ready_since + 0.5):
                self.window._check_broadcast_notifications()
            notify.assert_not_called()
            self.activity_title(agent, "Working")
            self.assertIsNone(watch.ready_since)
            self.activity_title(agent, "Ready")
            self.check_after_ready_delay(watch)
            notify.assert_called_once()

    def test_broadcast_notification_is_cancelled_when_recipient_closes_or_exits(self):
        with patch("swarm_app.app.notify_agents_finished") as notify:
            for action in ("close", "exit"):
                with self.subTest(action=action):
                    agent, _folder = self.add()
                    self.activity_title(agent, "Ready")
                    watch = self.notifying_broadcast()
                    self.activity_title(agent, "Working")
                    self.activity_title(agent, "Ready")
                    if action == "close":
                        self.window.close_session(agent, confirm=False)
                    else:
                        os.kill(agent.pid, signal.SIGTERM)
                        pump_until(lambda: agent.state == "exited")
                    self.assertEqual(self.window.broadcast_watches, [])
                    self.check_after_ready_delay(watch)
            notify.assert_not_called()

    def test_broadcast_notification_is_cancelled_if_recipient_process_is_replaced(self):
        agent, _folder = self.add()
        self.activity_title(agent, "Ready")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            watch = self.notifying_broadcast()
            self.assertEqual(watch.identities[agent], agent.agent_identity)
            self.activity_title(agent, "Working")
            self.activity_title(agent, "Ready")
            with patch.object(TerminalSession, "agent_identity", new_callable=PropertyMock,
                              return_value=("replacement",)):
                self.window._check_broadcast_notifications()
            self.assertEqual(self.window.broadcast_watches, [])
            self.check_after_ready_delay(watch)
            notify.assert_not_called()

    def test_broadcast_notification_is_cancelled_if_any_delivery_fails(self):
        first, _folder = self.add()
        failed, _folder = self.add()
        for session in (first, failed):
            self.activity_title(session, "Ready")
        completions = []

        def delayed_delivery(_message, completed, **_kwargs):
            completions.append(completed)
            return True

        with patch.object(first, "broadcast", side_effect=delayed_delivery), \
                patch.object(failed, "broadcast", side_effect=delayed_delivery), \
                patch("swarm_app.app.notify_agents_finished") as notify:
            self.assertTrue(self.window.broadcast("Task", notify_when_done=True))
            self.assertEqual(len(self.window.broadcast_watches), 1)
            completions[1](False)
            self.assertEqual(self.window.broadcast_watches, [])
            completions[0](True)
            for session in (first, failed):
                self.activity_title(session, "Working")
                self.activity_title(session, "Ready")
            self.window._check_broadcast_notifications()
            self.assertEqual(self.window.broadcast_watches, [])
            notify.assert_not_called()

    def test_new_overlapping_broadcast_supersedes_previous_notification(self):
        agent, _folder = self.add()
        self.activity_title(agent, "Ready")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            previous = self.notifying_broadcast()
            latest = self.notifying_broadcast()
            self.assertTrue(all(watch is not previous for watch in self.window.broadcast_watches))
            self.assertEqual(self.window.broadcast_watches, [latest])
            self.activity_title(agent, "Working")
            self.activity_title(agent, "Ready")
            self.check_after_ready_delay(latest)
            notify.assert_called_once()

    def test_closing_last_tab_restores_blank_workspace(self):
        session, _folder = self.add()
        self.window.close_session(session, confirm=False)
        self.assertEqual(self.window.sessions, [])
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertFalse(self.window.global_item.get_sensitive())
        self.assertEqual(session.state, "closed")

    def activity_title(self, session, title):
        session.managed_activity_title = True
        session.terminal.feed(f"\x1b]0;{title}\x07".encode())
        pump_until(lambda: session.terminal.get_window_title() == title)
        session.refresh_activity()

    def tab_order(self):
        return [self.window.notebook.get_nth_page(index)
                for index in range(self.window.notebook.get_n_pages())]

    def test_ready_tab_waits_and_cancels_a_false_finish(self):
        first, _ = self.add()
        second, _ = self.add()
        with patch("swarm_app.app.IDLE_TAB_REORDER_DELAY_MS", 180):
            self.activity_title(second, "Ready")
            self.assertEqual(self.tab_order(), [first, second])
            self.assertIn(second, self.window.idle_reorder_sources)
            self.activity_title(second, "Working")
            self.assertNotIn(second, self.window.idle_reorder_sources)
            started = time.monotonic()
            pump_until(lambda: time.monotonic() - started > .22)
            self.assertEqual(self.tab_order(), [first, second])
            self.assertNotIn(second, self.window.idle_agents)
            self.activity_title(second, "Ready")
            self.assertEqual(self.tab_order(), [first, second])
            pump_until(lambda: self.tab_order() == [second, first])

    def test_ready_tabs_stay_grouped_and_keep_the_selected_page_and_focus(self):
        first, _ = self.add()
        second, _ = self.add()
        third, _ = self.add()
        self.window.notebook.set_current_page(self.window.notebook.page_num(third))
        third.terminal.grab_focus()
        with patch("swarm_app.app.IDLE_TAB_REORDER_DELAY_MS", 80):
            for session in (first, second):
                self.activity_title(session, "Ready")
                pump_until(lambda: session in self.window.idle_agents)
        self.assertEqual(self.tab_order(), [second, first, third])
        self.assertIs(self.window.current(), third)
        self.assertIs(self.window.get_focus(), third.terminal)
        # A drag into the Ready group cannot strand a working tab between it.
        self.window.notebook.reorder_child(third, 1)
        self.assertEqual(self.tab_order(), [second, first, third])
        self.activity_title(second, "Working")
        self.assertEqual(self.tab_order(), [first, second, third])
        self.assertIs(self.window.current(), third)
        self.assertIs(self.window.get_focus(), third.terminal)

    def test_closing_tab_cancels_pending_ready_reorder(self):
        session, _ = self.add()
        self.activity_title(session, "Ready")
        self.assertIn(session, self.window.idle_reorder_sources)
        self.window.close_session(session, confirm=False)
        self.assertNotIn(session, self.window.idle_reorder_sources)
        self.assertNotIn(session, self.window.idle_agents)

    def test_context_copy_uses_clicked_terminal_and_stays_available(self):
        first, _ = self.add()
        first.terminal.feed(b"Text selected in the first terminal\x1b]0;Selection ready\x07")
        pump_until(lambda: first.terminal.get_window_title() == "Selection ready")
        first.terminal.select_all()
        second, _ = self.add()
        menus = []
        with patch.object(Gtk.Menu, "popup_at_pointer", lambda menu, _event: menus.append(menu)):
            self.assertTrue(self.window._terminal_menu(first.terminal, SimpleNamespace(button=3)))
        menu = menus[0]
        try:
            copy = menu.get_children()[0]
            self.assertTrue(copy.get_sensitive())
            self.assertIs(self.window.current(), second)
            copy.activate()
            self.assertIn("Text selected in the first terminal",
                          Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text())
        finally:
            menu.destroy()

    def native_input(self, *args):
        if (shutil.which("xdotool") is None
                or Gdk.Display.get_default().__gtype__.name != "GdkX11Display"):
            self.skipTest("Native pointer/keyboard regression requires X11 and xdotool")
        subprocess.run(["xdotool", *args], check=True, timeout=5)

    def click_widget(self, widget):
        x, y = widget.translate_coordinates(self.window, 0, 0)
        _ok, origin_x, origin_y = self.window.get_window().get_origin()
        allocation = widget.get_allocation()
        self.native_input("mousemove", str(origin_x + x + allocation.width // 2),
                          str(origin_y + y + allocation.height // 2), "click", "1")

    def test_click_tab_then_arrows_navigates_until_terminal_is_focused(self):
        first, _ = self.add()
        second, second_folder = self.add()
        third, _ = self.add()
        self.click_widget(self.window.tab_labels[second])
        pump_until(lambda: self.window.current() is second and self.window.notebook.is_focus())
        self.native_input("key", "Left")
        pump_until(lambda: self.window.current() is first)
        self.native_input("key", "Left")
        pump_until(lambda: self.window.current() is third)
        self.native_input("key", "Right", "Right")
        pump_until(lambda: self.window.current() is second)
        with patch("swarm_app.app.IDLE_TAB_REORDER_DELAY_MS", 80):
            self.activity_title(third, "Ready")
            pump_until(lambda: self.tab_order() == [third, first, second])
        self.assertIs(self.window.current(), second)
        self.assertTrue(self.window.notebook.is_focus())
        self.native_input("key", "Right")
        pump_until(lambda: self.window.current() is third)
        self.native_input("key", "Left")
        pump_until(lambda: self.window.current() is second)
        self.native_input("key", "Return")
        pump_until(lambda: self.window.get_focus() is second.terminal)
        self.native_input("key", "Left", "Right")
        pump_until(lambda: (second_folder / "input").read_bytes().endswith(b"\x1b[D\x1b[C"))
        self.assertIs(self.window.current(), second)
        # Clicking the selected tab again must restore strip navigation too.
        self.click_widget(self.window.tab_labels[second])
        pump_until(self.window.notebook.is_focus)
        self.click_widget(second.terminal)
        pump_until(lambda: self.window.get_focus() is second.terminal)
        before = (second_folder / "input").stat().st_size
        self.native_input("key", "Left")
        pump_until(lambda: (second_folder / "input").stat().st_size > before)
        self.assertIs(self.window.current(), second)

    def test_click_tab_then_typing_keeps_the_first_key_and_backspace(self):
        first, folder = self.add()
        self.add()
        self.click_widget(self.window.tab_labels[first])
        pump_until(self.window.notebook.is_focus)
        self.native_input("key", "a")
        pump_until(lambda: (folder / "input").read_bytes() == b"a")
        self.assertIs(self.window.get_focus(), first.terminal)
        self.click_widget(self.window.tab_labels[first])
        pump_until(self.window.notebook.is_focus)
        self.native_input("key", "BackSpace")
        pump_until(lambda: (folder / "input").read_bytes() == b"a\x7f")
        self.assertIs(self.window.get_focus(), first.terminal)
        self.click_widget(self.window.tab_labels[first])
        pump_until(self.window.notebook.is_focus)
        self.native_input("key", "shift+a")
        pump_until(lambda: (folder / "input").read_bytes() == b"a\x7fA")
        self.assertIs(self.window.get_focus(), first.terminal)

    def test_interrupt_all_agents_menu_order_and_sensitivity(self):
        item = self.window.interrupt_all_agents_item
        menu_items = item.get_parent().get_children()
        self.assertIs(menu_items[menu_items.index(item) + 1], self.window.kill_all_agents_item)
        self.assertEqual(item.get_label().replace("_", ""), "Interrupt All Agents")
        self.assertFalse(item.get_sensitive())
        self.add(kind="shell")
        self.add(kind="login")
        self.add(mode="exit")
        with patch.object(TerminalSession, "start"):
            starting = self.window.add_session("Starting", "agent", [], str(self.directory))
        self.sessions.append(starting)
        self.assertFalse(item.get_sensitive())
        agent, _folder = self.add()
        self.assertTrue(item.get_sensitive())
        os.kill(agent.pid, signal.SIGTERM)
        pump_until(lambda: agent.state == "exited")
        self.assertFalse(item.get_sensitive())
        self.assertTrue(self.window.kill_all_agents_item.get_sensitive())
        with patch.object(self.window, "confirm") as confirm:
            self.window.interrupt_all_agents()
        confirm.assert_not_called()

    def test_interrupt_all_agents_sends_escape_only_in_current_window_and_keeps_tabs(self):
        busy, busy_folder = self.add()
        idle, idle_folder = self.add()
        self.activity_title(busy, "Working")
        self.activity_title(idle, "Ready")
        shell, shell_folder = self.add(kind="shell")
        login, login_folder = self.add(kind="login")
        exited, _folder = self.add(mode="exit")
        manual, manual_folder = self.add(kind="shell")
        process = manual._process
        detected = DetectedCodex(process.pid, process.start, process.session, process.group, str(manual_folder))
        other_window = self.app.new_window(start_terminal=False)
        self.windows.append(other_window)
        other, other_folder = self.add(other_window)
        original_tabs = self.window.sessions[:]
        self.window.notebook.set_current_page(self.window.notebook.page_num(shell))
        with patch.object(manual, "_foreground_codex", return_value=detected):
            with patch.object(self.window, "confirm") as confirm:
                self.window.interrupt_all_agents_item.activate()
            confirm.assert_not_called()
            pump_until(lambda: all((folder / "input").read_bytes() == b"\x1b"
                                  for folder in (busy_folder, idle_folder, manual_folder)))
            self.assertTrue(manual.is_detected_agent)
        for folder in (shell_folder, login_folder, other_folder):
            self.assertEqual((folder / "input").read_bytes(), b"")
        self.assertEqual(self.window.sessions, original_tabs)
        self.assertEqual(self.window.notebook.get_n_pages(), len(original_tabs))
        self.assertIs(self.window.current(), shell)
        self.assertEqual(other_window.sessions, [other])
        for session in (busy, idle, manual, shell, login, other):
            self.assertEqual(session.state, "running")
            self.assertTrue(process_alive(session.pid))
        self.assertEqual(exited.state, "exited")
        self.assertTrue(self.window.interrupt_all_agents_item.get_sensitive())

    def test_interrupt_all_agents_cancels_completion_notification(self):
        agent, folder = self.add()
        self.activity_title(agent, "Ready")
        with patch("swarm_app.app.notify_agents_finished") as notify:
            watch = self.notifying_broadcast()
            self.activity_title(agent, "Working")
            self.assertEqual(watch.started, {agent})
            self.window.interrupt_all_agents()
            pump_until(lambda: (folder / "input").read_bytes().endswith(b"\x1b"))
            self.activity_title(agent, "Ready")
            self.window._check_broadcast_notifications()
            with patch("swarm_app.app.time.monotonic", return_value=time.monotonic() + 2):
                self.window._check_broadcast_notifications()
            notify.assert_not_called()
        self.assertEqual(self.window.broadcast_watches, [])

    def test_kill_all_agents_is_disabled_without_agent_tabs(self):
        self.assertFalse(self.window.kill_all_agents_item.get_sensitive())
        shell, _folder = self.add(kind="shell")
        login, _folder = self.add(kind="login")
        self.assertFalse(self.window.kill_all_agents_item.get_sensitive())
        with patch.object(self.window, "confirm") as confirm:
            self.window.kill_all_agents()
        confirm.assert_not_called()
        self.assertEqual(self.window.sessions, [shell, login])
        self.assertEqual([session.state for session in self.window.sessions], ["running", "running"])

    def test_kill_all_agents_menu_closes_idle_agents_without_warning(self):
        idle, _folder = self.add()
        unknown, _folder = self.add()
        self.activity_title(idle, "Ready")
        self.assertTrue(idle.agent_idle)
        self.assertIsNone(unknown.activity)
        self.assertTrue(self.window.kill_all_agents_item.get_sensitive())
        with patch.object(self.window, "confirm") as confirm:
            self.window.kill_all_agents_item.activate()
        confirm.assert_not_called()
        self.assertEqual([idle.state, unknown.state], ["closed", "closed"])
        self.assertEqual(self.window.sessions, [])
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertFalse(self.window.kill_all_agents_item.get_sensitive())

    def test_kill_all_agents_warns_once_and_honors_cancel_or_accept(self):
        first, _folder = self.add()
        second, _folder = self.add()
        idle, _folder = self.add()
        for session in (first, second):
            self.activity_title(session, "Working")
        self.activity_title(idle, "Ready")
        # The action must refresh activity before deciding whether to warn.
        first.activity = None
        second.activity = None
        with patch.object(self.window, "confirm", return_value=False) as confirm:
            self.window.kill_all_agents()
        confirm.assert_called_once()
        title, detail, button = confirm.call_args.args
        self.assertEqual(title, "Kill all agents?")
        self.assertIn("2 agents are still running", detail)
        self.assertIn("3 agent tabs in this window", detail)
        self.assertEqual(button, "Kill All Agents")
        self.assertEqual(self.window.sessions, [first, second, idle])
        self.assertTrue(all(session.state == "running" for session in self.window.sessions))
        with patch.object(self.window, "confirm", return_value=True) as confirm:
            self.window.kill_all_agents()
        confirm.assert_called_once()
        self.assertEqual(self.window.sessions, [])
        self.assertTrue(all(session.state == "closed" for session in (first, second, idle)))

    def test_kill_all_agents_includes_nonrunning_tabs_and_preserves_other_sessions(self):
        exited, _folder = self.add(mode="exit")
        self.assertTrue(self.window.kill_all_agents_item.get_sensitive())
        with patch.object(TerminalSession, "start"):
            starting = self.window.add_session("Starting", "agent", [], str(self.directory))
            failed = self.window.add_session("Failed", "agent", [], str(self.directory))
        self.sessions.extend([starting, failed])
        failed.state = "failed"
        self.window._session_changed(failed)
        shell, _folder = self.add(kind="shell")
        login, _folder = self.add(kind="login")
        other_window = self.app.new_window(start_terminal=False)
        self.windows.append(other_window)
        other, _folder = self.add(other_window)
        self.activity_title(other, "Working")
        with patch.object(self.window, "confirm") as confirm:
            self.window.kill_all_agents()
        confirm.assert_not_called()
        self.assertTrue(all(session.state == "closed" for session in (exited, starting, failed)))
        self.assertEqual(self.window.sessions, [shell, login])
        self.assertFalse(self.window.kill_all_agents_item.get_sensitive())
        self.assertEqual(other_window.sessions, [other])
        self.assertTrue(other.agent_busy)
        self.assertTrue(other_window.kill_all_agents_item.get_sensitive())

    def test_kill_all_agents_rechecks_kind_and_preserves_tabs_added_during_warning(self):
        busy, _folder = self.add()
        demoted, _folder = self.add()
        self.activity_title(busy, "Working")
        added = []

        def refresh_kind(_codex):
            if added:
                demoted.kind = "shell"

        def accept_after_new_tab(*_args):
            added.append(self.add()[0])
            return True

        with patch.object(demoted, "refresh_agent", side_effect=refresh_kind), \
                patch.object(self.window, "confirm", side_effect=accept_after_new_tab) as confirm:
            self.window.kill_all_agents()
        confirm.assert_called_once()
        self.assertEqual(busy.state, "closed")
        self.assertEqual(self.window.sessions, [demoted, added[0]])
        self.assertEqual(demoted.kind, "shell")
        self.assertEqual([session.state for session in self.window.sessions], ["running", "running"])

    def test_ended_agent_tab_shows_killed_skull_in_red(self):
        agent, _folder = self.add(mode="exit")
        title = self.window.tab_labels[agent]
        self.assertEqual(title.get_text(), agent.title + " · killed 💀")
        self.assertTrue(title.get_style_context().has_class("killed-tab"))
        self.assertEqual(agent.state, "exited")
        self.assertEqual(self.window.active_agents(), [])

    def test_ended_shell_tab_keeps_normal_exit_label(self):
        shell, _folder = self.add(kind="shell", mode="exit")
        title = self.window.tab_labels[shell]
        self.assertEqual(title.get_text(), shell.title + " · exited")
        self.assertFalse(title.get_style_context().has_class("killed-tab"))

    def test_restarted_agent_has_normal_tab_color(self):
        agent, _folder = self.add(mode="exit")
        with patch.object(self.window, "resolve_codex", return_value="/test/codex"), \
                patch.object(TerminalSession, "start") as start:
            self.window.restart_current()
        replacement = self.window.current()
        start.assert_called_once_with(["/test/codex", "--yolo", "-c", 'tui.terminal_title=["spinner","status"]'])
        self.assertIsNot(replacement, agent)
        self.assertNotIn("killed", self.window.tab_labels[replacement].get_text())
        self.assertFalse(self.window.tab_labels[replacement].get_style_context().has_class("killed-tab"))

    def test_missing_codex_reports_error_without_creating_tab(self):
        with patch.object(self.window, "message") as message:
            self.window.new_agent()
        message.assert_called_once()
        self.assertEqual(self.window.sessions, [])

    def test_codex_path_is_absolute_before_working_directory_changes(self):
        with patch("swarm_app.app.shutil.which", return_value="./codex"):
            self.assertEqual(self.window.resolve_codex(), str(Path("./codex").resolve()))

    def test_login_refresh_is_repeated_after_pending_check(self):
        self.app.auth_pending = True
        self.app.refresh_auth()
        self.assertTrue(self.app.auth_refresh_requested)
        with patch.object(self.app, "refresh_auth") as refresh:
            self.app._auth_finished("Sign in from Session")
        refresh.assert_called_once()
        self.assertFalse(self.app.auth_refresh_requested)

    def test_login_is_direct_cli_flow_shared_across_windows(self):
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                patch.object(TerminalSession, "start") as start:
            self.window.login(device=True)
            other.login()
        start.assert_called_once_with(["/test/codex", "login", "--device-auth"])
        self.assertEqual(len(self.window.sessions), 1)
        self.assertEqual(self.window.sessions[0].kind, "login")
        self.assertEqual(other.sessions, [])
        self.assertEqual(self.window.active_agents(), [])

    def test_account_menu_tracks_sign_in_and_out_in_every_window(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        for window in self.windows:
            self.assertEqual(window.account_item.get_label(), "Log out of _ChatGPT")
            self.assertFalse(window.device_login_item.get_visible())
            self.assertTrue(window.account_item.get_sensitive())
        self.app._set_auth_status("Signed out", "signed_out")
        for window in self.windows:
            self.assertEqual(window.account_item.get_label(), "Sign in with _ChatGPT")
            self.assertTrue(window.device_login_item.get_visible())
            self.assertEqual(window.account_label.get_text(), "Signed out")

    def test_other_auth_uses_codex_logout_label(self):
        self.app._set_auth_status("Signed in to Codex", "other")
        self.assertEqual(self.window.account_item.get_label(), "Log out of _Codex")
        self.assertFalse(self.window.device_login_item.get_visible())

    def test_account_action_uses_current_state(self):
        with patch.object(self.window, "login") as login, patch.object(self.window, "logout") as logout:
            self.app._set_auth_status("Signed out", "signed_out")
            self.window.account_item.activate()
            login.assert_called_once_with()
            logout.assert_not_called()
            self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
            self.window.account_item.activate()
            logout.assert_called_once_with()

    def test_cancelling_logout_confirmation_keeps_login(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
        with patch.object(self.window, "confirm", return_value=False), patch.object(self.app, "logout") as logout:
            self.window.logout()
        logout.assert_not_called()
        self.assertEqual(self.app.auth_state, "chatgpt")

    def test_logout_rechecks_operations_after_confirmation(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")

        def begin_check(*_args):
            self.app.auth_pending = True
            return True

        with patch.object(self.window, "confirm", side_effect=begin_check), patch.object(self.app, "logout") as logout:
            self.window.logout()
        logout.assert_not_called()
        self.app.auth_pending = False

    def test_logout_runs_cli_then_restores_login_options(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")

        def cli(argv, **_kwargs):
            if argv == ["/test/codex", "logout"]:
                return subprocess.CompletedProcess(argv, 0, "", "Successfully logged out")
            self.assertEqual(argv, ["/test/codex", "login", "status"])
            return subprocess.CompletedProcess(argv, 1, "", "Not logged in")

        with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                patch("swarm_app.app.subprocess.run", side_effect=cli) as run, \
                patch.object(self.window, "confirm", return_value=True):
            self.window.logout()
            self.assertTrue(self.app.logout_pending)
            self.assertFalse(self.window.account_item.get_sensitive())
            pump_until(lambda: not self.app.logout_pending and not self.app.auth_pending)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["/test/codex", "logout"], ["/test/codex", "login", "status"]])
        self.assertEqual(self.app.auth_state, "signed_out")
        self.assertEqual(self.window.account_item.get_label(), "Sign in with _ChatGPT")
        self.assertTrue(self.window.device_login_item.get_visible())

    def test_failed_or_timed_out_logout_does_not_claim_signed_out(self):
        for failure in ("exit", "timeout"):
            with self.subTest(failure=failure):
                self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")

                def cli(argv, **_kwargs):
                    if argv[-1] == "logout":
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(argv, 12)
                        return subprocess.CompletedProcess(argv, 1, "", "Logout failed")
                    return subprocess.CompletedProcess(argv, 0, "", "Logged in using ChatGPT")

                with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                        patch("swarm_app.app.subprocess.run", side_effect=cli):
                    self.assertTrue(self.app.logout("/test/codex"))
                    pump_until(lambda: not self.app.logout_pending and not self.app.auth_pending)
                self.assertEqual(self.app.auth_state, "chatgpt")
                self.assertEqual(self.window.account_item.get_label(), "Log out of _ChatGPT")
                self.assertIn("Could not sign out", self.window.status_label.get_text())

    def test_login_flow_and_pending_check_block_logout(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
        self.app.auth_pending = True
        with patch("swarm_app.app.subprocess.run") as run:
            self.assertFalse(self.app.logout("/test/codex"))
            run.assert_not_called()
        self.app.auth_pending = False
        with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                patch.object(TerminalSession, "start"):
            self.window.login()
        self.assertTrue(self.app.has_login_flow())
        self.assertFalse(self.window.account_item.get_sensitive())
        with patch("swarm_app.app.subprocess.run") as run:
            self.assertFalse(self.app.logout("/test/codex"))
            run.assert_not_called()

    def test_refresh_waits_for_logout_and_duplicate_logout_is_blocked(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
        with patch("swarm_app.app.threading.Thread") as worker:
            self.assertTrue(self.app.logout("/test/codex"))
            self.assertFalse(self.app.logout("/test/codex"))
            self.app.refresh_auth()
            worker.assert_called_once()
        self.assertTrue(self.app.auth_refresh_requested)
        with patch.object(self.app, "refresh_auth") as refresh:
            self.app._logout_finished(True)
        refresh.assert_called_once_with()
        self.assertFalse(self.app.logout_pending)

    def test_logout_completion_after_origin_window_closes(self):
        self.app._set_auth_status("Signed in with ChatGPT", "chatgpt")
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        with patch("swarm_app.app.threading.Thread"):
            self.assertTrue(self.app.logout("/test/codex"))
        self.window.destroy()
        with patch.object(self.app, "refresh_auth"):
            self.app._logout_finished(True)
        self.assertEqual(other.account_item.get_label(), "Sign in with _ChatGPT")
        self.assertTrue(other.device_login_item.get_visible())

    def test_closing_login_window_refreshes_remaining_window(self):
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        with patch("swarm_app.app.shutil.which", return_value="/test/codex"), \
                patch.object(TerminalSession, "start"):
            self.window.login()
        with patch.object(self.app, "refresh_auth") as refresh:
            self.window.destroy()
        refresh.assert_called_once_with()
        self.assertFalse(self.app.has_login_flow())

    def test_new_and_restarted_agents_wait_until_logout_finishes(self):
        self.app.logout_pending = True
        with patch.object(self.window, "resolve_codex") as resolve:
            self.window.new_agent()
            self.window.restart_current()
        resolve.assert_not_called()
        self.assertEqual(self.window.sessions, [])
        self.app.logout_pending = False


if __name__ == "__main__":
    unittest.main()
