"""Aplicación GTK: ciclo de vida, acciones globales y cierre controlado.

Las sesiones de agentes pertenecen a la aplicación (``AppContext.sessions``), no a
la ventana. Minimizar u ocultar la ventana no las afecta. Al salir por completo
(cerrar la ventana, Ctrl+Q o una señal del sistema) con sesiones activas, se pide
confirmación y se finalizan de forma ordenada antes de terminar el proceso.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ia_workspace import APP_ID, APP_NAME, VERSION
from ia_workspace.config import resolve_paths
from ia_workspace.context import AppContext
from ia_workspace.persistence.database import Database
from ia_workspace.sessions.manager import Callback

SHUTDOWN_GRACE_SECONDS = 10.0


def _glib_dispatch(fn: Callback) -> None:
    def run() -> bool:
        fn()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run)


def _glib_schedule(seconds: float, fn: Callback) -> Callback:
    fired = False

    def run() -> bool:
        nonlocal fired
        fired = True
        fn()
        return GLib.SOURCE_REMOVE

    source_id = GLib.timeout_add(int(seconds * 1000), run)

    def cancel() -> None:
        if not fired:
            GLib.source_remove(source_id)

    return cancel


class IAWorkspaceApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.ctx: AppContext | None = None
        self._window = None
        self._quitting = False

    # ---- ciclo de vida ---------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        paths = resolve_paths()
        self.ctx = AppContext.create(
            Database(paths.database),
            paths=paths,
            dispatch=_glib_dispatch,
            schedule=_glib_schedule,
            simulated_agents=os.environ.get("IAW_SIMULATED_AGENTS") == "1",
        )
        from ia_workspace.ui.dialogs.preferences import apply_color_scheme

        apply_color_scheme(self.ctx)
        self._load_css()
        self._setup_actions()
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_unix_signal)

    def do_activate(self) -> None:
        if self._window is None:
            from ia_workspace.ui.window import MainWindow

            assert self.ctx is not None
            self._window = MainWindow(self, self.ctx)
        self._window.present()

    def do_shutdown(self) -> None:
        if self._window is not None:
            self._window.save_state()
        if self.ctx is not None:
            self.ctx.close()
            self.ctx = None
        Adw.Application.do_shutdown(self)

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).parent / "ui" / "style.css"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # ---- acciones --------------------------------------------------------------

    def _setup_actions(self) -> None:
        for name, handler, accels in (
            ("quit", lambda *_: self.request_quit(), ["<Control>q"]),
            ("about", lambda *_: self._show_about(), []),
            ("preferences", lambda *_: self._show_preferences(), ["<Control>comma"]),
            ("shortcuts", lambda *_: self._show_shortcuts(), ["<Control>question"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def _show_about(self) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="applications-engineering-symbolic",
            version=VERSION,
            developer_name="Elian Martinez",
            comments="Centro de coordinación de proyectos y agentes de IA para GNOME.",
            license_type=Gtk.License.CUSTOM,
            license="Uso personal.",
        )
        dialog.present(self._window)

    def _show_preferences(self) -> None:
        from ia_workspace.ui.dialogs.preferences import PreferencesDialog

        assert self.ctx is not None
        PreferencesDialog(self.ctx).present(self._window)

    def _show_shortcuts(self) -> None:
        from ia_workspace.ui.dialogs.shortcuts import build_shortcuts_dialog

        build_shortcuts_dialog().present(self._window)

    # ---- salida controlada -----------------------------------------------------

    def request_quit(self) -> None:
        """Salida completa solicitada por el usuario. Confirma si hay agentes activos."""
        if self._quitting or self.ctx is None:
            return
        active = self.ctx.sessions.active_sessions()
        if not active or self._window is None:
            self._shutdown_sessions_and_quit()
            return
        from ia_workspace.ui.dialogs.confirm_quit import build_confirm_quit_dialog

        dialog = build_confirm_quit_dialog(self.ctx, active)
        dialog.connect("response", self._on_quit_response)
        dialog.present(self._window)

    def _on_quit_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "quit":
            self._shutdown_sessions_and_quit()

    def _shutdown_sessions_and_quit(self) -> None:
        if self._quitting or self.ctx is None:
            return
        self._quitting = True
        if self._window is not None and self.ctx.sessions.has_active:
            self._window.show_shutting_down()
        self.ctx.sessions.shutdown(self.quit, grace_seconds=SHUTDOWN_GRACE_SECONDS)

    def _on_unix_signal(self) -> bool:
        # Señal del sistema (Ctrl+C en la terminal, apagado): sin diálogo, pero ordenado.
        self._shutdown_sessions_and_quit()
        return GLib.SOURCE_CONTINUE
