"""Clipboard paste ordering and attachment persistence checks under Xvfb."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gtk

from swarm_app.clipboard_image import ClipboardImageAttachment, save_broadcast_image, save_broadcast_images
from test_session import GTK_AVAILABLE, pump_until


def solid_image(color):
    image = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 2, 2)
    image.fill(color)
    return image


@unittest.skipUnless(GTK_AVAILABLE, "A display or xvfb-run is required")
class ClipboardImageTests(unittest.TestCase):
    def setUp(self):
        self.changed = Mock()
        self.widget = ClipboardImageAttachment(self.changed)
        self.editor = Gtk.TextView()
        self.editor.connect("paste-clipboard", self.widget.handle_paste_signal)
        self.window = Gtk.Window()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(self.editor, True, True, 0)
        box.pack_start(self.widget, False, False, 0)
        self.window.add(box)
        self.window.show_all()
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def tearDown(self):
        self.widget.destroy()
        self.editor.destroy()
        self.window.destroy()
        self.clipboard.clear()

    def text(self):
        buffer = self.editor.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def queued_pastes(self, count):
        callbacks = []
        clipboard = SimpleNamespace(request_image=lambda callback, data: callbacks.append((callback, data)))
        with patch("swarm_app.clipboard_image.Gtk.Clipboard.get", return_value=clipboard):
            for _ in range(count):
                self.widget.paste_from_clipboard()
        return [lambda image, cb=callback, data=data: cb(clipboard, image, data)
                for callback, data in callbacks]

    def assert_image_colors(self, expected):
        self.assertEqual([image.get_pixels() for image in self.widget.images],
                         [solid_image(color).get_pixels() for color in expected])

    def test_repeated_image_paste_appends_and_text_paste_replaces_selection_without_losing_images(self):
        self.clipboard.set_image(solid_image(0xFF0000FF))
        self.editor.emit("paste-clipboard")
        pump_until(lambda: len(self.widget.images) == 1)
        self.clipboard.set_image(solid_image(0x0000FFFF))
        self.widget.attach_button.clicked()
        pump_until(lambda: len(self.widget.images) == 2)
        self.assert_image_colors([0xFF0000FF, 0x0000FFFF])
        self.assertEqual(self.text(), "")
        self.assertFalse(self.widget.pending)
        self.assertIn("2 images attached", self.widget.status.get_text())
        attached = tuple(self.widget.images)
        buffer = self.editor.get_buffer()
        buffer.set_text("replace me")
        buffer.select_range(buffer.get_start_iter(), buffer.get_end_iter())
        self.clipboard.set_text("normal text", -1)
        self.editor.emit("paste-clipboard")
        pump_until(lambda: self.text() == "normal text")
        self.assertEqual(tuple(self.widget.images), attached)

    def test_out_of_order_callbacks_preserve_paste_order_and_pending_until_all_are_finished(self):
        first, second, third = self.queued_pastes(3)
        self.assertTrue(self.widget.pending)
        second(solid_image(0x0000FFFF))
        self.assertEqual(self.widget.images, [])
        self.assertTrue(self.widget.pending)
        first(solid_image(0xFF0000FF))
        self.assert_image_colors([0xFF0000FF, 0x0000FFFF])
        self.assertTrue(self.widget.pending)
        third(solid_image(0x00FF00FF))
        self.assert_image_colors([0xFF0000FF, 0x0000FFFF, 0x00FF00FF])
        self.assertFalse(self.widget.pending)
        self.assertEqual(len(self.widget.previews), 3)

    def test_individual_remove_retains_other_images_and_renumbers_previews(self):
        callbacks = self.queued_pastes(3)
        for callback, color in zip(callbacks, (0xFF0000FF, 0x0000FFFF, 0x00FF00FF)):
            callback(solid_image(color))
        self.widget.previews[1].remove_button.clicked()
        self.assert_image_colors([0xFF0000FF, 0x00FF00FF])
        self.assertEqual([preview.label.get_text() for preview in self.widget.previews], ["Image 1", "Image 2"])
        self.assertTrue(self.widget.preview_scroll.get_visible())
        self.widget.clear_button.clicked()
        self.assertEqual(self.widget.images, [])
        self.assertEqual(self.widget.previews, [])
        self.assertFalse(self.widget.preview_scroll.get_visible())
        self.assertFalse(self.widget.clear_button.get_visible())

    def test_clear_and_destroy_ignore_late_callbacks_but_new_pastes_after_clear_work(self):
        first, second = self.queued_pastes(2)
        self.widget._clear()
        self.assertFalse(self.widget.pending)
        first(solid_image(0xFF0000FF))
        third, = self.queued_pastes(1)
        second(solid_image(0x0000FFFF))
        self.assertEqual(self.widget.images, [])
        self.assertTrue(self.widget.pending)
        third(solid_image(0x00FF00FF))
        self.assert_image_colors([0x00FF00FF])
        late, = self.queued_pastes(1)
        self.widget.destroy()
        self.assertFalse(self.widget.pending)
        self.changed.reset_mock()
        late(solid_image(0xFF0000FF))
        self.assert_image_colors([0x00FF00FF])
        self.changed.assert_not_called()

    def test_clipboard_error_and_empty_result_do_not_discard_images_or_block_later_pastes(self):
        first, second, third = self.queued_pastes(3)
        second(None)
        first(solid_image(0xFF0000FF))
        self.assertTrue(self.widget.pending)
        third(solid_image(0x00FF00FF))
        self.assertFalse(self.widget.pending)
        self.assert_image_colors([0xFF0000FF, 0x00FF00FF])
        clipboard = SimpleNamespace(request_image=Mock(side_effect=RuntimeError("unavailable")))
        with patch("swarm_app.clipboard_image.Gtk.Clipboard.get", return_value=clipboard):
            self.widget.paste_from_clipboard()
        self.assertFalse(self.widget.pending)
        self.assert_image_colors([0xFF0000FF, 0x00FF00FF])
        self.assertIn("Could not read", self.widget.status.get_text())

    def test_pending_images_are_copied_before_waiting_for_earlier_pastes(self):
        first, second = self.queued_pastes(2)
        source = solid_image(0x0000FFFF)
        second(source)
        source.fill(0x00FF00FF)
        first(solid_image(0xFF0000FF))
        self.assert_image_colors([0xFF0000FF, 0x0000FFFF])

    def test_saved_images_are_private_distinct_and_independent_of_clipboard(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("swarm_app.clipboard_image.GLib.get_user_cache_dir", return_value=folder):
            first, second = save_broadcast_images([solid_image(0xFF0000FF), solid_image(0x0000FFFF)])
            original = Path(first).read_bytes()
            self.clipboard.set_text("copied something else", -1)
            self.assertNotEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), original)
            self.assertNotEqual(original, Path(second).read_bytes())
            self.assertEqual(Path(first).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(first).parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(first).get_pixels(), solid_image(0xFF0000FF).get_pixels())
            self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(second).get_pixels(), solid_image(0x0000FFFF).get_pixels())

    def test_failed_batch_save_removes_earlier_images_but_preserves_other_batches(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("swarm_app.clipboard_image.GLib.get_user_cache_dir", return_value=folder):
            previous = save_broadcast_image(solid_image(0x00FF00FF))
            class BrokenImage:
                def savev(self, *_args):
                    raise OSError("disk full")
            with self.assertRaisesRegex(OSError, "disk full"):
                save_broadcast_images([solid_image(0xFF0000FF), solid_image(0x0000FFFF), BrokenImage()])
            self.assertEqual(list(Path(folder).rglob("*.png")), [Path(previous)])
            self.assertEqual(save_broadcast_images([]), ())

    def test_failed_save_removes_incomplete_file(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch("swarm_app.clipboard_image.GLib.get_user_cache_dir", return_value=folder):
            class BrokenImage:
                def savev(self, *_args):
                    raise OSError("disk full")
            with self.assertRaises(OSError):
                save_broadcast_image(BrokenImage())
            self.assertEqual(list(Path(folder).rglob("*.png")), [])


if __name__ == "__main__":
    unittest.main()
