"""Clipboard image attachment controls shared by broadcast dialogs."""

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Gtk


def save_broadcast_image(image):
    """Keep a private, immutable PNG for CLI queues and conversation history.

    A successful terminal write is not an acknowledgement that an agent has
    read the file. Keep attachments in the user cache rather than deleting
    them on submission or relying on the mutable system clipboard.
    """
    directory = Path(GLib.get_user_cache_dir()) / "swarm" / "broadcast-images"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix="broadcast-", suffix=".png", dir=directory)
    os.close(descriptor)
    try:
        if not image.savev(filename, "png", [], []):
            raise OSError("Could not save the attached image.")
    except Exception:
        Path(filename).unlink(missing_ok=True)
        raise
    return filename


def save_broadcast_images(images):
    """Save every attachment, removing this batch if any image fails to save."""
    filenames = []
    try:
        for image in images:
            filenames.append(save_broadcast_image(image))
    except Exception:
        for filename in filenames:
            try:
                Path(filename).unlink(missing_ok=True)
            except OSError:
                # Keep the original save error if cleanup also fails.
                pass
        raise
    return tuple(filenames)


@dataclass
class _PasteRequest:
    editor: object
    complete: bool = False
    image: object = None
    error: str | None = None


class _ImagePreview(Gtk.Box):
    def __init__(self, image, remove):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        width, height = image.get_width(), image.get_height()
        scale = min(1.0, 96 / width, 64 / height)
        thumbnail = image.scale_simple(max(1, round(width * scale)), max(1, round(height * scale)),
                                       GdkPixbuf.InterpType.BILINEAR)
        preview = Gtk.Image.new_from_pixbuf(thumbnail)
        preview.set_size_request(96, 64)
        preview.set_tooltip_text(f"{width} × {height}")
        self.pack_start(preview, False, False, 0)
        footer = Gtk.Box(spacing=4)
        self.label = Gtk.Label()
        footer.pack_start(self.label, True, True, 0)
        self.remove_button = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        self.remove_button.set_relief(Gtk.ReliefStyle.NONE)
        self.remove_button.connect("clicked", lambda *_: remove(image))
        footer.pack_end(self.remove_button, False, False, 0)
        self.pack_start(footer, False, False, 0)


