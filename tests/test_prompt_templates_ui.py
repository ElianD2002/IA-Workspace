"""Pruebas de interfaz y flujos de usuario para la biblioteca de plantillas de prompts (fase 6B.2).

Cubre:
1. Pantalla TasksPage con sección Biblioteca de prompts (crear, editar, eliminar, buscar, filtrar).
2. Botón «Biblioteca» en el Workspace y diálogo PromptLibraryDialog.
3. Inserción en el editor de prompts con preservación de borrador si el usuario cancela.
4. Relleno seguro de variables parametrizables {{variable}} con FillVariablesDialog.
5. Guardado de borrador como plantilla desde el Workspace.
6. Transferencia hacia un agente partiendo de una plantilla y creación de la tarea vinculada.
7. Aislamiento estricto entre proyectos en la UI del Workspace y Biblioteca.
8. Compatibilidad de estilos y temas Catppuccin Latte y Mocha.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import temp_dir

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib

from ia_workspace.context import AppContext
from ia_workspace.domain.models import (
    AgentKind,
    PromptTemplateScope,
    TaskState,
)
from ia_workspace.persistence.database import Database
from ia_workspace.ui.dialogs.fill_variables import FillVariablesDialog
from ia_workspace.ui.dialogs.prompt_library import PromptLibraryDialog
from ia_workspace.ui.dialogs.template_editor import TemplateEditorDialog
from ia_workspace.ui.pages.base import Navigator
from ia_workspace.ui.pages.tasks import TasksPage
from ia_workspace.ui.pages.workspace import WorkspaceView


class DummyNav(Navigator):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.toasts: list[str] = []
        self.opened_projects: list[int] = []

    def navigate(self, screen: str) -> None:
        pass

    def open_project(self, project_id: int) -> None:
        self.opened_projects.append(project_id)

    def sidebar_toggle(self) -> Gtk.Widget:
        return Gtk.Button()

    def toast(self, message: str) -> None:
        self.toasts.append(message)


class PromptTemplatesUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.db = Database(str(self.path / "test.db"))
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.nav = DummyNav(self.ctx)

        # Proyectos de prueba
        p1_dir = self.path / "p1"
        p1_dir.mkdir()
        p2_dir = self.path / "p2"
        p2_dir.mkdir()
        self.p1 = self.ctx.projects.register("Proyecto Alfa", str(p1_dir), (AgentKind.CLAUDE_CODE,))
        self.p2 = self.ctx.projects.register("Proyecto Beta", str(p2_dir), (AgentKind.CODEX,))

        self._pump()

    def tearDown(self) -> None:
        self.ctx.close()
        self.tmp.cleanup()

    def _pump(self) -> None:
        while GLib.MainContext.default().iteration(False):
            pass

    def test_01_tasks_page_templates_section_navigation(self) -> None:
        """Verifica el cambio de sección entre Tareas y Biblioteca en TasksPage."""
        page = TasksPage(self.nav)
        page.refresh()
        self._pump()

        # Por defecto muestra tareas
        self.assertEqual(page._active_section, "tasks")
        self.assertEqual(page._main_stack.get_visible_child_name(), "tasks")

        # Cambiar a biblioteca
        page._section_toggle.set_active_name("templates")
        self._pump()
        self.assertEqual(page._active_section, "templates")
        self.assertEqual(page._main_stack.get_visible_child_name(), "templates")
        self.assertEqual(page._tmpl_stack.get_visible_child_name(), "empty_no_templates")

    def test_02_template_editor_create_and_delete(self) -> None:
        """Creación y eliminación de una plantilla desde el editor con notificación."""
        saved_templates = []
        dialog = TemplateEditorDialog(
            ctx=self.ctx,
            project=self.p1,
            on_saved=lambda t: saved_templates.append(t),
        )
        dialog.name_entry.set_text("Revisión de Seguridad")
        dialog.category_entry.set_text("Seguridad")
        dialog.text_view.get_buffer().set_text("Auditar vulnerabilidades OWASP en {{archivo}}.")
        dialog.scope_toggle.set_active_name("project")

        # Guardar
        dialog._save()
        self._pump()

        self.assertEqual(len(saved_templates), 1)
        created = saved_templates[0]
        self.assertEqual(created.name, "Revisión de Seguridad")
        self.assertEqual(created.scope, PromptTemplateScope.PROJECT)
        self.assertEqual(created.project_id, self.p1.id)
        self.assertEqual(created.variables(), ["archivo"])

        # Visible en lista de p1 pero no de p2
        self.assertEqual(len(self.ctx.prompt_templates.list_for_project(self.p1.id)), 1)
        self.assertEqual(len(self.ctx.prompt_templates.list_for_project(self.p2.id)), 0)

        # Eliminar
        deleted = self.ctx.prompt_templates.delete(created.id)
        self.assertTrue(deleted)
        self.assertEqual(len(self.ctx.prompt_templates.list_for_project(self.p1.id)), 0)

    def test_03_fill_variables_dialog(self) -> None:
        """Diálogo interactivo para rellenar variables parametrizables."""
        tmpl = self.ctx.prompt_templates.create(
            name="Generador Tests",
            content="Escribe tests unitarios para {{modulo}} considerando {{casos}}.",
            category="Pruebas",
        )

        completed_prompt = []
        dialog = FillVariablesDialog(
            template=tmpl,
            on_complete=lambda res: completed_prompt.append(res),
        )

        # Antes de rellenar, el botón de inserción está bloqueado
        self.assertFalse(dialog.insert_btn.get_sensitive())

        # Rellenar parcialmente
        dialog._entries["modulo"].set_text("auth_service")
        self._pump()
        self.assertFalse(dialog.insert_btn.get_sensitive())

        # Rellenar completamente
        dialog._entries["casos"].set_text("tokens expirados y credenciales vacías")
        self._pump()
        self.assertTrue(dialog.insert_btn.get_sensitive())

        # Aplicar
        dialog._apply()
        self.assertEqual(len(completed_prompt), 1)
        self.assertEqual(
            completed_prompt[0],
            "Escribe tests unitarios para auth_service considerando tokens expirados y credenciales vacías.",
        )

    def test_04_workspace_prompt_library_dialog_insertion(self) -> None:
        """Flujo en WorkspaceView: abrir biblioteca, seleccionar plantilla e insertar en editor."""
        tmpl = self.ctx.prompt_templates.create(
            name="Plantilla Directa",
            content="Analiza el rendimiento de la consulta SQL.",
            category="Optimización",
            scope=PromptTemplateScope.GLOBAL,
        )

        view = WorkspaceView(self.nav, self.p1)
        self._pump()

        # Editor vacío inicialmente
        self.assertEqual(view.state.prompt_draft, "")

        # Abrir biblioteca
        view._open_prompt_library()
        dialog: PromptLibraryDialog = view._last_dialog
        self.assertIsNotNone(dialog)
        self._pump()

        # Seleccionar plantilla e insertar directamente
        dialog._select_template(tmpl)
        dialog._use_selected()
        self._pump()

        # El editor ahora contiene el texto de la plantilla
        self.assertEqual(view.state.prompt_draft, "Analiza el rendimiento de la consulta SQL.")


    def test_05_workspace_confirm_replace_existing_draft(self) -> None:
        """Si el editor ya tiene texto, cancelar conserva el borrador anterior."""
        self.ctx.prompt_templates.create(
            name="Plantilla Sobrescritura",
            content="Nuevo contenido desde plantilla.",
            scope=PromptTemplateScope.GLOBAL,
        )

        view = WorkspaceView(self.nav, self.p1)
        self._pump()

        # Escribir borrador previo
        view._prompt.get_buffer().set_text("Mi borrador importante previo.")
        self._pump()
        self.assertEqual(view.state.prompt_draft, "Mi borrador importante previo.")

        # Intentar insertar plantilla
        view._on_template_selected("Nuevo contenido desde plantilla.")
        self._pump()

        # Surge diálogo de confirmación
        alert: Adw.AlertDialog = view._last_dialog
        self.assertIsInstance(alert, Adw.AlertDialog)

        # Simular Cancelar: el borrador debe permanecer intacto
        alert.emit("response", "cancel")
        self._pump()
        self.assertEqual(view.state.prompt_draft, "Mi borrador importante previo.")

        # Simular Reemplazar: el borrador cambia
        alert.emit("response", "replace")
        self._pump()
        self.assertEqual(view.state.prompt_draft, "Nuevo contenido desde plantilla.")


    def test_06_workspace_save_draft_as_template(self) -> None:
        """Acción 'Guardar como plantilla' desde el editor del Workspace."""
        view = WorkspaceView(self.nav, self.p1)
        self._pump()

        # Botón deshabilitado si el editor está vacío
        self.assertFalse(view._save_template_btn.get_sensitive())

        # Escribir texto
        view._prompt.get_buffer().set_text("Prompt reutilizable con {{param}}.")
        self._pump()
        self.assertTrue(view._save_template_btn.get_sensitive())

        # Abrir diálogo para guardar
        view._save_draft_as_template()
        editor: TemplateEditorDialog = view._last_dialog
        self.assertIsInstance(editor, TemplateEditorDialog)

        # El contenido se pre-cargó
        start, end = editor.text_view.get_buffer().get_bounds()
        text = editor.text_view.get_buffer().get_text(start, end, True)
        self.assertEqual(text, "Prompt reutilizable con {{param}}.")

        # Guardar como plantilla del proyecto
        editor.name_entry.set_text("Mi Plantilla Local")
        editor._save()
        self._pump()

        # Verificar que se creó y que está en la base de datos
        project_templates = self.ctx.prompt_templates.list_for_project(self.p1.id)
        self.assertTrue(any(t.name == "Mi Plantilla Local" for t in project_templates))


    def test_07_transfer_with_template_creates_task(self) -> None:
        """Compatibilidad con tareas: plantilla -> editor -> transferencia -> tarea."""
        tmpl = self.ctx.prompt_templates.create(
            name="Plantilla Flujo Completo",
            content="Implementar endpoint REST para {{recurso}}.",
            scope=PromptTemplateScope.GLOBAL,
        )

        rendered = self.ctx.prompt_templates.render(tmpl.content, {"recurso": "usuarios"})

        # Transferencia oficial
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt=rendered,
            session_id="sess-tmpl-1",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task)
        self.assertEqual(task.prompt, "Implementar endpoint REST para usuarios.")
        self.assertEqual(task.status, TaskState.IN_PROGRESS)

    def test_08_theme_compatibility(self) -> None:
        """Verifica compatibilidad con temas Catppuccin Latte y Mocha."""
        style_mgr = Adw.StyleManager.get_default()
        page = TasksPage(self.nav)

        # Modo claro (Latte)
        style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        page.refresh()
        self._pump()
        self.assertTrue(style_mgr.get_color_scheme() == Adw.ColorScheme.FORCE_LIGHT)

        # Modo oscuro (Mocha)
        style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        page.refresh()
        self._pump()
        self.assertTrue(style_mgr.get_color_scheme() == Adw.ColorScheme.FORCE_DARK)
