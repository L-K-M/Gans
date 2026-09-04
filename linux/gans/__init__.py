"""Gans for Linux — a tray-resident, end-to-end-encrypted client for Ente Auth.

The package below ``gans.ui`` never imports GTK, so the crypto, protocol, and model
layers stay unit-testable on a headless box. See ``linux/PLAN.md`` for the design.
"""

from .version import app_version

__version__ = app_version()

__all__ = ["__version__", "app_version"]
