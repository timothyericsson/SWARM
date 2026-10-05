"""Linked harness preferences and multi-agent launch integration checks."""

import json
import os
from pathlib import Path
import shlex
import signal
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.activity import codex_agent_command
from swarm_app.app import Gtk, SwarmApplication
from swarm_app.linked_agents import LinkedAgentsSettings
from swarm_app.session import TerminalSession
from gtk_test_support import shutdown_application
from test_session import FIXTURE, process_alive, pump_until


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class OpenSwarmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.config_temp.cleanup)
        cls.settings_path = Path(cls.config_temp.name) / "linked-agents.json"
        cls.app = SwarmApplication("/tmp", "/test/swarm-codex", "/test/swarm-hermes",
                                   linked_agents_path=cls.settings_path)
        cls.app.set_application_id("io.swarm.Terminal.OpenSwarmTests")
        if not cls.app.register(None):
            raise AssertionError("The test application could not register")
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
        self.app.set_linked_agent_enabled("codex", True)
        self.app.set_linked_agent_enabled("hermes", True)
        self.window = self.app.new_window(start_terminal=False)
        self.windows = [self.window]

    def tearDown(self):
        sessions = [session for window in self.windows for session in window.sessions]
        for window in self.windows:
            window.destroy()
        pump_until(lambda: all(s.pid is None or not process_alive(s.pid) for s in sessions))
        self.temp.cleanup()

    @staticmethod
    def executable(command):
        return {"/test/swarm-codex": "/test/swarm-codex",
                "/test/swarm-hermes": "/test/swarm-hermes"}.get(command)

    def startup_window(self):
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]), \
                patch.dict(os.environ, {"HISTFILE": "/dev/null", "INPUTRC": "/dev/null"}):
            window = self.app.new_window()
        self.windows.append(window)
        shell = window.current()
        pump_until(lambda: shell.state == "running" and shell.working_directory() == str(self.directory))
        return window, shell

    def harness_executables(self):
        executables = {}
        for harness in ("codex", "hermes"):
            executable = self.directory / harness
            executable.write_text(
                "#!/usr/bin/python3\nimport json, sys, time\nfrom pathlib import Path\n"
                "Path(__file__).with_suffix('.launch').write_text("
                "json.dumps({'cwd': str(Path.cwd()), 'argv': sys.argv[1:]}))\n"
                "time.sleep(60)\n")
            executable.chmod(0o755)
            executables[harness] = executable
        return executables

    def test_open_swarm_creates_one_agent_for_each_enabled_harness(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start", autospec=True) as start:
            sessions = self.window.open_swarm()
        self.assertEqual(self.window.sessions, sessions)
        self.assertEqual([session.harness for session in sessions], ["codex", "hermes"])
        self.assertEqual([session.kind for session in sessions], ["agent", "agent"])
        self.assertRegex(sessions[0].title, r"^Codex \d+$")
        self.assertRegex(sessions[1].title, r"^Hermes \d+$")
        self.assertTrue(sessions[0].managed_activity_title)
        self.assertFalse(sessions[1].managed_activity_title)
        self.assertEqual(start.call_args_list[0].args,
                         (sessions[0], codex_agent_command("/test/swarm-codex")))
        self.assertEqual(start.call_args_list[1].args,
                         (sessions[1], ["/test/swarm-hermes"]))
        self.assertEqual(self.window.notebook.get_n_pages(), 2)

    def test_disabling_hermes_launches_only_codex(self):
        self.assertTrue(self.app.set_linked_agent_enabled("hermes", False))
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            sessions = self.window.open_swarm()
        self.assertEqual([session.harness for session in sessions], ["codex"])
        resolve.assert_called_once_with(self.app.codex)
        start.assert_called_once_with(codex_agent_command("/test/swarm-codex"))

    def test_disabling_codex_launches_only_hermes(self):
        self.assertTrue(self.app.set_linked_agent_enabled("codex", False))
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            sessions = self.window.open_swarm()
        self.assertEqual([session.harness for session in sessions], ["hermes"])
        resolve.assert_called_once_with(self.app.hermes)
        start.assert_called_once_with(["/test/swarm-hermes"])

    def test_disabling_both_explains_empty_selection_and_opens_preferences(self):
        self.app.set_linked_agent_enabled("codex", False)
        self.app.set_linked_agent_enabled("hermes", False)
        with patch("swarm_app.app.shutil.which") as resolve, \
                patch.object(TerminalSession, "start") as start, \
                patch.object(self.window, "message") as message, \
                patch.object(self.window, "open_linked_agents") as preferences:
            self.assertEqual(self.window.open_swarm(), [])
        self.assertEqual(self.window.sessions, [])
        resolve.assert_not_called()
        start.assert_not_called()
        message.assert_called_once()
        preferences.assert_called_once()

    def test_missing_enabled_harness_never_opens_a_partial_swarm(self):
        for missing in (self.app.codex, self.app.hermes):
            with self.subTest(missing=missing), \
                    patch("swarm_app.app.shutil.which",
                          side_effect=lambda command: None if command == missing else self.executable(command)), \
                    patch.object(TerminalSession, "start") as start, \
                    patch.object(self.window, "message") as message:
                self.assertEqual(self.window.open_swarm(), [])
            start.assert_not_called()
            message.assert_called_once()
            self.assertIn("Codex" if missing == self.app.codex else "Hermes",
                          " ".join(message.call_args.args))
            self.assertEqual(self.window.sessions, [])

    def test_unavailable_working_folder_does_not_launch_any_harness(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(self.window, "working_directory", return_value=None), \
                patch.object(TerminalSession, "start") as start, \
                patch.object(self.window, "message") as message:
            self.assertEqual(self.window.open_swarm(), [])
        start.assert_not_called()
        message.assert_called_once()
        self.assertEqual(self.window.sessions, [])

    def test_open_swarm_repeatedly_adds_tabs_with_distinct_names(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            first = self.window.open_swarm()
            second = self.window.open_swarm()
        self.assertEqual(len(self.window.sessions), 4)
        self.assertEqual(len({session.title for session in first + second}), 4)
        self.assertEqual([session.harness for session in second], ["codex", "hermes"])

    def test_both_harnesses_really_spawn_in_selected_shells_navigated_folder(self):
        project = self.directory / "project with spaces 日本語"
        project.mkdir()
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]), \
                patch.dict(os.environ, {"HISTFILE": "/dev/null", "INPUTRC": "/dev/null"}):
            shell = self.window.new_terminal()
        pump_until(lambda: shell.state == "running")
        shell.terminal.feed_child(("cd -- " + shlex.quote(str(project)) + "\n").encode())
        pump_until(lambda: shell.working_directory() == str(project))
        executables = {}
        for harness in ("codex", "hermes"):
            executable = self.directory / harness
            executable.write_text(
                "#!/usr/bin/python3\nimport json, sys\nfrom pathlib import Path\n"
                "Path(__file__).with_suffix('.launch').write_text("
                "json.dumps({'cwd': str(Path.cwd()), 'argv': sys.argv[1:]}))\n")
            executable.chmod(0o755)
            executables[harness] = executable
        with patch.object(self.app, "codex", str(executables["codex"])), \
                patch.object(self.app, "hermes", str(executables["hermes"])):
            sessions = self.window.open_swarm()
        pump_until(lambda: all(executable.with_suffix(".launch").exists()
                               for executable in executables.values()))
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            launch = json.loads(executables[session.harness].with_suffix(".launch").read_text())
            self.assertEqual(launch["cwd"], str(project))
            self.assertEqual(session.directory, str(project))
        self.assertEqual(shell.state, "running")
        self.assertIn(shell, self.window.sessions)

    def test_open_swarm_closes_startup_terminal_after_launching_in_its_navigated_folder(self):
        window, shell = self.startup_window()
        project = self.directory / "project with spaces 日本語"
        project.mkdir()
        shell.terminal.feed_child(("cd -- " + shlex.quote(str(project)) + "\n").encode())
        pump_until(lambda: shell.working_directory() == str(project))
        executables = self.harness_executables()
        with patch.object(self.app, "codex", str(executables["codex"])), \
                patch.object(self.app, "hermes", str(executables["hermes"])):
            sessions = window.open_swarm()
        pump_until(lambda: shell.state == "closed" and all(
            executable.with_suffix(".launch").exists() for executable in executables.values()))
        self.assertEqual(window.sessions, sessions)
        self.assertEqual(window.notebook.get_n_pages(), 2)
        self.assertIn(window.current(), sessions)
        for session in sessions:
            launch = json.loads(executables[session.harness].with_suffix(".launch").read_text())
            self.assertEqual(launch["cwd"], str(project))
            self.assertEqual(session.directory, str(project))
            self.assertEqual(session.state, "running")
        pump_until(lambda: not process_alive(shell.pid))

    def test_failed_swarm_spawn_preserves_startup_terminal(self):
        window, shell = self.startup_window()
        executables = self.harness_executables()
        executables["hermes"].write_text("#!/missing/swarm-test-interpreter\n")
        with patch.object(self.app, "codex", str(executables["codex"])), \
                patch.object(self.app, "hermes", str(executables["hermes"])):
            sessions = window.open_swarm()
        pump_until(lambda: sessions[0].state == "running" and sessions[1].state == "failed")
        self.assertIn(shell, window.sessions)
        self.assertEqual(shell.state, "running")
        self.assertTrue(process_alive(shell.pid))
        self.assertEqual(window.notebook.get_n_pages(), 3)

    def test_open_swarm_preserves_startup_terminal_with_foreground_or_background_job(self):
        executables = self.harness_executables()
        for background in (False, True):
            with self.subTest(background=background):
                window, shell = self.startup_window()
                if background:
                    marker = self.directory / "background-job.pid"
                    command = "/bin/sleep 60 & printf '%s' $! > " + shlex.quote(str(marker))
                    shell.terminal.feed_child((command + "\n").encode())
                    pump_until(lambda: marker.exists() and bool(marker.read_text()))
                    job_pid = int(marker.read_text())
                    pump_until(lambda: os.tcgetpgrp(shell.terminal.get_pty().get_fd()) == shell.pid)
                else:
                    shell.terminal.feed_child(b"/bin/sleep 60\n")
                    pump_until(lambda: os.tcgetpgrp(shell.terminal.get_pty().get_fd()) != shell.pid)
                    job_pid = os.tcgetpgrp(shell.terminal.get_pty().get_fd())
                with patch.object(self.app, "codex", str(executables["codex"])), \
                        patch.object(self.app, "hermes", str(executables["hermes"])):
                    sessions = window.open_swarm()
                pump_until(lambda: all(session.state == "running" for session in sessions))
                self.assertIn(shell, window.sessions)
                self.assertEqual(shell.state, "running")
                self.assertTrue(process_alive(job_pid))

    def test_open_swarm_removes_only_startup_terminal_and_keeps_extra_terminal(self):
        window, startup = self.startup_window()
        with patch.object(self.app, "shell_command", return_value=["/bin/bash", "--noprofile", "--norc", "-i"]):
            extra = window.new_terminal()
        pump_until(lambda: extra.state == "running")
        window.notebook.set_current_page(window.notebook.page_num(startup))
        executables = self.harness_executables()
        with patch.object(self.app, "codex", str(executables["codex"])), \
                patch.object(self.app, "hermes", str(executables["hermes"])):
            sessions = window.open_swarm()
        pump_until(lambda: startup.state == "closed")
        self.assertEqual(window.sessions, [extra, *sessions])
        self.assertEqual(extra.state, "running")
        self.assertTrue(process_alive(extra.pid))
        pump_until(lambda: not process_alive(startup.pid))

    def test_open_swarm_button_and_menu_activate_the_same_action(self):
        self.assertIsInstance(self.window.open_swarm_button, Gtk.Button)
        self.assertTrue(self.window.open_swarm_button.get_visible())
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            self.window.open_swarm_button.clicked()
            self.window.open_swarm_item.activate()
        self.assertEqual([session.harness for session in self.window.sessions],
                         ["codex", "hermes", "codex", "hermes"])

    def test_linked_agents_menu_opens_a_reusable_dialog(self):
        self.window.linked_agents_item.activate()
        dialog = self.window.linked_agents_dialog
        self.assertIsInstance(dialog, Gtk.Dialog)
        self.assertTrue(dialog.get_visible())
        self.window.open_linked_agents()
        self.assertIs(self.window.linked_agents_dialog, dialog)
        dialog.response(Gtk.ResponseType.CLOSE)
        self.assertIsNone(self.window.linked_agents_dialog)

    def test_switch_changes_persist_and_synchronize_dialogs_in_other_windows(self):
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        self.window.open_linked_agents()
        other.open_linked_agents()
        first = self.window.linked_agents_dialog
        second = other.linked_agents_dialog
        self.assertTrue(first.switches["codex"].get_active())
        self.assertTrue(first.switches["hermes"].get_active())
        first.switches["hermes"].set_active(False)
        self.assertFalse(self.app.linked_agents.enabled["hermes"])
        self.assertFalse(second.switches["hermes"].get_active())
        self.assertTrue(second.switches["codex"].get_active())
        reloaded = LinkedAgentsSettings(self.settings_path)
        self.assertEqual(reloaded.enabled, {"codex": True, "hermes": False})
        first.response(Gtk.ResponseType.CLOSE)
        self.window.open_linked_agents()
        self.assertFalse(self.window.linked_agents_dialog.switches["hermes"].get_active())

    def test_unsaved_switch_change_reverts_and_reports_failure(self):
        self.window.open_linked_agents()
        dialog = self.window.linked_agents_dialog
        with patch.object(self.app.linked_agents, "set_enabled", side_effect=OSError("Disk full")):
            dialog.switches["hermes"].set_active(False)
        self.assertTrue(dialog.switches["hermes"].get_active())
        self.assertTrue(self.app.linked_agents.enabled["hermes"])
        self.assertIn("Disk full", dialog.feedback.get_text())
        self.assertTrue(LinkedAgentsSettings(self.settings_path).enabled["hermes"])

    def test_codex_logout_blocks_codex_swarm_but_allows_hermes_only(self):
        self.app.logout_pending = True
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            self.assertEqual(self.window.open_swarm(), [])
            resolve.assert_not_called()
            start.assert_not_called()
            self.app.set_linked_agent_enabled("codex", False)
            sessions = self.window.open_swarm()
        self.assertEqual([session.harness for session in sessions], ["hermes"])
        resolve.assert_called_once_with(self.app.hermes)
        start.assert_called_once_with(["/test/swarm-hermes"])

    def test_hermes_restart_keeps_original_harness_even_when_disabled(self):
        self.app.set_linked_agent_enabled("codex", False)
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            original = self.window.open_swarm()[0]
        original.state = "exited"
        original.title = "Research assistant"
        self.app.set_linked_agent_enabled("hermes", False)
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            self.window.restart_current()
        replacement = self.window.current()
        self.assertIsNot(replacement, original)
        self.assertEqual(replacement.harness, "hermes")
        self.assertEqual(replacement.title, "Research assistant")
        self.assertEqual(replacement.directory, str(self.directory))
        self.assertFalse(replacement.managed_activity_title)
        resolve.assert_called_once_with(self.app.hermes)
        start.assert_called_once_with(["/test/swarm-hermes"])

    def test_new_agent_remains_codex_when_linked_codex_is_disabled(self):
        self.app.set_linked_agent_enabled("codex", False)
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start") as start:
            session = self.window.new_agent()
        self.assertEqual(session.harness, "codex")
        start.assert_called_once_with(codex_agent_command("/test/swarm-codex"))

    def test_new_agent_shortcuts_match_selected_swarm_harness_and_folder(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            originals = self.window.open_swarm()
        for original in originals:
            folder = self.directory / (original.harness + " project 日本語")
            folder.mkdir()
            original.directory = str(folder)
        for shortcut in ("<Primary>t", "<Primary><Shift>t"):
            for original in originals:
                with self.subTest(shortcut=shortcut, harness=original.harness):
                    self.window.notebook.set_current_page(self.window.notebook.page_num(original))
                    before = list(self.window.sessions)
                    with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                            patch.object(TerminalSession, "start", autospec=True) as start:
                        key, modifiers = Gtk.accelerator_parse(shortcut)
                        activated = Gtk.accel_groups_activate(self.window, key, modifiers)
                    self.assertTrue(activated)
                    session = self.window.current()
                    self.assertEqual(self.window.sessions, [*before, session])
                    self.assertNotIn(session, before)
                    self.assertEqual(self.window.notebook.get_n_pages(), len(before) + 1)
                    self.assertEqual(session.harness, original.harness)
                    self.assertEqual(session.kind, "agent")
                    self.assertEqual(session.directory, original.directory)
                    self.assertEqual(session.managed_activity_title, original.harness == "codex")
                    executable = getattr(self.app, original.harness)
                    resolve.assert_called_once_with(executable)
                    command = codex_agent_command(executable) if original.harness == "codex" else [executable]
                    start.assert_called_once_with(session, command)

    def test_new_agent_shortcut_matches_exited_agent_even_when_unlinked(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            originals = self.window.open_swarm()
        self.app.set_linked_agent_enabled("codex", False)
        self.app.set_linked_agent_enabled("hermes", False)
        for original in originals:
            with self.subTest(harness=original.harness):
                original.state = "exited"
                original.title = "Research assistant"
                self.window.notebook.set_current_page(self.window.notebook.page_num(original))
                before = list(self.window.sessions)
                with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                        patch.object(TerminalSession, "start") as start:
                    key, modifiers = Gtk.accelerator_parse("<Primary>t")
                    self.assertTrue(Gtk.accel_groups_activate(self.window, key, modifiers))
                session = self.window.current()
                self.assertEqual(self.window.sessions, [*before, session])
                self.assertEqual(session.harness, original.harness)
                self.assertEqual(session.directory, original.directory)
                executable = getattr(self.app, original.harness)
                resolve.assert_called_once_with(executable)
                command = codex_agent_command(executable) if original.harness == "codex" else [executable]
                start.assert_called_once_with(command)

    def test_codex_logout_blocks_codex_shortcut_but_allows_hermes_shortcut(self):
        with patch("swarm_app.app.shutil.which", side_effect=self.executable), \
                patch.object(TerminalSession, "start"):
            codex, hermes = self.window.open_swarm()
        self.app.logout_pending = True
        key, modifiers = Gtk.accelerator_parse("<Primary>t")
        self.window.notebook.set_current_page(self.window.notebook.page_num(codex))
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            self.assertTrue(Gtk.accel_groups_activate(self.window, key, modifiers))
            self.assertEqual(self.window.sessions, [codex, hermes])
            resolve.assert_not_called()
            start.assert_not_called()
            self.window.notebook.set_current_page(self.window.notebook.page_num(hermes))
            self.assertTrue(Gtk.accel_groups_activate(self.window, key, modifiers))
        session = self.window.current()
        self.assertEqual(self.window.sessions, [codex, hermes, session])
        self.assertEqual(session.harness, "hermes")
        resolve.assert_called_once_with(self.app.hermes)
        start.assert_called_once_with(["/test/swarm-hermes"])

    def test_ctrl_t_from_terminal_defaults_to_codex(self):
        with patch.object(TerminalSession, "start"):
            shell = self.window.new_terminal()
        self.app.set_linked_agent_enabled("codex", False)
        with patch("swarm_app.app.shutil.which", side_effect=self.executable) as resolve, \
                patch.object(TerminalSession, "start") as start:
            key, modifiers = Gtk.accelerator_parse("<Primary>t")
            self.assertTrue(Gtk.accel_groups_activate(self.window, key, modifiers))
        session = self.window.current()
        self.assertEqual(self.window.sessions, [shell, session])
        self.assertEqual(session.harness, "codex")
        self.assertEqual(session.directory, shell.directory)
        resolve.assert_called_once_with(self.app.codex)
        start.assert_called_once_with(codex_agent_command("/test/swarm-codex"))

    def test_mixed_swarm_spinners_and_shared_total_follow_both_harnesses(self):
        sessions = {}
        for harness in ("codex", "hermes"):
            folder = self.directory / harness
            folder.mkdir()
            session = self.window.add_session(
                harness.title(), "agent", ["/usr/bin/python3", str(FIXTURE), str(folder)],
                str(folder), managed_activity_title=harness == "codex", harness=harness)
            pump_until(lambda: session.state == "running" and (folder / "input").exists())
            sessions[harness] = session
        codex, hermes = sessions["codex"], sessions["hermes"]
        codex.terminal.feed(b"\x1b]0;Working\x07")
        rule = "─" * hermes.terminal.get_column_count()
        hermes.terminal.feed(f"\x1b[2J\x1b[H{rule}\r\n⚕ Thinking...".encode())
        # The normal window poll must observe classic Hermes prompt redraws.
        pump_until(lambda: self.window.status_label.get_text() == "2 agents · 2 running")
        for session in sessions.values():
            spinner = self.window.tab_spinners[session]
            self.assertTrue(spinner.get_visible())
            self.assertTrue(spinner.get_property("active"))
        hermes.terminal.feed("\x1b]0;✓ Hermes\x07".encode())
        pump_until(lambda: self.window.status_label.get_text() == "2 agents · 1 running")
        self.assertFalse(self.window.tab_spinners[hermes].get_visible())
        self.assertFalse(self.window.tab_spinners[hermes].get_property("active"))
        self.assertTrue(self.window.tab_spinners[codex].get_visible())
        hermes.terminal.feed("\x1b]0;⏳ Hermes\x07".encode())
        pump_until(lambda: self.window.status_label.get_text() == "2 agents · 2 running")
        self.assertTrue(self.window.tab_spinners[hermes].get_visible())
        os.kill(hermes.pid, signal.SIGTERM)
        pump_until(lambda: self.window.status_label.get_text() == "1 agent · 1 running")
        self.assertEqual(hermes.state, "exited")
        self.assertFalse(self.window.tab_spinners[hermes].get_visible())
        self.assertFalse(self.window.tab_spinners[hermes].get_property("active"))

    def test_mixed_swarm_broadcasts_work_without_codex_only_finish_notifications(self):
        folders = []
        for harness in ("codex", "hermes"):
            folder = self.directory / harness
            folder.mkdir()
            session = self.window.add_session(
                harness.title(), "agent", ["/usr/bin/python3", str(FIXTURE), str(folder)],
                str(folder), harness=harness)
            pump_until(lambda: session.state == "running" and (folder / "input").exists())
            folders.append(folder)
            if harness == "codex":
                self.window.open_broadcast()
                dialog = self.window.broadcast_dialog
                self.assertTrue(dialog.notify_checkbox.get_sensitive())
                dialog.notify_checkbox.set_active(True)
        dialog._refresh()
        self.assertFalse(dialog.notify_checkbox.get_sensitive())
        self.assertFalse(dialog.notify_checkbox.get_active())
        self.assertIn("Codex-only", dialog.notify_help.get_text())
        self.assertFalse(self.window.broadcast("Notify after task", notify_when_done=True))
        self.assertEqual(self.window.broadcast_watches, [])
        for folder in folders:
            self.assertEqual((folder / "input").read_bytes(), b"")
        self.assertTrue(self.window.broadcast("Ordinary mixed task"))
        pump_until(lambda: all((folder / "input").read_bytes().endswith(b"\r") for folder in folders))
        for folder in folders:
            self.assertIn(b"Ordinary mixed task", (folder / "input").read_bytes())
        dialog.response(Gtk.ResponseType.CANCEL)


if __name__ == "__main__":
    unittest.main()
