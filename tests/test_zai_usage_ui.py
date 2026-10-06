"""Z.ai usage badge integration checks with local, deterministic usage responses."""

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from swarm_app.app import GLib, Gtk, SwarmApplication
from swarm_app.usage import UsageSnapshot, UsageUnavailable, UsageWindow
from gtk_test_support import shutdown_application
from test_session import pump_until


SNAPSHOT = UsageSnapshot(UsageWindow(74.9, 300, 1900000000),
                         UsageWindow(32.4, 10080, 1900600000))


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class ZaiUsageWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deepseek_patch = patch("swarm_app.app.fetch_deepseek_usage", return_value=None)
        deepseek_patch.start()
        cls.addClassCleanup(deepseek_patch.stop)
        cls.config_temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.config_temp.cleanup)
        cls.fetch_patch = patch("swarm_app.app.fetch_zai_usage", return_value=UsageSnapshot(None, None))
        cls.fetch_patch.start()
        cls.addClassCleanup(cls.fetch_patch.stop)
        cls.app = SwarmApplication("/tmp", "/no-such-swarm-codex", "/no-such-swarm-hermes",
                                   linked_agents_path=Path(cls.config_temp.name) / "linked-agents.json")
        cls.app.set_application_id("io.swarm.Terminal.ZaiUsageTests")
        if not cls.app.register(None):
            raise AssertionError("The test application could not register")
        cls.app.hold()

    @classmethod
    def tearDownClass(cls):
        cls.app.release()
        shutdown_application(cls.app)

    def setUp(self):
        self.app.auth_status = "Signed out"
        self.app.auth_state = "signed_out"
        self.app.auth_pending = False
        self.app.auth_refresh_requested = False
        self.app.logout_pending = False
        self.app.invalidate_usage("Signed out")
        self.app.invalidate_zai_usage("Z.ai usage unavailable")
        self.window = self.app.new_window(start_terminal=False)
        self.windows = [self.window]

    def tearDown(self):
        self.app.auth_pending = False
        self.app.logout_pending = False
        self.app.invalidate_zai_usage("Test finished")
        for window in self.windows:
            window.destroy()
        pump_until(lambda: not self.app.get_windows())

    def show_snapshot(self, snapshot=SNAPSHOT):
        self.app.zai_usage_snapshot = snapshot
        self.app.zai_usage_updated_at = 1899999000
        self.app.sync_zai_usage()

    def assert_orange(self):
        label = self.window.zai_usage_button.get_child()
        color = label.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        self.assertAlmostEqual(color.red, 245 / 255, places=3)
        self.assertAlmostEqual(color.green, 164 / 255, places=3)
        self.assertAlmostEqual(color.blue, 93 / 255, places=3)

    def test_orange_badge_is_immediately_right_of_codex_and_floors_remaining(self):
        self.show_snapshot()
        codex = self.window.usage_button
        zai = self.window.zai_usage_button
        self.assertIs(zai.get_parent(), codex.get_parent())
        siblings = zai.get_parent().get_children()
        self.assertEqual(siblings.index(zai), siblings.index(codex) + 1)
        pump_until(lambda: zai.get_allocation().width > 1)
        self.assertGreaterEqual(zai.get_allocation().x,
                                codex.get_allocation().x + codex.get_allocation().width)
        self.assertEqual(zai.get_label(), "74%")
        self.assertTrue(zai.get_style_context().has_class("usage-badge"))
        self.assertTrue(zai.get_style_context().has_class("zai-usage-badge"))
        self.assert_orange()

    def test_low_usage_stays_orange_and_does_not_change_codex_badge(self):
        self.app.usage_snapshot = UsageSnapshot(UsageWindow(88, 300, None), None)
        self.app.sync_usage()
        self.show_snapshot(UsageSnapshot(UsageWindow(4.8, 300, None), None))
        self.assertEqual(self.window.zai_usage_button.get_label(), "4%")
        self.assertEqual(self.window.usage_button.get_label(), "88%")
        self.assert_orange()

    def test_details_tooltip_and_accessibility_describe_both_windows(self):
        self.show_snapshot()
        detail = self.window.zai_usage_details.get_text()
        self.assertIn("5-hour: 74% left", detail)
        self.assertIn("Weekly: 32% left", detail)
        self.assertIn("Resets", detail)
        self.assertIn("local time", detail)
        tooltip = self.window.zai_usage_button.get_tooltip_text()
        self.assertIsNotNone(tooltip)
        self.assertIn("74%", tooltip)
        self.assertIn("Z.ai", tooltip)
        accessible = self.window.zai_usage_button.get_accessible()
        self.assertIn("GLM", accessible.get_name())
        self.assertIn("74%", accessible.get_name())
        self.assertIn(detail, accessible.get_description())
        self.assertIn("Updated", self.window.zai_usage_updated.get_text())

    def test_click_opens_usage_panel_and_manual_refresh_uses_zai(self):
        self.show_snapshot()
        with patch.object(self.app, "refresh_zai_usage") as refresh_zai, \
                patch.object(self.app, "refresh_usage") as refresh_codex:
            self.window.zai_usage_button.clicked()
            self.assertTrue(self.window.zai_usage_panel.get_visible())
            refresh_zai.assert_called_once_with()
            self.window.zai_usage_refresh_button.clicked()
            self.assertEqual(refresh_zai.call_count, 2)
            refresh_codex.assert_not_called()

    def test_async_request_is_shared_deduplicated_and_does_not_block_gtk(self):
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        entered = threading.Event()
        release = threading.Event()
        heartbeat = []

        def fetch(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("The test did not release its usage worker")
            return SNAPSHOT

        with patch("swarm_app.app.fetch_zai_usage", side_effect=fetch) as request:
            try:
                self.assertTrue(self.app.refresh_zai_usage())
                pump_until(entered.is_set)
                self.assertFalse(self.app.refresh_zai_usage())
                request.assert_called_once()
                GLib.idle_add(lambda: heartbeat.append(True) or GLib.SOURCE_REMOVE)
                pump_until(lambda: bool(heartbeat))
                for window in self.windows:
                    self.assertFalse(window.zai_usage_refresh_button.get_sensitive())
                    self.assertIn("Refreshing", window.zai_usage_updated.get_text())
                self.assertEqual(self.window.zai_usage_button.get_label(), "—%")
            finally:
                release.set()
                pump_until(lambda: not self.app.zai_usage_pending)
        for window in self.windows:
            self.assertEqual(window.zai_usage_button.get_label(), "74%")
            self.assertTrue(window.zai_usage_refresh_button.get_sensitive())
        self.assertIsNotNone(self.app.zai_usage_updated_at)

    def test_codex_signout_and_auth_work_do_not_block_or_clear_zai(self):
        self.app.auth_pending = True
        self.app.logout_pending = True
        with patch("swarm_app.app.fetch_zai_usage", return_value=SNAPSHOT) as request:
            self.assertTrue(self.app.refresh_zai_usage())
            pump_until(lambda: not self.app.zai_usage_pending)
            request.assert_called_once()
        self.app._set_auth_status("Signed out", "signed_out")
        self.assertEqual(self.window.zai_usage_button.get_label(), "74%")
        self.assertEqual(self.window.usage_button.get_label(), "—%")
        self.assertTrue(self.window.zai_usage_refresh_button.get_sensitive())

    def test_failed_refresh_clears_previous_number_and_updated_time(self):
        self.show_snapshot()
        with patch("swarm_app.app.fetch_zai_usage", side_effect=UsageUnavailable("Z.ai usage unavailable")):
            self.assertTrue(self.app.refresh_zai_usage())
            pump_until(lambda: not self.app.zai_usage_pending)
        self.assertIsNone(self.app.zai_usage_snapshot)
        self.assertIsNone(self.app.zai_usage_updated_at)
        self.assertEqual(self.window.zai_usage_button.get_label(), "—%")
        self.assertIn("unavailable", self.window.zai_usage_details.get_text())
        self.assertNotIn("74%", self.window.zai_usage_button.get_tooltip_text())
        self.assertEqual(self.window.zai_usage_updated.get_text(), "")
        self.assertTrue(self.window.zai_usage_refresh_button.get_sensitive())

    def test_unexpected_errors_are_sanitized_and_empty_limits_remain_unknown(self):
        with patch("swarm_app.app.fetch_zai_usage", side_effect=RuntimeError("private credential data")):
            self.assertTrue(self.app.refresh_zai_usage())
            pump_until(lambda: not self.app.zai_usage_pending)
        self.assertEqual(self.window.zai_usage_button.get_label(), "—%")
        self.assertNotIn("private credential data", self.window.zai_usage_details.get_text())
        self.show_snapshot(UsageSnapshot(None, None))
        self.assertEqual(self.window.zai_usage_button.get_label(), "—%")

    def test_invalidating_cancels_request_and_discards_its_late_result(self):
        with patch("swarm_app.app.threading.Thread"):
            self.assertTrue(self.app.refresh_zai_usage())
        old_generation = self.app.zai_usage_generation
        cancel = self.app.zai_usage_cancel
        self.assertFalse(cancel.is_set())
        self.app.invalidate_zai_usage("Credentials changed")
        self.assertTrue(cancel.is_set())
        self.assertFalse(self.app.zai_usage_pending)
        self.app._zai_usage_finished(old_generation, SNAPSHOT, None)
        self.assertIsNone(self.app.zai_usage_snapshot)
        self.assertEqual(self.window.zai_usage_button.get_label(), "—%")
        self.assertIn("Credentials changed", self.window.zai_usage_details.get_text())

    def test_old_callback_cannot_complete_a_newer_pending_request(self):
        with patch("swarm_app.app.threading.Thread"):
            self.assertTrue(self.app.refresh_zai_usage())
            old_generation = self.app.zai_usage_generation
            self.app.invalidate_zai_usage("Credentials changed")
            self.assertTrue(self.app.refresh_zai_usage())
        new_generation = self.app.zai_usage_generation
        new_cancel = self.app.zai_usage_cancel
        self.app._zai_usage_finished(old_generation, SNAPSHOT, None)
        self.assertTrue(self.app.zai_usage_pending)
        self.assertIs(self.app.zai_usage_cancel, new_cancel)
        self.assertIsNone(self.app.zai_usage_snapshot)
        replacement = UsageSnapshot(UsageWindow(56.7, 300, None), None)
        self.app._zai_usage_finished(new_generation, replacement, None)
        self.assertFalse(self.app.zai_usage_pending)
        self.assertEqual(self.window.zai_usage_button.get_label(), "56%")

    def test_request_can_finish_after_its_original_window_closes(self):
        other = self.app.new_window(start_terminal=False)
        self.windows.append(other)
        with patch("swarm_app.app.threading.Thread"):
            self.assertTrue(self.app.refresh_zai_usage())
        generation = self.app.zai_usage_generation
        self.window.destroy()
        self.app._zai_usage_finished(generation, SNAPSHOT, None)
        self.assertEqual(other.zai_usage_button.get_label(), "74%")

    def test_startup_and_periodic_refresh_include_zai(self):
        with patch.object(self.app, "new_window") as new_window, \
                patch.object(self.app, "refresh_auth") as refresh_auth, \
                patch.object(self.app, "refresh_zai_usage") as refresh_zai:
            self.app.do_activate()
            new_window.assert_called_once_with(self.app.directory)
            refresh_auth.assert_called_once_with()
            refresh_zai.assert_called_once_with()
        with patch.object(self.app, "refresh_usage") as refresh_codex, \
                patch.object(self.app, "refresh_zai_usage") as refresh_zai:
            self.assertEqual(self.app._poll_usage(), GLib.SOURCE_CONTINUE)
            refresh_codex.assert_called_once_with()
            refresh_zai.assert_called_once_with()


@unittest.skipUnless(os.environ.get("DISPLAY"), "A display or xvfb-run is required")
class ZaiUsageShutdownTests(unittest.TestCase):
    def test_shutdown_cancels_both_provider_requests_and_removes_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            app = SwarmApplication(directory, linked_agents_path=Path(directory) / "linked-agents.json")
            app.set_application_id("io.swarm.Terminal.ZaiUsageShutdownTests")
            self.assertTrue(app.register(None))
            codex_cancel = threading.Event()
            zai_cancel = threading.Event()
            app.usage_cancel = codex_cancel
            app.zai_usage_cancel = zai_cancel
            app.usage_pending = True
            app.zai_usage_pending = True
            with patch("swarm_app.app.fetch_zai_usage") as fetch:
                shutdown_application(app)
                fetch.assert_not_called()
            self.assertTrue(codex_cancel.is_set())
            self.assertTrue(zai_cancel.is_set())
            self.assertEqual(app.usage_poll, 0)
            self.assertFalse(app.usage_pending)
            self.assertFalse(app.zai_usage_pending)


if __name__ == "__main__":
    unittest.main()
