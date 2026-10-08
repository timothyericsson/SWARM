"""Image recipient selection and paste integration for every broadcast dialog."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from swarm_app.app import BroadcastDialog, Gdk, Gtk, SwarmWindow
from swarm_app.custom_broadcast import CustomBroadcastDialog
from gi.repository import GdkPixbuf
from test_clipboard_image import solid_image
from test_session import GTK_AVAILABLE, pump_until


class Target:
    def __init__(self, harness):
        self.harness = harness
        self.supports_image_broadcast = harness in {"codex", "hermes"}
        self.agent_identity = (id(self), 1, 1)
        self.broadcast_identity = self.agent_identity
        self.title = harness
        self.directory = "/tmp"
        self.state = "running"
        self.agent_busy = False
        self.agent_idle = harness == "codex"
        self.activity = False if self.agent_idle else None
        self.deliveries = []

    def broadcast(self, message, callback, **options):
        self.deliveries.append((message, options))
        callback(True)
        return True


class Owner(Gtk.Window):
    broadcast = SwarmWindow.broadcast

    def __init__(self):
        super().__init__()
        self.targets = [Target(name) for name in ("codex", "hermes", "custom")]
        self.custom_selection = {target: target.broadcast_identity for target in self.targets}
        self.broadcast_watches = []
        self.broadcast_dialog = None
        self.custom_broadcast_dialog = None
        self.messages = []

    def flash(self, message):
        self.messages.append(message)

    def active_agents(self):
        return self.targets

    def custom_agents(self):
        return self.targets

    def ready_agents(self):
        return [target for target in self.targets if target.agent_idle]

    def _check_broadcast_notifications(self):
        pass


@unittest.skipUnless(GTK_AVAILABLE, "A display or xvfb-run is required")
class ImageBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        self.temp = tempfile.TemporaryDirectory()
        self.cache = patch("swarm_app.clipboard_image.GLib.get_user_cache_dir",
                           return_value=self.temp.name)
        self.cache.start()
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def tearDown(self):
        self.owner.destroy()
        self.clipboard.clear()
        self.cache.stop()
        self.temp.cleanup()

    def test_global_sleeper_and_custom_dialogs_send_multiple_pasted_images(self):
        for mode in ("global", "sleeper", "custom"):
            for message in ("", "compare these images"):
                with self.subTest(mode=mode, message=message):
                    for target in self.owner.targets:
                        target.deliveries.clear()
                    dialog = (CustomBroadcastDialog(self.owner) if mode == "custom" else
                              BroadcastDialog(self.owner, idle_only=mode == "sleeper"))
                    dialog.editor.get_buffer().set_text(message)
                    for index, color in enumerate((0x00FF00FF, 0xFF0000FF), 1):
                        self.clipboard.set_image(solid_image(color))
                        dialog.editor.emit("paste-clipboard")
                        pump_until(lambda: len(dialog.image_attachment.images) == index)
                    self.assertTrue(dialog.send_button.get_sensitive())
                    self.assertEqual(dialog.text(), message)
                    if not message:
                        expected_count = 1 if mode == "sleeper" else 2
                        self.assertEqual(dialog.send_button.get_label(),
                                         f"Send 2 images to {expected_count} agent{'s' if expected_count != 1 else ''}")
                    dialog.response(Gtk.ResponseType.OK)
                    codex, hermes, custom = self.owner.targets
                    paths = codex.deliveries[-1][1]["images"]
                    self.assertEqual(len(paths), 2)
                    self.assertEqual(codex.deliveries[-1][0], message)
                    self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(paths[0]).get_pixels(),
                                     solid_image(0x00FF00FF).get_pixels())
                    self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(paths[1]).get_pixels(),
                                     solid_image(0xFF0000FF).get_pixels())
                    if mode != "sleeper":
                        self.assertEqual(hermes.deliveries[-1][1]["images"], paths)
                        if message:
                            self.assertEqual(custom.deliveries[-1][0], message)
                            self.assertEqual(custom.deliveries[-1][1]["images"], ())
                        else:
                            self.assertEqual(custom.deliveries, [])
                    else:
                        self.assertEqual(hermes.deliveries, [])
                        self.assertEqual(custom.deliveries, [])

    def test_mixed_harness_broadcast_saves_once_and_keeps_clipboard(self):
        self.clipboard.set_text("keep this clipboard text", -1)
        self.assertTrue(self.owner.broadcast("look at this", images=(solid_image(0x00FF00FF),
                                                                    solid_image(0xFF0000FF))))
        codex, hermes, custom = self.owner.targets
        paths = codex.deliveries[0][1]["images"]
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertEqual(hermes.deliveries[0][1]["images"], paths)
        self.assertEqual(custom.deliveries[0][1]["images"], ())
        self.assertEqual(len(list(Path(self.temp.name).rglob("*.png"))), 2)
        self.assertEqual(self.clipboard.wait_for_text(), "keep this clipboard text")
        self.assertIn("1 received text only", self.owner.messages[-1])

    def test_image_only_custom_selection_excludes_unknown_harness(self):
        self.assertTrue(self.owner.broadcast("", recipients=self.owner.custom_selection,
                                             images=(solid_image(0x00FF00FF), solid_image(0xFF0000FF))))
        self.assertEqual([len(target.deliveries) for target in self.owner.targets], [1, 1, 0])
        self.assertEqual(self.owner.messages[-1], "2 images submitted to 2 agents")

    def test_image_save_failure_preserves_draft_and_sends_nothing(self):
        with patch("swarm_app.app.save_broadcast_images", side_effect=OSError("disk full")):
            self.assertFalse(self.owner.broadcast("draft", images=(solid_image(0x00FF00FF),)))
        self.assertTrue(all(not target.deliveries for target in self.owner.targets))
        self.assertIn("Could not save", self.owner.messages[-1])

    def test_removing_an_image_keeps_remaining_order_and_clear_disables_empty_send(self):
        dialog = BroadcastDialog(self.owner)
        for index, color in enumerate((0x00FF00FF, 0xFF0000FF, 0x0000FFFF), 1):
            self.clipboard.set_image(solid_image(color))
            dialog.editor.emit("paste-clipboard")
            pump_until(lambda: len(dialog.image_attachment.images) == index)
        first, _, third = dialog.image_attachment.images
        dialog.image_attachment.previews[1].remove_button.clicked()
        self.assertEqual(dialog.image_attachment.images, [first, third])
        self.assertEqual(dialog.send_button.get_label(), "Send 2 images to 2 agents")
        dialog.image_attachment.clear_button.clicked()
        self.assertFalse(dialog.send_button.get_sensitive())
        dialog.destroy()

    def test_send_waits_for_all_pending_pastes_and_failed_send_retains_full_draft(self):
        dialog = BroadcastDialog(self.owner)
        dialog.editor.get_buffer().set_text("draft")
        callbacks = []
        class DeferredClipboard:
            def request_image(self, callback, request):
                callbacks.append((callback, request))
        with patch("swarm_app.clipboard_image.Gtk.Clipboard.get", return_value=DeferredClipboard()):
            dialog.editor.emit("paste-clipboard")
            dialog.editor.emit("paste-clipboard")
        self.assertFalse(dialog.send_button.get_sensitive())
        callbacks[1][0](self.clipboard, solid_image(0xFF0000FF), callbacks[1][1])
        self.assertFalse(dialog.send_button.get_sensitive())
        callbacks[0][0](self.clipboard, solid_image(0x00FF00FF), callbacks[0][1])
        self.assertTrue(dialog.send_button.get_sensitive())
        with patch("swarm_app.app.save_broadcast_images", side_effect=OSError("disk full")):
            dialog.response(Gtk.ResponseType.OK)
        self.assertTrue(dialog.get_visible())
        self.assertEqual(dialog.text(), "draft")
        self.assertEqual(len(dialog.image_attachment.images), 2)
        self.assertTrue(all(not target.deliveries for target in self.owner.targets))
        dialog.destroy()


if __name__ == "__main__":
    unittest.main()
