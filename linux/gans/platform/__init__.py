"""Desktop integration: session detection, X11 (focus, XTest typing, key grabs), the
clipboard, code delivery, the global hotkey backends, the polkit app lock, autostart,
and the honk. Only the modules that must talk to GTK/GLib import ``gi``, and always
lazily enough to stay constructible without a display."""
