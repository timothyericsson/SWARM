"""Automatic credential sync, balance refresh and watcher lifecycle in GTK."""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from swarm_app.app import SwarmApplication
from swarm_app.deepseek_usage import CreditBalance, DeepSeekSnapshot, _load_api_key
from gtk_test_support import shutdown_application
from test_session import pump_until


SNAPSHOT = DeepSeekSnapshot((CreditBalance("USD", Decimal("5"), Decimal("10")),), True)


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class DeepSeekKeyWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "deepseek.txt"
        self.home = self.root / "hermes"
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "DEEPSEEK_API_KEY": "sk-stale-parent"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fetch = patch("swarm_app.app.fetch_deepseek_usage", return_value=SNAPSHOT)
        self.request = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.source.write_text("sk-startup-key\n")
        self.app = SwarmApplication(str(self.root), "/no-such-codex", "/no-such-hermes",
                                    linked_agents_path=self.root / "linked-agents.json",
                                    deepseek_key_path=self.source)
        self.app.set_application_id("io.swarm.Terminal.DeepSeekKeyTests")
        self.assertTrue(self.app.register(None))
        self.app.hold()
        self.window = self.app.new_window(start_terminal=False)

    def tearDown(self):
        self.window.destroy()
        pump_until(lambda: not self.app.get_windows())
        self.app.release()
        shutdown_application(self.app)
        self.assertEqual(self.app.deepseek_key_poll, 0)

    def test_startup_and_periodic_rotation_refresh_balance_and_cancel_stale_request(self):
        self.assertEqual(_load_api_key(), "sk-startup-key")
        self.assertNotEqual(self.app.deepseek_key_poll, 0)
        self.request.assert_not_called()
        self.app.deepseek_usage_snapshot = SNAPSHOT
        self.app.deepseek_usage_pending = True
        cancel = threading.Event()
        self.app.deepseek_usage_cancel = cancel
        generation = self.app.deepseek_usage_generation
        replacement = self.root / "replacement"
        replacement.write_text("sk-rotated-key\n")
        os.replace(replacement, self.source)
        pump_until(lambda: _load_api_key() == "sk-rotated-key", timeout=5)
        pump_until(lambda: not self.app.deepseek_usage_pending)
        self.assertTrue(cancel.is_set())
        self.request.assert_called_once()
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "50%")
        self.app._deepseek_usage_finished(generation, None, "stale failure")
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "50%")
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-rotated-key")

    def test_invalid_source_is_visible_and_recovery_clears_warning(self):
        self.source.write_text("private-invalid-key\n")
        self.app._poll_deepseek_key()
        self.assertEqual(_load_api_key(), "sk-startup-key")
        self.assertIn("deepseek.txt must contain", self.window.deepseek_usage_details.get_text())
        self.assertNotIn("private-invalid-key", self.window.deepseek_usage_details.get_text())
        self.request.assert_not_called()
        self.source.write_text("sk-recovered-key\n")
        self.app._poll_deepseek_key()
        pump_until(lambda: not self.app.deepseek_usage_pending)
        self.assertIsNone(self.app.deepseek_key_error)
        self.assertNotIn("deepseek.txt must contain", self.window.deepseek_usage_details.get_text())
        self.assertEqual(_load_api_key(), "sk-recovered-key")


if __name__ == "__main__":
    unittest.main()
