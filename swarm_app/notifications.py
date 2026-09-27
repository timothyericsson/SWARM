"""Desktop completion notifications through the session notification service."""

from gi.repository import Gio, GLib


def notify_agents_finished(count, on_error=None):
    """Send asynchronously; report delivery errors to ``on_error(error)`` if set.

    Call from the GTK main thread. The desktop controls display and expiration;
    no notification action or private broadcast text is included.
    """
    body = ("The agent from your broadcast is ready." if count == 1 else
            f"All {count} agents from your broadcast are ready.")

    def failed(error):
        if on_error is not None:
            try:
                on_error(error)
            except Exception:
                # Notification failures must not interrupt the GTK main loop.
                pass

    def delivered(connection, result, _data):
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            failed(error)

    def connected(_source, result, _data):
        try:
            connection = Gio.bus_get_finish(result)
            connection.call(
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications",
                "Notify",
                GLib.Variant("(susssasa{sv}i)", (
                    "SWARM", 0, "swarm-terminal", "Agents finished", body, [],
                    {"desktop-entry": GLib.Variant("s", "swarm-terminal")}, -1,
                )),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
                delivered,
                None,
            )
        except GLib.Error as error:
            failed(error)

    try:
        Gio.bus_get(Gio.BusType.SESSION, None, connected, None)
    except GLib.Error as error:
        failed(error)
