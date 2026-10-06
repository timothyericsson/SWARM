"""DeepSeek badge positioning, honest credit details and async lifecycle."""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from swarm_app.app import GLib, Gtk, SwarmApplication
from swarm_app.deepseek_usage import CreditBalance, DeepSeekSnapshot
from swarm_app.usage import UsageUnavailable
from gtk_test_support import shutdown_application
from test_session import pump_until


SNAPSHOT = DeepSeekSnapshot((CreditBalance("USD", Decimal("7.49"), Decimal("10")),), True)


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class DeepSeekUsageWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        for name in ("fetch_zai_usage", "fetch_deepseek_usage"):
            mock = patch("swarm_app.app." + name, return_value=None)
            mock.start()
            cls.addClassCleanup(mock.stop)
        cls.app = SwarmApplication("/tmp", "/no-such-codex", "/no-such-hermes",
                                   linked_agents_path=Path(cls.temp.name) / "linked-agents.json")
        cls.app.set_application_id("io.swarm.Terminal.DeepSeekUsageTests")
        if not cls.app.register(None):
            raise AssertionError("Could not register test application")
        cls.app.hold()

    @classmethod
    def tearDownClass(cls):
        cls.app.release()
        shutdown_application(cls.app)

    def setUp(self):
        self.app.invalidate_deepseek_usage("Unavailable")
        self.app.auth_state = "signed_out"
        self.app.auth_pending = False
        self.app.logout_pending = False
        self.window = self.app.new_window(start_terminal=False)
        self.windows = [self.window]

    def tearDown(self):
        self.app.invalidate_deepseek_usage("Test finished")
        self.app.auth_pending = False
        self.app.logout_pending = False
        for window in self.windows:
            window.destroy()
        pump_until(lambda: not self.app.get_windows())

    def show_snapshot(self):
        self.app.deepseek_usage_snapshot = SNAPSHOT
        self.app.deepseek_usage_updated_at = 1900000000
        self.app.sync_deepseek_usage()

    def test_blue_percentage_follows_zai_and_explains_baseline_and_dollars(self):
        self.show_snapshot()
        button = self.window.deepseek_usage_button
        siblings = button.get_parent().get_children()
        self.assertEqual(siblings.index(button), siblings.index(self.window.zai_usage_button) + 1)
        self.assertEqual(button.get_label(), "74%")
        pump_until(lambda: button.get_allocation().width > 1)
        self.assertGreaterEqual(button.get_allocation().x,
                                self.window.zai_usage_button.get_allocation().x
                                + self.window.zai_usage_button.get_allocation().width)
        color = button.get_child().get_style_context().get_color(Gtk.StateFlags.NORMAL)
        self.assertAlmostEqual(color.blue, 1, places=3)
        self.assertAlmostEqual(color.red, 128 / 255, places=3)
        details = self.window.deepseek_usage_details.get_text()
        self.assertIn("USD 7.49 available", details)
        self.assertIn("highest balance observed", details)
        self.assertIn("no daily or weekly", details)
        self.assertIn("USD 10.00 tracked credit", details)
        self.assertIn(details, button.get_tooltip_text())
        self.assertIn("74%", button.get_accessible().get_name())

    def test_click_and_refresh_use_deepseek_and_poll_includes_all_providers(self):
        with patch.object(self.app, "refresh_deepseek_usage") as deepseek, \
                patch.object(self.app, "refresh_zai_usage") as zai, \
                patch.object(self.app, "refresh_usage") as codex:
            self.window.deepseek_usage_button.clicked()
            self.assertTrue(self.window.deepseek_usage_panel.get_visible())
            self.window.deepseek_usage_refresh_button.clicked()
            self.assertEqual(deepseek.call_count, 2)
            zai.assert_not_called()
            codex.assert_not_called()
            deepseek.reset_mock()
            self.assertEqual(self.app._poll_usage(), GLib.SOURCE_CONTINUE)
            deepseek.assert_called_once_with()
            zai.assert_called_once_with()
            codex.assert_called_once_with()

    def test_shared_worker_deduplicates_without_blocking_gtk_or_codex_auth(self):
        self.windows.append(self.app.new_window(start_terminal=False))
        entered, release = threading.Event(), threading.Event()
        heartbeat = []

        def fetch(**kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Worker not released")
            return SNAPSHOT

        self.app.auth_pending = True
        self.app.logout_pending = True
        with patch("swarm_app.app.fetch_deepseek_usage", side_effect=fetch) as request:
            try:
                self.assertTrue(self.app.refresh_deepseek_usage())
                pump_until(entered.is_set)
                self.assertFalse(self.app.refresh_deepseek_usage())
                request.assert_called_once()
                GLib.idle_add(lambda: heartbeat.append(True) or GLib.SOURCE_REMOVE)
                pump_until(lambda: bool(heartbeat))
                for window in self.windows:
                    self.assertFalse(window.deepseek_usage_refresh_button.get_sensitive())
            finally:
                release.set()
                pump_until(lambda: not self.app.deepseek_usage_pending)
        for window in self.windows:
            self.assertEqual(window.deepseek_usage_button.get_label(), "74%")
            self.assertTrue(window.deepseek_usage_refresh_button.get_sensitive())

    def test_failed_refresh_clears_number_and_late_cancelled_result_is_ignored(self):
        self.show_snapshot()
        with patch("swarm_app.app.fetch_deepseek_usage", side_effect=UsageUnavailable("Offline")):
            self.app.refresh_deepseek_usage()
            pump_until(lambda: not self.app.deepseek_usage_pending)
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "—%")
        self.assertIsNone(self.app.deepseek_usage_updated_at)
        self.assertIn("Offline", self.window.deepseek_usage_details.get_text())
        generation = self.app.deepseek_usage_generation
        self.app.invalidate_deepseek_usage("Closed")
        self.app._deepseek_usage_finished(generation, SNAPSHOT, None)
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "—%")

    def test_insufficient_credit_is_visible(self):
        self.app.deepseek_usage_snapshot = DeepSeekSnapshot((CreditBalance("USD", Decimal("0"), Decimal("10")),), False)
        self.app.sync_deepseek_usage()
        self.assertEqual(self.window.deepseek_usage_button.get_label(), "0%")
        self.assertIn("insufficient credit", self.window.deepseek_usage_details.get_text())


if __name__ == "__main__":
    unittest.main()
