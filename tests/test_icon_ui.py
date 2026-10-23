"""El icono oficial en GTK4: tema de iconos, ventana y diálogo «Acerca de».

Necesita display (``scripts/check.sh`` usa Broadway aislado). La presentación en el
dock, Alt+Tab y el menú de aplicaciones de GNOME requiere comprobación manual.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
import unittest
from pathlib import Path

from helpers import ROOT, temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


def pump(seconds: float = 0.2) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


@unittest.skipUnless(HAS_DISPLAY, "sin display gráfico")
class IconInGtkTest(unittest.TestCase):
    def test_icon_resolves_for_window_and_about(self) -> None:
        from ia_workspace import APP_ID, APP_NAME
        from ia_workspace.app import IAWorkspaceApplication

        errors: list[str] = []
        with temp_dir() as data:
            os.environ["IAW_DATA_DIR"] = data
            app = IAWorkspaceApplication()
            app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
            previous_hook = sys.excepthook
            sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

            def check(condition: bool, message: str) -> None:
                if not condition:
                    errors.append(message)

            def scenario() -> bool:
                try:
                    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                    win = app._window
                    pump()
                    check(app.get_application_id() == APP_ID, "id de la aplicación inesperado")
                    check(Gtk.Window.get_default_icon_name() == APP_ID,
                          f"icono predeterminado: {Gtk.Window.get_default_icon_name()!r}")

                    theme = Gtk.IconTheme.get_for_display(win.get_display())
                    check(theme.has_icon(APP_ID), "el tema de iconos no encuentra el icono oficial")
                    for size in (16, 32, 48, 128, 256):
                        paintable = theme.lookup_icon(APP_ID, None, size, 1, Gtk.TextDirection.NONE, 0)
                        file = paintable.get_file()
                        path = Path(file.get_path()) if file is not None and file.get_path() else None
                        # Puede resolverse al del proyecto o, si el usuario instaló el lanzador, a su
                        # copia en ~/.local/share/icons (que tiene prioridad): debe ser el mismo icono.
                        project_icon = ROOT / "data" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
                        check(path is not None and path.name == f"{APP_ID}.png", f"{size}px: archivo {path}")
                        check(path is not None and path.is_file() and path.read_bytes() == project_icon.read_bytes(),
                              f"{size}px resolvió a {path}, que no es el icono oficial")

                    app.activate_action("about", None)
                    pump(0.3)
                    about = win.get_visible_dialog()
                    check(about is not None, "no se abrió «Acerca de»")
                    if about is not None:
                        check(about.get_application_icon() == APP_ID,
                              f"«Acerca de» usa {about.get_application_icon()!r}")
                        check(about.get_application_name() == APP_NAME, "nombre de la aplicación inesperado")
                        about.force_close()
                except Exception:  # noqa: BLE001
                    errors.append(traceback.format_exc())
                app.quit()
                return GLib.SOURCE_REMOVE

            app.connect("activate", lambda *_: GLib.idle_add(scenario))
            try:
                app.run([])
            finally:
                sys.excepthook = previous_hook
                os.environ.pop("IAW_DATA_DIR", None)
        self.assertEqual(errors, [], "\n".join(errors))

    def test_restore_icon_resolves_for_assistant(self) -> None:
        from ia_workspace import APP_RESTORE_ID
        from ia_workspace.config import AppPaths
        from ia_workspace.ui.dialogs.restore_assistant import RestoreAssistantApplication

        errors: list[str] = []
        with temp_dir() as data:
            paths = AppPaths(data_dir=Path(data))
            app = RestoreAssistantApplication(paths=paths)
            previous_hook = sys.excepthook
            sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

            def scenario() -> bool:
                try:
                    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                    win = app.window
                    pump()
                    if win is None:
                        errors.append("la ventana del asistente no fue creada")
                        app.quit()
                        return GLib.SOURCE_REMOVE

                    if app.get_application_id() != APP_RESTORE_ID:
                        errors.append(f"id inesperado: {app.get_application_id()!r}")
                    if Gtk.Window.get_default_icon_name() != APP_RESTORE_ID:
                        errors.append(f"icono predeterminado inesperado: {Gtk.Window.get_default_icon_name()!r}")

                    theme = Gtk.IconTheme.get_for_display(win.get_display())
                    if not theme.has_icon(APP_RESTORE_ID):
                        errors.append("el tema de iconos no encuentra el icono del asistente")

                    for size in (16, 32, 48, 128, 256):
                        paintable = theme.lookup_icon(APP_RESTORE_ID, None, size, 1, Gtk.TextDirection.NONE, 0)
                        file = paintable.get_file()
                        path = Path(file.get_path()) if file is not None and file.get_path() else None
                        project_icon = ROOT / "data" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_RESTORE_ID}.png"
                        if path is None or not path.is_file():
                            errors.append(f"{size}px: archivo de icono no encontrado: {path}")
                        elif path.read_bytes() != project_icon.read_bytes():
                            errors.append(f"{size}px resolvió a {path}, que no coincide con {project_icon}")
                except Exception:  # noqa: BLE001
                    errors.append(traceback.format_exc())
                app.quit()
                return GLib.SOURCE_REMOVE

            app.connect("activate", lambda *_: GLib.idle_add(scenario))
            try:
                app.run([])
            finally:
                sys.excepthook = previous_hook
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
