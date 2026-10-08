"""Local provider detection, explicit key targets, and secret-free saved-key UI."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from swarm_app.credentials import CredentialStore
from swarm_app.credentials_dialog import CredentialsDialog
from swarm_app.linked_agents import StartupAgent
from test_session import GTK_AVAILABLE


GENERIC_KEY = "sk-test-placeholder-12345678901234567890"
OPENAI_KEY = "sk-proj-test-placeholder-12345678901234567890"


@unittest.skipUnless(GTK_AVAILABLE, "A display or xvfb-run is required")
class CredentialsDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CredentialStore(Path(self.temp.name) / "keys.json")
        agents = [StartupAgent("codex", "My Codex", "codex --yolo"),
                  StartupAgent("glm", "My GLM", "hermes chat --provider zai --model glm-5.3-flash --yolo"),
                  StartupAgent("deepseek", "My DeepSeek", "hermes chat --provider deepseek --model deepseek-flash --yolo"),
                  StartupAgent("custom", "My wrapper", "my-helper")]
        self.app = SimpleNamespace(credentials=self.store, credential_error=None,
                                   linked_agents=SimpleNamespace(agents=agents))
        self.app.save_api_key = Mock(side_effect=self.save)
        self.app.delete_api_key = Mock(side_effect=self.delete)
        self.window = Gtk.Window()
        self.window.app = self.app
        self.window.credentials_dialog = None
        self.dialog = CredentialsDialog(self.window)
        self.window.credentials_dialog = self.dialog

    def tearDown(self):
        self.window.destroy()

    def save(self, provider, key, *, agent_id=None, variable=None):
        self.store.set(provider, key, agent_id=agent_id, variable=variable)
        self.app.credential_error = None
        self.dialog.refresh()
        return True

    def delete(self, provider, *, agent_id=None):
        self.store.delete(provider, agent_id=agent_id)
        self.app.credential_error = None
        self.dialog.refresh()
        return True

    def test_generic_key_requires_choice_and_is_masked_until_explicitly_shown(self):
        self.assertFalse(self.dialog.key_entry.get_visibility())
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.assertIn("Choose a provider", self.dialog.detection.get_text())
        self.dialog.show_key.set_active(True)
        self.assertTrue(self.dialog.key_entry.get_visibility())
        self.dialog.provider.set_active_id("deepseek")
        self.assertTrue(self.dialog.save_button.get_sensitive())
        self.assertIn("DeepSeek", self.dialog.detection.get_text())
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(self.store.get("deepseek").secret, GENERIC_KEY)
        self.assertEqual(self.dialog.key_entry.get_text(), "")
        self.assertFalse(self.dialog.key_entry.get_visibility())
        self.assertTrue(self.dialog.get_visible())

    def test_assignment_auto_identifies_provider_without_sending_assignment_text_as_secret(self):
        self.dialog.key_entry.set_text("DEEPSEEK_API_KEY=" + GENERIC_KEY)
        self.assertTrue(self.dialog.save_button.get_sensitive())
        self.assertIn("DeepSeek", self.dialog.detection.get_text())
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_api_key.assert_called_once_with("deepseek", GENERIC_KEY, agent_id=None, variable=None)
        self.assertEqual(self.store.get("deepseek").secret, GENERIC_KEY)

    def test_selected_startup_agent_resolves_generic_key_and_saves_only_for_that_agent(self):
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.target.set_active_id("agent:deepseek")
        self.assertTrue(self.dialog.save_button.get_sensitive())
        self.assertIn("startup agent My DeepSeek", self.dialog.detection.get_text())
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertIsNone(self.store.get("deepseek"))
        self.assertEqual(self.store.get("deepseek", "deepseek").secret, GENERIC_KEY)
        self.assertIn("My DeepSeek", self.dialog.saved_rows[0].label.get_text())

    def test_identified_provider_cannot_be_routed_to_an_incompatible_agent(self):
        self.dialog.key_entry.set_text(OPENAI_KEY)
        self.dialog.target.set_active_id("agent:glm")
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_api_key.assert_not_called()
        self.assertIn("matching provider", self.dialog.feedback.get_text())
        self.dialog.target.set_active_id("default")
        self.dialog.provider.set_active_id("deepseek")
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_api_key.assert_not_called()
        self.assertNotIn(OPENAI_KEY, self.dialog.feedback.get_text())

    def test_custom_provider_requires_environment_variable_and_cannot_replace_codex_provider(self):
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.provider.set_active_id("custom")
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.assertIn("Choose a startup agent", self.dialog.detection.get_text())
        self.dialog.target.set_active_id("agent:custom")
        self.assertTrue(self.dialog.variable.get_visible())
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.variable.set_text("MY_SERVICE_API_KEY")
        self.assertTrue(self.dialog.save_button.get_sensitive())
        self.dialog.target.set_active_id("agent:codex")
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_api_key.assert_not_called()
        self.dialog.target.set_active_id("agent:custom")
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(self.store.get("custom", "custom").variable, "MY_SERVICE_API_KEY")

    def test_failed_save_retains_draft_and_displays_safe_error(self):
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.provider.set_active_id("deepseek")
        self.app.save_api_key.side_effect = None
        self.app.save_api_key.return_value = False
        self.app.credential_error = "Could not save keys. Check folder permissions."
        self.dialog.response(Gtk.ResponseType.OK)
        self.assertEqual(self.dialog.key_entry.get_text(), GENERIC_KEY)
        self.assertTrue(self.dialog.get_visible())
        self.assertEqual(self.dialog.feedback.get_text(), self.app.credential_error)
        self.assertIsNone(self.store.get("deepseek"))

    def test_saved_key_metadata_supports_replace_and_remove_without_showing_secret(self):
        self.store.set("deepseek", GENERIC_KEY, agent_id="deepseek")
        self.dialog.refresh()
        self.assertEqual(len(self.dialog.saved_rows), 1)
        self.assertNotIn(GENERIC_KEY, self.dialog.saved_rows[0].label.get_text())
        self.dialog.key_entry.set_text("draft to discard")
        self.dialog.show_key.set_active(True)
        self.dialog.saved_rows[0].replace_button.clicked()
        self.assertEqual(self.dialog.provider.get_active_id(), "deepseek")
        self.assertEqual(self.dialog.target.get_active_id(), "agent:deepseek")
        self.assertEqual(self.dialog.key_entry.get_text(), "")
        self.assertFalse(self.dialog.key_entry.get_visibility())
        self.dialog.saved_rows[0].remove_button.clicked()
        self.app.delete_api_key.assert_called_once_with("deepseek", agent_id="deepseek")
        self.assertIsNone(self.store.get("deepseek", "deepseek"))
        self.assertEqual(self.dialog.saved_rows, [])

    def test_refresh_retains_draft_and_blocks_removed_target_instead_of_changing_scope(self):
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.target.set_active_id("agent:deepseek")
        self.app.linked_agents.agents = [agent for agent in self.app.linked_agents.agents if agent.id != "deepseek"]
        self.dialog.refresh()
        self.assertEqual(self.dialog.key_entry.get_text(), GENERIC_KEY)
        self.assertEqual(self.dialog.target.get_active_id(), "agent:deepseek")
        self.assertFalse(self.dialog.save_button.get_sensitive())
        self.dialog.response(Gtk.ResponseType.OK)
        self.app.save_api_key.assert_not_called()
        self.assertIn("no longer exists", self.dialog.feedback.get_text())

    def test_failed_remove_retains_saved_metadata(self):
        self.store.set("deepseek", GENERIC_KEY)
        self.dialog.refresh()
        self.app.delete_api_key.side_effect = None
        self.app.delete_api_key.return_value = False
        self.app.credential_error = "Could not update saved keys."
        self.dialog.saved_rows[0].remove_button.clicked()
        self.assertEqual(len(self.dialog.saved_rows), 1)
        self.assertIsNotNone(self.store.get("deepseek"))
        self.assertEqual(self.dialog.feedback.get_text(), self.app.credential_error)

    def test_agent_override_does_not_claim_to_replace_the_provider_default(self):
        self.store.set("deepseek", GENERIC_KEY)
        self.dialog.refresh()
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.target.set_active_id("agent:deepseek")
        self.assertNotIn("replaces", self.dialog.detection.get_text())
        self.store.set("deepseek", GENERIC_KEY, agent_id="deepseek")
        self.dialog.refresh()
        self.assertIn("replaces", self.dialog.detection.get_text())

    def test_close_clears_secret_entry_and_owner_dialog_reference(self):
        self.dialog.key_entry.set_text(GENERIC_KEY)
        self.dialog.response(Gtk.ResponseType.CLOSE)
        self.assertEqual(self.dialog.key_entry.get_text(), "")
        self.assertIsNone(self.window.credentials_dialog)
        self.app.save_api_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
