"""Genera capturas PNG de las pantallas principales para revisar el diseño.

Uso (sin abrir ventanas en el escritorio):
    scripts/screenshots.sh [claro|oscuro]

Crea carpetas y datos de demostración solo dentro de ``var/`` del repositorio.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEMO = ROOT / "var" / "demo"
OUT = ROOT / "var" / "screenshots"

import gi  # noqa: E402

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


def capture(widget: Gtk.Widget, name: str) -> None:
    pump(1.2)
    node = None
    deadline = time.monotonic() + 3
    while node is None and time.monotonic() < deadline:
        widget.queue_draw()
        pump(0.1)
        width, height = widget.get_width(), widget.get_height()
        paintable = Gtk.WidgetPaintable(widget=widget)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
    if node is None:
        print(f"[!] {name}: nada que dibujar")
        return
    texture = widget.get_native().get_renderer().render_texture(node, None)
    path = OUT / f"{name}.png"
    texture.save_to_png(str(path))
    print(f"[ok] {path.relative_to(ROOT)}")


def make_demo_folders() -> list[tuple[str, Path]]:
    shutil.rmtree(DEMO, ignore_errors=True)
    specs = [
        ("FleetControl", "fleet-control", "main", {"package.json": '{"dependencies":{"next":"1","prisma":"1"}}', "AGENTS.md": ""}),
        ("Sistema Pablo", "sistema-pablo", "feature/facturas", {"package.json": '{"dependencies":{"express":"1"}}', "CLAUDE.md": ""}),
        ("Inventario", "inventario", None, {"pyproject.toml": ""}),
    ]
    result = []
    for name, folder, branch, files in specs:
        path = DEMO / folder
        path.mkdir(parents=True)
        if branch:
            (path / ".git").mkdir()
            (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        for file, content in files.items():
            (path / file).write_text(content)
        result.append((name, path))
    return result


def main() -> None:
    scheme = sys.argv[1] if len(sys.argv) > 1 else "oscuro"
    data = ROOT / "var" / "demo-data"
    shutil.rmtree(data, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["IAW_DATA_DIR"] = str(data)
    os.environ["IAW_SIMULATED_AGENTS"] = "1"
    # Shell determinista y sin historial para las capturas.
    os.environ["IAW_TERMINAL_SHELL"] = "/bin/bash --noprofile --norc"
    os.environ["HISTFILE"] = ""

    from ia_workspace.app import IAWorkspaceApplication
    from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind

    app = IAWorkspaceApplication()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)

    def scenario() -> bool:
        try:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_LIGHT if scheme == "claro" else Adw.ColorScheme.FORCE_DARK
            )
            # Broadway sin navegador no avanza las animaciones: se desactivan.
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
            win, ctx = app._window, app.ctx
            win.set_default_size(1366, 768)
            capture(win, f"{scheme}-00-bienvenida")
            ids = []
            for i, (name, path) in enumerate(make_demo_folders()):
                agents = SELECTABLE_AGENTS if i != 2 else (AgentKind.CLAUDE_CODE,)
                ids.append(ctx.projects.register(name, str(path), agents).id)
            win.open_project(ids[0])
            view = win._workspaces[ids[0]]
            view._prompt.get_buffer().set_text(
                "Agrega un filtro por estado de mantenimiento en la lista de vehículos."
            )
            view._start_session(with_prompt=True)
            win.open_project(ids[1])
            win._workspaces[ids[1]]._prompt.get_buffer().set_text("Revisa la migración de facturas.")
            win._workspaces[ids[1]]._start_session(with_prompt=True)
            pump(2.5)
            from ia_workspace.brain.model import Scope

            key = ctx.brain.project_key(ctx.projects.get(ids[0]))
            ctx.brain.create(Scope.PROJECT, key, "estado", "Rama activa",
                             "feature/mantenimiento-filtros — filtro por estado de mantenimiento en el listado.", "git")
            ctx.brain.create(Scope.PROJECT, key, "estado", "Estabilidad",
                             "Reportes y asignación de conductores estables en producción.")
            ctx.brain.create(Scope.PROJECT, key, "decisiones", "ADR-006 · Fechas en UTC",
                             "Se guardan en UTC; la conversión a la zona local se hace solo al presentar.", "fechas")
            ctx.brain.create(Scope.PROJECT, key, "modulos", "Webhooks de pagos",
                             "Reciben eventos firmados y se reintentan tres veces.", "pagos", module="Pagos")
            ctx.brain.create(Scope.GLOBAL, "", "preferencias", "Idioma", "Responde siempre en español.")
            for screen in ("home", "projects", "agents", "memory"):
                win.navigate(screen)
                capture(win, f"{scheme}-{('home', 'projects', 'agents', 'memory').index(screen) + 1:02d}-{screen}")
            ctx.chat_links.link(ids[0], "https://chatgpt.com/c/6718a2b4-1c2d-8000-9f3a-0123456789ab",
                                "Plan: mantenimiento preventivo")
            ctx.chat_links.link(ids[0], "https://chatgpt.com/share/aaaabbbb-cccc-8000-9999-000011112222",
                                "Esquema de facturación (compartido)")
            memory_page = win._pages["memory"]
            win.navigate("memory")
            memory_page.select_project(ids[0])
            memory_page.select_category("estado")
            capture(win, f"{scheme}-04b-memoria-estado")
            memory_page.set_query("utc")
            capture(win, f"{scheme}-04c-memoria-busqueda")
            memory_page.set_query("")
            memory_page.open_document("decisiones/adr-006-fechas-en-utc.md")
            capture(win, f"{scheme}-04d-memoria-documento")
            memory_page._last_dialog.force_close()
            win.open_project(ids[0])
            capture(win, f"{scheme}-05-workspace")

            # Transferencia de un prompt a un agente: diálogo de revisión y pestaña «Tareas».
            view._prompt.get_buffer().set_text("Agrega pruebas automatizadas para el checkout.")
            view._agent_picker.set_selected(0)
            view.request_transfer()
            capture(win, f"{scheme}-05e-transferir")
            view._last_dialog.confirm_button.emit("clicked")
            pump(0.5)
            view._ctx_toggle.set_active_name("tasks")
            capture(win, f"{scheme}-05f-tareas")

            # Diálogo flotante de detalle de tarea desde el panel derecho
            first_task_item = view._tasks_list.get_first_child()
            if first_task_item:
                first_task_item.emit("activated")
                pump(0.6)
                capture(win, f"{scheme}-05i-dialogo-tarea")
                if hasattr(view, "_last_dialog") and view._last_dialog:
                    view._last_dialog.close()
                    pump(0.3)

            # Sembrar plantillas de demostración
            from ia_workspace.domain.models import PromptTemplateScope
            tmpl1 = ctx.prompt_templates.create(
                name="Auditoría de Seguridad OWASP",
                content="Analiza {{archivo}} buscando posibles vulnerabilidades de inyección y autenticación. Entrega mitigaciones concretas para {{modulo}}.",
                category="Seguridad",
                scope=PromptTemplateScope.GLOBAL,
                description="Auditoría estricta de código antes de pull request.",
            )
            ctx.prompt_templates.create(
                name="Refactorización y tipado estricto",
                content="Revisa el archivo {{archivo}} y añade anotaciones de tipo exhaustivas según PEP 484.",
                category="Calidad",
                scope=PromptTemplateScope.PROJECT,
                project_id=ids[0],
                description="Modernización de código Python legado.",
            )

            # Pantalla completa «Tareas y prompts»
            win.navigate("tasks")
            pump(0.6)
            tasks_page = win._pages.get("tasks")
            if tasks_page:
                tasks_page.refresh()
                pump(0.4)
            capture(win, f"{scheme}-05g-pantalla-tareas")
            if tasks_page and hasattr(tasks_page, "_tasks_list"):
                first_row = tasks_page._tasks_list.get_first_child()
                if first_row and hasattr(first_row, "set_expanded"):
                    first_row.set_expanded(True)
                    pump(0.5)
                    capture(win, f"{scheme}-05h-tarea-detalle")

            # Captura de la sección «Biblioteca de prompts»
            if tasks_page and hasattr(tasks_page, "_section_toggle"):
                tasks_page._section_toggle.set_active_name("templates")
                pump(0.8)
                first_tmpl_row = tasks_page._templates_list.get_first_child()
                if first_tmpl_row and hasattr(first_tmpl_row, "set_expanded"):
                    first_tmpl_row.set_expanded(True)
                    pump(0.5)
                capture(win, f"{scheme}-05j-biblioteca-prompts")

            win.open_project(ids[0])
            pump(0.8)
            view = win._workspaces[ids[0]]

            # Diálogo modal de Biblioteca en el Workspace
            view._open_prompt_library()
            pump(1.0)
            capture(win, f"{scheme}-05k-dialogo-biblioteca")

            # Abrir diálogo de variables para la plantilla seleccionada
            from ia_workspace.ui.dialogs.fill_variables import FillVariablesDialog
            if view._last_dialog:
                view._last_dialog.close()
                pump(0.3)
            var_dlg = FillVariablesDialog(template=tmpl1, on_complete=lambda _r: None)
            var_dlg.present(win)
            if "archivo" in var_dlg._entries:
                var_dlg._entries["archivo"].set_text("src/auth/service.py")
            if "modulo" in var_dlg._entries:
                var_dlg._entries["modulo"].set_text("Módulo de autenticación")
            pump(0.8)
            capture(win, f"{scheme}-05l-dialogo-variables")
            var_dlg.close()
            pump(0.3)

            win.set_default_size(1024, 768)
            view.request_link_conversation()
            view._last_dialog.url_row.set_text("https://chatgpt.com/share/0f0e0d0c-0b0a-8000-9999-aabbccddeeff")
            capture(win, f"{scheme}-05a-vincular")
            view._last_dialog.force_close()
            view.set_context_visible(False)
            win.queue_resize()
            capture(win, f"{scheme}-05c-panel-oculto")
            view.set_context_visible(True)
            win._split.set_show_sidebar(False)  # deja sitio para el panel ChatGPT a 1024 px
            win.queue_resize()
            capture(win, f"{scheme}-05d-conversaciones")
            win._split.set_show_sidebar(True)
            terminal = view.new_terminal()
            if terminal:
                pump(1.0)
                ctx.sessions.send_input(terminal, "PS1='\\W $ '; clear; pwd; ls -la; for i in 1 2 3; do echo paso $i; done\n")
                capture(win, f"{scheme}-05b-terminal")
            win._workspaces[ids[0]]._ctx_toggle.set_active_name("memory")
            win.open_project(ids[2])
            capture(win, f"{scheme}-06-workspace-vacio")
            from ia_workspace.ui.dialogs.add_project import AddProjectDialog

            dialog = AddProjectDialog(ctx, on_added=lambda _i: None)
            dialog.present(win)
            dialog.set_folder(str(DEMO / "fleet-control"))
            capture(win, f"{scheme}-07-agregar-proyecto")
            dialog.force_close()
            from ia_workspace.ui.dialogs.confirm_quit import build_confirm_quit_dialog

            confirm = build_confirm_quit_dialog(ctx, ctx.sessions.active_sessions())
            confirm.present(win)
            capture(win, f"{scheme}-08-confirmar-salida")
            confirm.force_close()
            app.activate_action("about", None)
            capture(win, f"{scheme}-10-acerca-de")
            about = win.get_visible_dialog()
            if about is not None:
                about.force_close()
            win.set_default_size(820, 700)
            pump(0.3)
            win.open_project(ids[0])
            capture(win, f"{scheme}-09-estrecho")
        finally:
            app._shutdown_sessions_and_quit()
        return GLib.SOURCE_REMOVE

    app.connect("activate", lambda *_: GLib.idle_add(scenario))
    app.run([])


if __name__ == "__main__":
    main()
