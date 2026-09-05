"""GTK 3 user interface: the ``Gtk.Application`` that wires everything, the tray item,
the Spotlight-style Quick Search, and the Login/Settings windows.

This is the only package that imports GTK at module level. Everything it renders comes
from the plain-Python layers below (``gans.ente``, ``gans.prefs``, ``gans.search``), and
anything that blocks — network, Argon2id — runs on a worker thread and marshals back
to the main loop through ``GLib.idle_add``.
"""
