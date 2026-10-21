"""Punto de entrada de IA Workspace."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


def main(argv: list[str] | None = None) -> int:
    from gi.repository import GLib

    from ia_workspace import APP_ID
    from ia_workspace.app import IAWorkspaceApplication

    # En X11 el WM_CLASS sale del prgname (por defecto «python3»): se iguala al
    # identificador para que el lanzador agrupe la ventana. En Wayland GTK ya usa el
    # id de la aplicación como app_id.
    GLib.set_prgname(APP_ID)

    app = IAWorkspaceApplication()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
