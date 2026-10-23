"""Validación de continuidad y aislamiento de sesiones multiproyecto (Fase 7A.1).

Cubre bajo entorno Broadway con procesos y agentes simulados:
1. Iniciar sesiones simuladas en dos proyectos independientes.
2. Cambiar entre proyectos sin detener ni reiniciar sus procesos.
3. Comprobar que cada proyecto conserva sus pestañas, terminales y borrador.
4. Confirmar que el workspace visible solo muestra datos del proyecto seleccionado.
5. Verificar que minimizar la ventana no termina los procesos.
6. Comprobar la advertencia al cerrar completamente con agentes activos.
7. Confirmar la finalización controlada de los procesos cuando el usuario autoriza el cierre.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import traceback
import unittest

from helpers import temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
CID_ALFA = "11111111-1111-8000-0000-000000000001"
CID_BETA = "22222222-2222-8000-0000-000000000002"


def pump(until=lambda: False, seconds: float = 0.2) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not until():
        context.iteration(False)
        time.sleep(0.005)


@unittest.skipUnless(HAS_DISPLAY, "sin display gráfico")
class MultiprojectSessionsUITest(unittest.TestCase):
    def test_multiproject_sessions_continuity_and_isolation(self) -> None:
        with temp_dir() as data, temp_dir() as folders:
            dir_a = Path(folders) / "proyecto-alfa"
            dir_b = Path(folders) / "proyecto-beta"
            dir_a.mkdir()
            dir_b.mkdir()
            (dir_a / "AGENTS.md").write_text("reglas alfa")
            (dir_b / "CLAUDE.md").write_text("reglas beta")

            os.environ["IAW_DATA_DIR"] = data
            os.environ["IAW_SIMULATED_AGENTS"] = "1"
            try:
                errors = self._run_scenario(dir_a, dir_b)
            finally:
                os.environ.pop("IAW_DATA_DIR", None)
                os.environ.pop("IAW_SIMULATED_AGENTS", None)

            self.assertEqual(errors, [])

    def _run_scenario(self, dir_a: Path, dir_b: Path) -> list[str]:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionState

        errors: list[str] = []
        app = IAWorkspaceApplication()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        previous_hook = sys.excepthook

        def hook(exc_type, exc, tb):
            errors.append("".join(traceback.format_exception(exc_type, exc, tb)))

        sys.excepthook = hook

        def check(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        def scenario() -> bool:
            try:
                Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                win = app._window
                ctx = app.ctx
                pump(seconds=0.05)

                # Registrar los dos proyectos independientes
                p_a = ctx.projects.register("Proyecto Alfa", str(dir_a), (AgentKind.CLAUDE_CODE, AgentKind.CODEX))
                p_b = ctx.projects.register("Proyecto Beta", str(dir_b), (AgentKind.CLAUDE_CODE, AgentKind.ANTIGRAVITY))

                # Registrar datos complementarios aislados por proyecto (conversaciones y tareas)
                ctx.chat_links.link(p_a.id, f"https://chatgpt.com/c/{CID_ALFA}", "Chat Alfa")
                ctx.chat_links.link(p_b.id, f"https://chatgpt.com/c/{CID_BETA}", "Chat Beta")
                tr_a = ctx.prompt_transfers.record(p_a.id, AgentKind.CLAUDE_CODE, "Prompt inicial Alfa", "s_init_a", "direct", "sent", "")
                ctx.tasks.create_for_transfer(tr_a, title="Tarea Alfa 1")
                tr_b = ctx.prompt_transfers.record(p_b.id, AgentKind.ANTIGRAVITY, "Prompt inicial Beta", "s_init_b", "direct", "sent", "")
                ctx.tasks.create_for_transfer(tr_b, title="Tarea Beta 1")

                # =========================================================================
                # 1. Iniciar en Proyecto Alfa y abrir 2 sesiones simuladas
                # =========================================================================
                win.open_project(p_a.id)
                pump(seconds=0.1)
                view_a = win._workspaces[p_a.id]
                check(win._stack.get_visible_child() is view_a, "Alfa no es la vista visible inicial")
                check(view_a.window_title.get_title() == "Proyecto Alfa", "El título de cabecera no es Alfa")

                # Redactar borrador de prompt en Proyecto Alfa
                view_a._prompt.get_buffer().set_text("Prompt borrador para Alfa")
                pump(seconds=0.05)
                check(view_a.state.prompt_draft == "Prompt borrador para Alfa", "Alfa no guardó su borrador")

                # Iniciar primera sesión simulada en Alfa (Claude Code)
                view_a.start_agent(AgentKind.CLAUDE_CODE)
                pump(lambda: len(ctx.sessions.sessions(p_a.id)) >= 1, seconds=3.0)
                sessions_a = ctx.sessions.sessions(p_a.id)
                check(len(sessions_a) == 1, "no se inició la primera sesión en Alfa")
                s_a1 = sessions_a[0]
                check(s_a1.state.is_active, "la sesión s_a1 de Alfa no está activa")

                # Iniciar segunda sesión simulada en Alfa (Codex)
                view_a.start_agent(AgentKind.CODEX)
                pump(lambda: len(ctx.sessions.sessions(p_a.id)) >= 2, seconds=3.0)
                sessions_a = ctx.sessions.sessions(p_a.id)
                check(len(sessions_a) == 2, "no se inició la segunda sesión en Alfa")
                s_a2 = sessions_a[1]
                check(s_a2.state.is_active, "la sesión s_a2 de Alfa no está activa")

                # Comprobar pestañas de sesiones en Alfa
                check(set(view_a._tab_buttons.keys()) == {s_a1.id, s_a2.id}, "Alfa no muestra sus dos pestañas")

                # Comprobar aislamiento de datos visibles en Alfa (Requisito 4)
                links_a = ctx.chat_links.list(p_a.id)
                check(len(links_a) == 1 and links_a[0].title == "Chat Alfa", "Alfa no lista exclusivamente Chat Alfa")
                tasks_a = ctx.tasks.list_for_project(p_a.id)
                check(len(tasks_a) == 1 and tasks_a[0].title == "Tarea Alfa 1", "Alfa no lista exclusivamente Tarea Alfa 1")

                # =========================================================================
                # 2. Cambiar a Proyecto Beta y verificar aislamiento estricto
                # =========================================================================
                win.open_project(p_b.id)
                pump(seconds=0.1)
                view_b = win._workspaces[p_b.id]
                check(win._stack.get_visible_child() is view_b, "Beta no es la vista visible tras el cambio")
                check(view_b.window_title.get_title() == "Proyecto Beta", "El título de cabecera no es Beta")

                # Aislamiento: el borrador de Alfa no se filtró a Beta
                check(view_b.state.prompt_draft == "", "el borrador de Alfa se filtró al estado de Beta")
                buf_b_initial = view_b._prompt.get_buffer()
                text_b_initial = buf_b_initial.get_text(buf_b_initial.get_start_iter(), buf_b_initial.get_end_iter(), False)
                check(text_b_initial == "", "el widget de prompt de Beta no está vacío")

                # Aislamiento: las pestañas de Alfa no aparecen en Beta
                check(len(view_b._tab_buttons) == 0, "Beta muestra pestañas de sesiones al inicio sin tener sesiones")

                # Aislamiento de datos en Beta
                links_b = ctx.chat_links.list(p_b.id)
                check(len(links_b) == 1 and links_b[0].title == "Chat Beta", "Beta no lista exclusivamente Chat Beta")
                tasks_b = ctx.tasks.list_for_project(p_b.id)
                check(len(tasks_b) == 1 and tasks_b[0].title == "Tarea Beta 1", "Beta no lista exclusivamente Tarea Beta 1")

                # =========================================================================
                # 3. Continuidad: los procesos de Alfa siguen activos en segundo plano
                # =========================================================================
                check(ctx.sessions.get(s_a1.id).state.is_active, "s_a1 de Alfa se detuvo al abrir Beta")
                check(ctx.sessions.get(s_a2.id).state.is_active, "s_a2 de Alfa se detuvo al abrir Beta")

                # Iniciar sesión simulada en Beta (Antigravity)
                view_b._prompt.get_buffer().set_text("Prompt borrador para Beta")
                pump(seconds=0.05)
                check(view_b.state.prompt_draft == "Prompt borrador para Beta", "Beta no guardó su borrador")

                view_b.start_agent(AgentKind.ANTIGRAVITY)
                pump(lambda: len(ctx.sessions.sessions(p_b.id)) >= 1, seconds=3.0)
                sessions_b = ctx.sessions.sessions(p_b.id)
                check(len(sessions_b) == 1, "no se inició la sesión en Beta")
                s_b1 = sessions_b[0]
                check(s_b1.state.is_active, "la sesión s_b1 de Beta no está activa")

                # Comprobar pestañas de sesiones en Beta (solo s_b1, ninguna de Alfa)
                check(set(view_b._tab_buttons.keys()) == {s_b1.id}, "Beta no muestra exactamente su pestaña")

                # Enviar entrada a s_a1 (Alfa) mientras Beta sigue visible para confirmar funcionamiento en background
                ctx.sessions.send_input(s_a1.id, "instrucción en segundo plano para Alfa")
                pump(seconds=0.1)
                check(ctx.sessions.get(s_a1.id).state.is_active, "s_a1 falló al recibir input en background")

                # =========================================================================
                # 4. Volver a Alfa y comprobar persistencia de pestañas, borrador y procesos
                # =========================================================================
                win.open_project(p_a.id)
                pump(seconds=0.1)
                check(win._stack.get_visible_child() is view_a, "Alfa no volvió a ser la vista visible")

                # Conserva borrador independiente
                check(view_a.state.prompt_draft == "Prompt borrador para Alfa", "Alfa perdió su borrador")
                buf_a = view_a._prompt.get_buffer()
                text_a = buf_a.get_text(buf_a.get_start_iter(), buf_a.get_end_iter(), False)
                check(text_a == "Prompt borrador para Alfa", "el widget de prompt de Alfa perdió su texto")

                # Conserva pestañas y terminales
                check(set(view_a._tab_buttons.keys()) == {s_a1.id, s_a2.id}, "Alfa perdió sus pestañas de sesión")

                # El proceso de Beta sigue activo en segundo plano mientras Alfa está abierto
                check(ctx.sessions.get(s_b1.id).state.is_active, "s_b1 de Beta se detuvo al volver a Alfa")

                # =========================================================================
                # 5. Volver a Beta y comprobar persistencia de pestañas, borrador y procesos
                # =========================================================================
                win.open_project(p_b.id)
                pump(seconds=0.1)
                check(win._stack.get_visible_child() is view_b, "Beta no volvió a ser la vista visible")

                # Conserva borrador independiente
                check(view_b.state.prompt_draft == "Prompt borrador para Beta", "Beta perdió su borrador")
                buf_b = view_b._prompt.get_buffer()
                text_b = buf_b.get_text(buf_b.get_start_iter(), buf_b.get_end_iter(), False)
                check(text_b == "Prompt borrador para Beta", "el widget de prompt de Beta perdió su texto")

                # Conserva pestaña
                check(set(view_b._tab_buttons.keys()) == {s_b1.id}, "Beta perdió su pestaña de sesión")

                # Los procesos de Alfa siguen activos
                check(ctx.sessions.get(s_a1.id).state.is_active, "s_a1 se detuvo")
                check(ctx.sessions.get(s_a2.id).state.is_active, "s_a2 se detuvo")

                # =========================================================================
                # 6. Minimizar la ventana no termina los procesos (Requisito 5)
                # =========================================================================
                win.minimize()
                pump(seconds=0.3)
                check(ctx.sessions.get(s_a1.id).state.is_active, "minimizar detuvo s_a1 de Alfa")
                check(ctx.sessions.get(s_a2.id).state.is_active, "minimizar detuvo s_a2 de Alfa")
                check(ctx.sessions.get(s_b1.id).state.is_active, "minimizar detuvo s_b1 de Beta")
                check(len(ctx.sessions.active_sessions()) == 3, "el número de sesiones activas cambió tras minimizar")
                win.unminimize()
                pump(seconds=0.1)

                # =========================================================================
                # 7. Advertencia al cerrar con agentes activos (Requisito 6)
                # =========================================================================
                # Dos solicitudes consecutivas/simultáneas no deben crear diálogos duplicados
                vetoed1 = win._on_close_request(win)
                check(vetoed1 is True, "_on_close_request no vetó el primer cierre con sesiones activas")
                first_dialog = app._last_quit_dialog
                check(first_dialog is not None, "no se presentó el diálogo confirm_quit")

                vetoed2 = win._on_close_request(win)
                check(vetoed2 is True, "_on_close_request no vetó el segundo cierre")
                check(app._last_quit_dialog is first_dialog, "una segunda solicitud creó un diálogo duplicado")

                dlg = app._last_quit_dialog
                check("3 sesiones activas" in dlg.get_heading(), f"encabezado inesperado del diálogo: {dlg.get_heading()!r}")
                check("Proyecto Alfa" in dlg.get_body(), "el diálogo de salida no menciona Proyecto Alfa")
                check("Proyecto Beta" in dlg.get_body(), "el diálogo de salida no menciona Proyecto Beta")

                # Simular respuesta 'cancel' (Seguir trabajando)
                dlg.emit("response", "cancel")
                pump(seconds=0.1)
                check(win.get_visible(), "la ventana se cerró tras cancelar el diálogo")
                check(app._last_quit_dialog is None, "_last_quit_dialog no se limpió tras cancelar")

                # Las sesiones continúan activas después de cancelar
                check(ctx.sessions.get(s_a1.id).state.is_active, "s_a1 se detuvo tras cancelar salida")
                check(ctx.sessions.get(s_a2.id).state.is_active, "s_a2 se detuvo tras cancelar salida")
                check(ctx.sessions.get(s_b1.id).state.is_active, "s_b1 se detuvo tras cancelar salida")

                # Cancelar el cierre permite abrir nuevamente el diálogo
                vetoed3 = win._on_close_request(win)
                check(vetoed3 is True, "la nueva solicitud de cierre no fue vetada")
                second_dialog = app._last_quit_dialog
                check(
                    second_dialog is not None and second_dialog is not first_dialog,
                    "no se abrió un nuevo diálogo tras cancelar el anterior",
                )

                # =========================================================================
                # 8. Finalización controlada cuando el usuario autoriza el cierre (Requisito 7)
                # =========================================================================
                second_dialog.emit("response", "quit")
                pump(seconds=0.05)

                check(win._root.get_visible_child_name() == "closing", "la ventana no mostró la pantalla 'closing'")
                check(ctx.sessions.is_shutting_down, "SessionManager no entró en shutting_down")

                # Esperar a que la finalización ordenada concluya
                pump(lambda: not ctx.sessions.has_active, seconds=6.0)
                check(not ctx.sessions.has_active, "quedaron sesiones activas tras shutdown")
                check(ctx.sessions.get(s_a1.id).state is SessionState.STOPPED, f"s_a1 estado: {ctx.sessions.get(s_a1.id).state}")
                check(ctx.sessions.get(s_a2.id).state is SessionState.STOPPED, f"s_a2 estado: {ctx.sessions.get(s_a2.id).state}")
                check(ctx.sessions.get(s_b1.id).state is SessionState.STOPPED, f"s_b1 estado: {ctx.sessions.get(s_b1.id).state}")

            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())
                app.quit()
            return GLib.SOURCE_REMOVE

        def timeout() -> bool:
            errors.append("tiempo límite de prueba agotado (timeout)")
            app.quit()
            return GLib.SOURCE_REMOVE

        app.connect("activate", lambda *_: GLib.idle_add(scenario))
        watchdog = GLib.timeout_add_seconds(20, timeout)
        try:
            app.run([])
        finally:
            if "tiempo límite de prueba agotado (timeout)" not in errors:
                GLib.source_remove(watchdog)
            sys.excepthook = previous_hook

        return errors


if __name__ == "__main__":
    unittest.main()
