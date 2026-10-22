"""Aplicación GTK: ciclo de vida, acciones globales y cierre controlado.

Las sesiones de agentes pertenecen a la aplicación (``AppContext.sessions``), no a
la ventana. Minimizar u ocultar la ventana no las afecta. Al salir por completo
(cerrar la ventana, Ctrl+Q o una señal del sistema) con sesiones activas, se pide
confirmación y se finalizan de forma ordenada antes de terminar el proceso.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from ia_workspace import APP_ID, APP_NAME, VERSION
from ia_workspace.config import resolve_paths
from ia_workspace.context import AppContext
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind
from ia_workspace.persistence.database import Database
from ia_workspace.sessions.manager import Callback
from ia_workspace.sessions.models import SessionSpec

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
        if GLib.get_application_name() != APP_NAME:  # solo una vez por proceso
            GLib.set_application_name(APP_NAME)
        self.ctx: AppContext | None = None
        self._window = None
        self._quitting = False
        self.terminal_backends: list = []

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
        self._register_terminals()
        self._load_css()
        self._setup_icon()
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
        # Red de seguridad: ningún proceso gestionado sobrevive a la aplicación.
        for backend in self.terminal_backends:
            backend.kill_all()
        if self._window is not None:
            self._window.save_state()
            self._window.release_web_views()
        from ia_workspace.ui.chatgpt_browser import release_sessions

        release_sessions()
        if self.ctx is not None:
            self.ctx.close()
            self.ctx = None
        Adw.Application.do_shutdown(self)

    def _register_terminals(self) -> None:
        try:
            from ia_workspace.terminal.backend import VteTerminalBackend
        except (ImportError, ValueError) as exc:  # falta gir1.2-vte-3.91
            print(f"IA Workspace: terminales no disponibles ({exc})", file=sys.stderr)
            return
        assert self.ctx is not None
        ctx = self.ctx
        backends = [VteTerminalBackend(settings=ctx.settings)]
        if not ctx.simulated_agents:
            # Agentes reales: su CLI oficial dentro de una terminal VTE. En modo simulado
            # (desarrollo y pruebas) nunca se lanzan, para no consumir sesiones de IA.
            from ia_workspace.agents.tools import build_launch_argv, build_login_argv

            def argv_for(kind: AgentKind):
                def build(spec: SessionSpec) -> list[str]:
                    if spec.mode == "login":
                        return build_login_argv(kind, which=ctx.agents.which)
                    context = spec.brain.launch_reference if spec.brain else None
                    extra_args = list(spec.args)
                    if kind is AgentKind.CLAUDE_CODE:
                        try:
                            from ia_workspace.quota.claude import get_claude_launch_settings
                            statusline_script = Path(__file__).resolve().parents[2] / "scripts" / "iaw-claude-statusline"
                            if statusline_script.is_file():
                                project_dir = Path(spec.cwd) if spec.cwd else None
                                claude_cfg = get_claude_launch_settings(statusline_script, project_dir=project_dir)
                                for k, v in claude_cfg.items():
                                    extra_args.extend(["--config", f"{k}={v}"])
                        except Exception:
                            pass
                    return build_launch_argv(
                        kind,
                        tuple(extra_args),
                        prompt=spec.initial_prompt,
                        which=ctx.agents.which,
                        context=context,
                        autonomy_mode=spec.autonomy_mode,
                    )

                return build

            backends += [VteTerminalBackend(kind, argv_for(kind), settings=ctx.settings) for kind in SELECTABLE_AGENTS]
        for backend in backends:
            ctx.sessions.register_backend(backend)
        self.terminal_backends = backends

    def _setup_icon(self) -> None:
        """Icono oficial: su nombre es el identificador de la aplicación.

        Instalada (o con el lanzador local), el tema de iconos ya lo encuentra en
        ``hicolor``. Desde el código fuente se añade ``data/icons`` a la búsqueda, para que
        la ventana y el diálogo «Acerca de» muestren el icono sin instalar nada.
        """
        display = Gdk.Display.get_default()
        source_icons = Path(__file__).resolve().parents[2] / "data" / "icons"
        if display is not None and (source_icons / "hicolor").is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(source_icons))
        Gtk.Window.set_default_icon_name(APP_ID)

    def _load_css(self) -> None:
        from ia_workspace.ui.theme import setup_theme

        display = Gdk.Display.get_default()
        setup_theme(display)

        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).parent / "ui" / "style.css"))
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
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
            application_icon=APP_ID,
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
