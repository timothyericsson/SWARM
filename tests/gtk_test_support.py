"""Lifecycle helpers for GTK applications registered without a main run loop."""

from unittest.mock import patch


def shutdown_application(app):
    """Run shutdown so GTK releases its D-Bus exports and application timers."""
    # quit() alone cannot unregister an application that never entered run().
    # Exercise the normal lifecycle, suppressing real sessions and auth work.
    handler = app.connect("activate", lambda application: application.quit())
    try:
        with patch.object(app, "new_window"), patch.object(app, "refresh_auth"):
            result = app.run([])
        if result != 0 or app.get_is_registered():
            raise AssertionError("The test application did not shut down cleanly")
    finally:
        app.disconnect(handler)