class ClipboardImageAttachment(Gtk.Box):
    def __init__(self, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._on_change = on_change
        self._request_id = 0
        self._generation = 0
        self._requests = {}
        self._disposed = False
        self._text_paste = False
        self._notice = None
        self.images = []
        self.previews = []

        toolbar = Gtk.Box(spacing=8)
        self.attach_button = Gtk.Button(label="Attach Clipboard Image")
        self.attach_button.set_valign(Gtk.Align.CENTER)
        self.attach_button.connect("clicked", self._attach_clicked)
        toolbar.pack_start(self.attach_button, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_hexpand(True)
        self.status.get_style_context().add_class("muted")
        toolbar.pack_start(self.status, True, True, 0)

        self.clear_button = Gtk.Button(label="Clear all")
        self.clear_button.set_valign(Gtk.Align.CENTER)
        self.clear_button.set_no_show_all(True)
        self.clear_button.connect("clicked", self._clear)
        toolbar.pack_end(self.clear_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        self.preview_strip = Gtk.Box(spacing=12)
        self.preview_strip.set_halign(Gtk.Align.START)
        self.preview_scroll = Gtk.ScrolledWindow()
        self.preview_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.preview_scroll.set_overlay_scrolling(False)
        self.preview_scroll.set_min_content_height(110)
        self.preview_scroll.set_no_show_all(True)
        self.preview_scroll.add(self.preview_strip)
        self.pack_start(self.preview_scroll, False, False, 0)
        self.delivery_note = Gtk.Label(xalign=0)
        self.delivery_note.set_line_wrap(True)
        self.delivery_note.set_no_show_all(True)
        self.delivery_note.get_style_context().add_class("muted")
        self.pack_start(self.delivery_note, False, False, 0)
        self.connect("destroy", self._destroyed)
        self._show_status()

    @property
    def pending(self):
        return bool(self._requests)

    def handle_paste_signal(self, widget):
        if self._text_paste:
            return
        GObject.signal_stop_emission_by_name(widget, "paste-clipboard")
        self.paste_from_clipboard(widget)

    def paste_from_clipboard(self, editor=None):
        if self._disposed:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self._request_id += 1
        request_id = self._request_id
        self._requests[request_id] = _PasteRequest(editor)
        self._notice = None
        self._show_status()
        self._on_change()
        request = (self._generation, request_id)
        try:
            clipboard.request_image(self._image_received, request)
        except (GLib.Error, RuntimeError):
            self._complete_request(request, error="Could not read the clipboard image")

    def _attach_clicked(self, *_args):
        self.paste_from_clipboard()

    def _image_received(self, _clipboard, pixbuf, request):
        # Copy while handling the callback so a later clipboard change cannot
        # mutate an image waiting for an earlier paste request to complete.
        generation, request_id = request
        if self._disposed or generation != self._generation or request_id not in self._requests:
            return
        self._complete_request(request, image=pixbuf.copy() if pixbuf is not None else None)

    def _complete_request(self, request, *, image=None, error=None):
        generation, request_id = request
        if self._disposed or generation != self._generation or request_id not in self._requests:
            return
        pending = self._requests[request_id]
        if pending.complete:
            return
        pending.complete = True
        pending.image = image
        pending.error = error
        # Dict insertion order is paste order, regardless of callback order.
        while self._requests:
            first_id = next(iter(self._requests))
            first = self._requests[first_id]
            if not first.complete:
                break
            del self._requests[first_id]
            if first.error:
                self._notice = first.error
            elif first.image is not None:
                self._append_image(first.image)
                self._notice = None
            elif first.editor is not None:
                # Let GTK handle normal text, selection replacement, and undo.
                self._text_paste = True
                try:
                    first.editor.emit("paste-clipboard")
                finally:
                    self._text_paste = False
                self._notice = None
            else:
                self._notice = "No image found on the clipboard"
        self._show_status()
        self._on_change()

    def _append_image(self, image):
        self.images.append(image)
        preview = _ImagePreview(image, self._remove_image)
        self.previews.append(preview)
        self.preview_strip.pack_start(preview, False, False, 0)
        self.preview_strip.show_all()
        self.preview_scroll.get_child().show()
        self.preview_scroll.show()
        self._renumber_previews()

    def _remove_image(self, image):
        for index, attached in enumerate(self.images):
            if attached is image:
                del self.images[index]
                self.previews.pop(index).destroy()
                break
        self._notice = None
        self._renumber_previews()
        self._show_status()
        self._on_change()

    def _renumber_previews(self):
        for index, preview in enumerate(self.previews, 1):
            preview.label.set_text(f"Image {index}")
            preview.remove_button.set_tooltip_text(f"Remove image {index}")
            preview.remove_button.get_accessible().set_name(f"Remove image {index}")

    def _show_status(self):
        count = len(self.images)
        if count:
            status = f"{count} image{'s' if count != 1 else ''} attached."
        else:
            status = "Ctrl+V with an image attaches it."
        if self.pending:
            status += f" Reading clipboard ({len(self._requests)} pending)…"
        elif self._notice:
            status += " " + self._notice
        self.status.set_text(status)
        self.delivery_note.set_text(
            "Codex: all image attachments. Hermes: first image + local file references. Other harnesses: text only."
            if count > 1 else "Codex and Hermes: image attachments. Other harnesses: text only.")
        self.delivery_note.set_visible(bool(count))
        self.clear_button.set_visible(bool(count or self.pending))
        self.preview_scroll.set_visible(bool(count))

    def _clear(self, *_args):
        self._generation += 1
        self._requests.clear()
        self.images.clear()
        for preview in self.previews:
            preview.destroy()
        self.previews.clear()
        self._notice = None
        self._show_status()
        self._on_change()

    def _destroyed(self, *_args):
        self._disposed = True
        self._generation += 1
        self._requests.clear()
