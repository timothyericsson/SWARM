"""Notification delivery checks with the desktop bus entirely mocked."""

import unittest
from unittest.mock import Mock, patch

from swarm_app.notifications import Gio, GLib, notify_agents_finished


class NotificationTests(unittest.TestCase):
    def setUp(self):
        get_bus = patch("swarm_app.notifications.Gio.bus_get")
        self.get_bus = get_bus.start()
        self.addCleanup(get_bus.stop)
        finish_bus = patch("swarm_app.notifications.Gio.bus_get_finish")
        self.finish_bus = finish_bus.start()
        self.addCleanup(finish_bus.stop)
        self.connection = self.finish_bus.return_value
        self.on_error = Mock()

    def connect(self):
        callback = self.get_bus.call_args.args[2]
        result = object()
        callback(None, result, None)
        self.finish_bus.assert_called_once_with(result)

    def test_delivery_is_async_with_expected_protocol_and_no_private_text(self):
        notify_agents_finished(4, self.on_error)
        self.assertEqual(self.get_bus.call_args.args[:2], (Gio.BusType.SESSION, None))
        self.finish_bus.assert_not_called()
        self.connect()
        args = self.connection.call.call_args.args
        self.assertEqual(args[:4], (
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
        ))
        self.assertEqual(args[4].get_type_string(), "(susssasa{sv}i)")
        self.assertEqual(args[4].unpack(), (
            "SWARM", 0, "swarm-terminal", "Agents finished",
            "All 4 agents from your broadcast are ready.", [],
            {"desktop-entry": "swarm-terminal"}, -1,
        ))
        self.assertEqual(args[5].dup_string(), "(u)")
        self.assertEqual(args[6:9], (Gio.DBusCallFlags.NONE, 5000, None))
        result = object()
        args[9](self.connection, result, None)
        self.connection.call_finish.assert_called_once_with(result)
        self.on_error.assert_not_called()

    def test_singular_message(self):
        notify_agents_finished(1)
        self.connect()
        body = self.connection.call.call_args.args[4].unpack()[4]
        self.assertEqual(body, "The agent from your broadcast is ready.")

    def test_missing_session_bus_reports_error(self):
        error = GLib.Error("No session bus")
        self.finish_bus.side_effect = error
        notify_agents_finished(2, self.on_error)
        self.connect()
        self.on_error.assert_called_once_with(error)
        self.connection.call.assert_not_called()

    def test_service_failure_reports_error(self):
        error = GLib.Error("No notification service")
        self.connection.call_finish.side_effect = error
        notify_agents_finished(2, self.on_error)
        self.connect()
        self.connection.call.call_args.args[9](self.connection, object(), None)
        self.on_error.assert_called_once_with(error)

    def test_immediate_bus_error_is_safe_without_error_callback(self):
        self.get_bus.side_effect = GLib.Error("No session bus")
        notify_agents_finished(2)

    def test_error_callback_failure_does_not_escape(self):
        self.get_bus.side_effect = GLib.Error("No session bus")
        self.on_error.side_effect = RuntimeError("Window already closed")
        notify_agents_finished(2, self.on_error)
        self.on_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
