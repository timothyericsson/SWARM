"""Window and broadcast integration checks using local fake terminal processes."""

import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import shlex
from unittest.mock import patch

from swarm_app.app import BroadcastDialog, Gtk, SwarmApplication
from swarm_app.session import TerminalSession
from test_session import FIXTURE, process_alive, pump_until


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = SwarmApplication("/tmp", "/no-such-swarm-codex")
        cls.app.register(None)
        cls.app.hold()

    @classmethod
    def tearDownClass(cls):
        cls.app.release()
        cls.app.quit()

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

    def test_closing_last_tab_restores_blank_workspace(self):
        session, _folder = self.add()
        self.window.close_session(session, confirm=False)
        self.assertEqual(self.window.sessions, [])
        self.assertEqual(self.window.stack.get_visible_child_name(), "empty")
        self.assertFalse(self.window.global_item.get_sensitive())
        self.assertEqual(session.state, "closed")

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
