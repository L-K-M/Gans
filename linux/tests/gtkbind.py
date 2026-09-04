"""One process-wide Xvfb session for every GTK test module.

GTK initialises once per process, against whatever ``DISPLAY`` was set when ``Gtk`` was
first imported, and a ``GdkDisplay`` can't be retired cleanly afterwards: closing it only
detaches its event source — the real ``XCloseDisplay`` runs when the last reference is
finalised, which with unittest keeping every TestCase (and its widget wrappers) alive
happens at interpreter exit, after the module's Xvfb is gone, and Xlib's I/O error
handler then ``_exit(1)``s the whole run. Leaving a display *connected* to a dead server
is fatal too: GDK's next main-loop iteration hits the broken socket.

So GTK tests share a single Xvfb (plus session bus) that lives until the process exits:

    class MyTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.session = gtk_session()   # never call session.stop() yourself

Modules that create GTK objects must not start a ``DisplaySession`` of their own.

``present_now`` focuses a window reliably: ``Gtk.Window.present()`` stamps its focus
request with GDK's last user-interaction time, and once anything has moved the X focus
with a newer timestamp (``X11Session.activate_window`` does), the server ignores it.
"""

from __future__ import annotations

import atexit
import os
import unittest
from typing import Optional

from tests.harness import DisplaySession

_shared: Optional[DisplaySession] = None


def gtk_session() -> DisplaySession:
    """The Xvfb session GDK is bound to — started (and GDK bound to it) on first use,
    stopped at interpreter exit."""
    global _shared
    if _shared is None:
        session = DisplaySession.start()
        atexit.register(session.stop)
        _bind_gdk(session)
        _shared = session
    return _shared


def _bind_gdk(session: DisplaySession) -> None:
    """Makes ``session``'s display GDK's default, initialising GTK if it was imported
    without a display."""
    # The simple IM context types synthesized keys straight into entries; an input-method
    # daemon (ibus) isn't running under Xvfb and would swallow them.
    os.environ["GTK_IM_MODULE"] = "gtk-im-context-simple"
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

    current = Gdk.Display.get_default()
    if current is None:
        # Gtk was imported before any display existed (gtk_available() at discovery time).
        # PyGObject's Gtk.Window override checks a module-level flag frozen at import, so
        # a later successful init has to be recorded there too.
        import gi.overrides.Gtk as gtk_override
        outcome = Gtk.init_check([])
        if not (outcome[0] if isinstance(outcome, tuple) else outcome):
            raise unittest.SkipTest(f"GTK couldn't initialise on {session.display_name}")
        gtk_override.initialized = True
        current = Gdk.Display.get_default()
    if current is not None and not current.is_closed() and current.get_name() == session.display_name:
        return
    opened = Gdk.Display.open(session.display_name)
    if opened is None:
        raise unittest.SkipTest(f"GDK couldn't open {session.display_name}")
    Gdk.DisplayManager.get().set_default_display(opened)


def present_now(window) -> None:
    """Shows ``window`` and focuses it with the X server's current time, so the request
    is honoured whatever focus changes happened since GDK last saw a key or button."""
    import gi
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11

    window.show_all()
    window.present_with_time(GdkX11.x11_get_server_time(window.get_window()))
