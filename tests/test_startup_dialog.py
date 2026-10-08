"""Startup command drafts, explicit saves and concurrent window edits."""

from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from swarm_app.linked_agents import StartupAgent
from swarm_app.linked_agents_dialog import LinkedAgentsDialog


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class StartupDialogTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(agents=[
            StartupAgent("codex", "Codex", "codex --yolo"),
            StartupAgent("hermes", "GLM", "hermes chat --model glm-5.3-flash --yolo"),
            StartupAgent("deepseek", "DeepSeek", "hermes chat --model deepseek-flash --yolo"),
        ], dont_show_again=False, load_error=None)
        self.app = SimpleNamespace(linked_agents=self.settings)
        self.app.save_startup_swarm = Mock(side_effect=self.save)
        self.windows = []
        self.window, self.dialog = self.open_dialog()

    def tearDown(self):
        for window in self.windows:
            window.destroy()

    def open_dialog(self):
        window = Gtk.Window()
        window.app = self.app
        window.linked_agents_dialog = None
        self.windows.append(window)
        window.linked_agents_dialog = LinkedAgentsDialog(window)
        return window, window.linked_agents_dialog

    def save(self, agents, dont_show_again):
        self.settings.agents = agents
        self.settings.dont_show_again = dont_show_again
        self.settings.load_error = None
        for window in self.windows:
            if window.linked_agents_dialog:
                window.linked_agents_dialog.refresh()
        return True

    def test_three_rows_and_edits_are_only_committed_on_save(self):
        dialog = self.dialog
        self.assertEqual(len(dialog.rows), 3)
        self.assertEqual(dialog.get_title(), "Startup Swarm")
        dialog.rows[0].command_entry.set_text('codex --yolo --model "my model"')
        dialog.rows[1].enabled.set_active(False)
        dialog.dont_show_again.set_active(True)
        self.app.save_startup_swarm.assert_not_called()
        self.assertFalse(self.settings.dont_show_again)
        self.assertTrue(self.settings.agents[1].enabled)
        dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(self.settings.agents[0].command, 'codex --yolo --model "my model"')
        self.assertFalse(self.settings.agents[1].enabled)
        self.assertTrue(self.settings.dont_show_again)
        self.assertIsNone(self.window.linked_agents_dialog)
        self.app.save_startup_swarm.assert_called_once()

    def test_add_remove_preserves_order_and_cancel_leaves_settings_unchanged(self):
        self.dialog.add_button_widget.clicked()
        new = self.dialog.rows[-1]
        new.name_entry.set_text("Custom")
        new.command_entry.set_text("custom-agent --yes")
        self.dialog.rows[1].remove_button.clicked()
        self.assertEqual([row.name_entry.get_text() for row in self.dialog.rows],
                         ["Codex", "DeepSeek", "Custom"])
        self.assertNotIn(new.agent_id, [agent.id for agent in self.settings.agents])
        self.dialog.response(Gtk.ResponseType.CANCEL)
        self.app.save_startup_swarm.assert_not_called()
        self.assertEqual(len(self.settings.agents), 3)
        self.assertIsNone(self.window.linked_agents_dialog)

    def test_save_failure_keeps_the_draft_and_can_be_retried(self):
        self.dialog.rows[0].command_entry.set_text("invalid command")
        self.settings.load_error = "Could not save: disk full"
        self.app.save_startup_swarm.side_effect = None
        self.app.save_startup_swarm.return_value = False
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertIs(self.window.linked_agents_dialog, self.dialog)
        self.assertEqual(self.dialog.rows[0].command_entry.get_text(), "invalid command")
        self.assertIn("disk full", self.dialog.feedback.get_text())
        self.assertEqual(self.settings.agents[0].command, "codex --yolo")
        self.app.save_startup_swarm.side_effect = self.save
        self.dialog.rows[0].command_entry.set_text("codex --yolo --model other")
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertIsNone(self.window.linked_agents_dialog)

    def test_clean_dialog_refreshes_after_another_window_saves(self):
        _, other = self.open_dialog()
        self.dialog.rows[0].name_entry.set_text("Updated Codex")
        self.dialog.dont_show_again.set_active(True)
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(other.rows[0].name_entry.get_text(), "Updated Codex")
        self.assertTrue(other.dont_show_again.get_active())
        other.response(Gtk.ResponseType.OK)
        self.assertEqual(self.app.save_startup_swarm.call_count, 2)

    def test_dirty_dialog_preserves_edits_and_cannot_overwrite_another_save(self):
        _, other = self.open_dialog()
        other.rows[0].name_entry.set_text("Unsaved local name")
        self.dialog.rows[0].name_entry.set_text("Saved elsewhere")
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(other.rows[0].name_entry.get_text(), "Unsaved local name")
        self.assertIn("another window", other.feedback.get_text())
        other.response(Gtk.ResponseType.OK)
        self.assertEqual(self.settings.agents[0].name, "Saved elsewhere")
        self.assertEqual(self.app.save_startup_swarm.call_count, 1)
        self.assertTrue(other.get_visible())

    def test_stale_save_is_rejected_even_without_a_refresh_notification(self):
        self.dialog.rows[1].enabled.set_active(False)
        self.settings.agents[0] = replace(self.settings.agents[0], command="codex --model other")
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_startup_swarm.assert_not_called()
        self.assertIn("another window", self.dialog.feedback.get_text())


if __name__ == "__main__":
    unittest.main()
