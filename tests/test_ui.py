"""Prueba de integración de la interfaz con la aplicación real.

Necesita un display. ``scripts/check.sh`` la ejecuta en un servidor Broadway
aislado (sin abrir ventanas en el escritorio). Sin display, se omite.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

from helpers import temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


def pump(until=lambda: False, seconds: float = 0.2) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        context.iteration(False)
        time.sleep(0.005)


@unittest.skipUnless(HAS_DISPLAY, "sin display gráfico")
class AppFlowTest(unittest.TestCase):
    def test_full_flow(self) -> None:
        with temp_dir() as data, temp_dir() as folders:
            proj_a = Path(folders) / "proyecto-alfa"
            proj_b = Path(folders) / "proyecto-beta"
            proj_a.mkdir()
            proj_b.mkdir()
            (proj_a / "AGENTS.md").write_text("reglas")
            os.environ["IAW_DATA_DIR"] = data
            os.environ["IAW_SIMULATED_AGENTS"] = "1"
            try:
                errors = self._run_app(proj_a, proj_b)
            finally:
                os.environ.pop("IAW_DATA_DIR")
                os.environ.pop("IAW_SIMULATED_AGENTS")
            self.assertEqual(errors, [])

            # Tras salir: los proyectos y el estado visual siguen en SQLite.
            from ia_workspace.persistence.database import Database
            from ia_workspace.persistence.repositories import ProjectRepository, WorkspaceStateRepository

            db = Database(Path(data) / "ia-workspace.db")
            projects = ProjectRepository(db).list()
            self.assertEqual([p.name for p in projects], ["proyecto-alfa", "Beta"])
            states = WorkspaceStateRepository(db)
            self.assertFalse(states.load(projects[0].id).show_chat)
            self.assertTrue(states.load(projects[1].id).show_chat)
            self.assertEqual(states.load(projects[1].id).prompt_draft, "borrador de B")
            db.close()

    def _run_app(self, proj_a: Path, proj_b: Path) -> list[str]:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.sessions.models import SessionState

        errors: list[str] = []
        app = IAWorkspaceApplication()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        previous_hook = sys.excepthook

        def hook(exc_type, exc, tb):  # excepciones dentro de callbacks GTK
            import traceback

            errors.append("".join(traceback.format_exception(exc_type, exc, tb)))

        sys.excepthook = hook

        def check(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        def scenario() -> bool:
            try:
                win = app._window
                ctx = app.ctx
                pump()
                check(win._stack.get_visible_child_name() == "home", "no inicia en Inicio")

                # Navegación principal.
                for screen in ("projects", "agents", "tasks", "memory", "history", "home"):
                    win.activate_action("win.navigate", GLib.Variant("s", screen))
                    pump(seconds=0.05)
                    check(win._stack.get_visible_child_name() == screen, f"no navega a {screen}")

                # Registro de proyectos (el diálogo usa el mismo servicio).
                from ia_workspace.ui.dialogs.add_project import AddProjectDialog

                added: list[int] = []
                dialog = AddProjectDialog(ctx, on_added=added.append)
                dialog.present(win)
                dialog.set_folder(str(proj_a))
                pump(seconds=0.05)
                check(dialog._name.get_text() == "proyecto-alfa", "no propone el nombre de la carpeta")
                dialog._submit()
                check(len(added) == 1, "el diálogo no registró el proyecto")
                b = ctx.projects.register("Beta", str(proj_b), ())
                a_id = added[0]
                pump(seconds=0.05)
                check(len(win.sidebar._project_rows) == 2, "la barra lateral no lista 2 proyectos")

                # Estados visuales independientes.
                win.open_project(a_id)
                pump(seconds=0.1)
                view_a = win._workspaces[a_id]
                check(ctx.workspace.active_project_id == a_id, "A no es el proyecto activo")
                view_a._prompt.get_buffer().set_text("Tarea de A")
                view_a._panel_switches["show_chat"].set_active(False)
                win.open_project(b.id)
                pump(seconds=0.1)
                view_b = win._workspaces[b.id]
                check(ctx.workspace.active_project_id == b.id, "B no es el proyecto activo")
                check(view_b.state.prompt_draft == "", "el borrador de A se filtró a B")
                check(view_b.state.show_chat and view_b._panel_switches["show_chat"].get_active(),
                      "ocultar el chat en A lo ocultó en B")
                check(not view_b._transfer.get_sensitive(), "B sin agentes no debería poder transferir")
                view_b._prompt.get_buffer().set_text("borrador de B")
                win.open_project(a_id)
                pump(seconds=0.05)
                check(view_a.state.prompt_draft == "Tarea de A", "A perdió su borrador")
                check(not view_a.state.show_chat and not view_a._chat_pane.get_visible(), "A perdió su distribución")

                # Sesión en A que sigue viva al cambiar de proyecto y de pantalla.
                view_a._start_session(with_prompt=True)
                sessions = ctx.sessions.sessions(a_id)
                check(len(sessions) == 1, "no se inició la sesión simulada")
                win.open_project(b.id)
                win.navigate("agents")
                pump(lambda: sessions[0].state is SessionState.IDLE, seconds=5)
                check(sessions[0].state is SessionState.IDLE, f"la sesión no avanzó: {sessions[0].state}")
                log = view_a._buffer_for(sessions[0].id)
                text = log.get_text(log.get_start_iter(), log.get_end_iter(), False)
                check("Tarea de A" in text, "la salida no llegó al workspace oculto de A")
                win.minimize()
                pump(seconds=0.2)
                check(sessions[0].state.is_active, "minimizar detuvo la sesión")

                # Autonomía de agentes en el Centro de agentes (Fase 4).
                from ia_workspace.domain.models import AgentKind, AutonomyMode

                page_agents = win._pages["agents"]
                check(page_agents._project is not None, "Centro de agentes sin proyecto seleccionado")
                claude_combo = page_agents._autonomy_rows[AgentKind.CLAUDE_CODE]
                check(claude_combo.get_selected() == 0, "Claude no inició en modo Normal")

                # 1. Pasar a Automático se persiste directamente.
                claude_combo.set_selected(1)
                pump(seconds=0.05)
                check(ctx.autonomy.get_mode(page_agents._project.id, AgentKind.CLAUDE_CODE) is AutonomyMode.AUTO,
                      "el modo automático no se guardó en SQLite")
                check(page_agents._last_dialog is None, "se abrió diálogo para modo automático")

                # 2. Pasar a Sin confirmaciones abre el diálogo de advertencia.
                claude_combo.set_selected(2)
                pump(seconds=0.05)
                check(page_agents._last_dialog is not None, "no se abrió el diálogo de confirmación para modo avanzado")

                # 3. Cancelar el diálogo revierte el combo y NO persiste DANGER.
                page_agents._last_dialog.emit("response", "cancel")
                pump(seconds=0.05)
                check(ctx.autonomy.get_mode(page_agents._project.id, AgentKind.CLAUDE_CODE) is AutonomyMode.AUTO,
                      "cancelar guardó cambios en SQLite")
                check(claude_combo.get_selected() == 1, "cancelar no revirtió la selección del combo")

                # 4. Confirmar el diálogo guarda el modo DANGER en SQLite.
                claude_combo.set_selected(2)
                pump(seconds=0.05)
                check(page_agents._last_dialog is not None, "no se abrió el diálogo de confirmación al reintentar")
                page_agents._last_dialog.emit("response", "confirm")
                pump(seconds=0.05)
                check(ctx.autonomy.get_mode(page_agents._project.id, AgentKind.CLAUDE_CODE) is AutonomyMode.DANGER,
                      "confirmar no guardó el modo DANGER en SQLite")

                # Diálogos secundarios.
                from ia_workspace.ui.dialogs.confirm_quit import build_confirm_quit_dialog
                from ia_workspace.ui.dialogs.preferences import PreferencesDialog
                from ia_workspace.ui.dialogs.shortcuts import build_shortcuts_dialog

                for dlg in (PreferencesDialog(ctx), build_shortcuts_dialog(),
                            build_confirm_quit_dialog(ctx, ctx.sessions.active_sessions())):
                    dlg.present(win)
                    pump(seconds=0.05)
                    dlg.force_close()

                # Cerrar la ventana con agentes activos no sale: pide confirmación.
                win.close()
                pump(seconds=0.1)
                check(win.get_visible(), "la ventana se cerró sin confirmar")
                check(sessions[0].state.is_active, "cerrar sin confirmar detuvo la sesión")

                # Confirmar: finalización ordenada y salida.
                app._on_quit_response(None, "quit")
                check(win._root.get_visible_child_name() == "closing", "no muestra la pantalla de cierre")
            except Exception:  # noqa: BLE001
                import traceback

                errors.append(traceback.format_exc())
                app.quit()
            started.extend(locals().get("sessions") or [])
            return GLib.SOURCE_REMOVE

        def timeout() -> bool:
            errors.append("tiempo agotado")
            app.quit()
            return GLib.SOURCE_REMOVE

        started: list = []
        app.connect("activate", lambda *_: GLib.idle_add(scenario))
        watchdog = GLib.timeout_add_seconds(20, timeout)
        try:
            app.run([])
        finally:
            if "tiempo agotado" not in errors:
                GLib.source_remove(watchdog)
            sys.excepthook = previous_hook
        for info in started:
            if not info.state.is_terminal:
                errors.append(f"la sesión seguía activa al salir: {info.state}")
        return errors


if __name__ == "__main__":
    unittest.main()
