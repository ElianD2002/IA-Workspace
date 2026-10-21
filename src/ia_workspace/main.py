"""Punto de entrada de IA Workspace."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


def main(argv: list[str] | None = None) -> int:
    from ia_workspace.app import IAWorkspaceApplication

    app = IAWorkspaceApplication()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
