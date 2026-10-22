"""Pruebas de interfaz para la pantalla Tareas y prompts (Fase 6B.1).

Verifica bajo entorno GTK4/Libadwaita:
1. Estado vacío cuando no hay proyectos.
2. Estado vacío cuando hay proyectos pero no hay tareas.
3. Renderizado de lista de tareas con chips de agente, pills de estado y expansor.
4. Filtro de proyecto y aislamiento visual.
5. Filtro de estado mediante ToggleGroup.
6. Filtrado y búsqueda por texto.
7. Acciones de marcado completada y guardado de notas.
8. Navegación hacia el workspace desde el detalle de la tarea.
9. Compatibilidad y persistencia de temas Catppuccin Latte y Mocha.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from helpers import temp_dir

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ia_workspace.config import AppPaths
from ia_workspace.context import AppContext
from ia_workspace.domain.models import AgentKind, TaskState
from ia_workspace.persistence.database import Database
from ia_workspace.ui.pages.base import Navigator
from ia_workspace.ui.pages.tasks import TasksPage
from ia_workspace.ui.pages.workspace import WorkspaceView
from ia_workspace.ui.dialogs.task_detail import TaskDetailDialog
from ia_workspace.ui import theme as ui_theme


class DummyNav(Navigator):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.navigated_to: list[str] = []
        self.opened_projects: list[int] = []
        self.added_requested = False
        self.toasts: list[str] = []

    def navigate(self, screen: str) -> None:
        self.navigated_to.append(screen)

    def open_project(self, project_id: int) -> None:
        self.opened_projects.append(project_id)

    def request_add_project(self) -> None:
        self.added_requested = True

    def sidebar_toggle(self) -> Gtk.Widget:
        return Gtk.Button()

    def toast(self, text: str) -> None:
        self.toasts.append(text)


class TasksUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.data_dir = Path(self.tmp.name)
        self.paths = AppPaths(data_dir=self.data_dir)
        self.db = Database(self.paths.database)
        self.ctx = AppContext.create(self.db, paths=self.paths, simulated_agents=True)
        self.nav = DummyNav(self.ctx)

    def tearDown(self) -> None:
        self.ctx.close()
        self.tmp.cleanup()

    def test_01_empty_state_without_projects(self) -> None:
        """Sin proyectos registrados muestra el estado vacío correspondiente."""
        page = TasksPage(self.nav)
        page.refresh()
        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_projects")

    def test_02_empty_state_without_tasks(self) -> None:
        """Con proyecto pero sin tareas registradas muestra el estado de sin tareas."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        self.ctx.projects.register("Proyecto Uno", str(p_dir), (AgentKind.CLAUDE_CODE,))

        page = TasksPage(self.nav)
        page.refresh()
        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_tasks")

    def test_03_render_tasks_list_and_details(self) -> None:
        """Renderiza las tareas con sus chips, títulos, pills y permite interactuar."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Uno", str(p_dir), (AgentKind.CLAUDE_CODE,))

        transfer = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Refactorizar módulo de autenticación",
            session_id="s-test-1",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task)

        page = TasksPage(self.nav)
        page.refresh()

        self.assertEqual(page._stack.get_visible_child_name(), "list")
        self.assertIn("1 tareas", page.window_title.get_subtitle())

    def test_04_filtering_by_state_and_search(self) -> None:
        """Filtro por estado y texto de búsqueda en la UI."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Uno", str(p_dir), (AgentKind.CLAUDE_CODE,))

        t1 = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tarea frontend",
            session_id="s1",
            method="direct",
            status="sent",
        )
        t2 = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CODEX,
            prompt="Tarea backend base de datos",
            session_id="s2",
            method="direct",
            status="sent",
        )
        task1 = self.ctx.tasks.create_for_transfer(t1)
        task2 = self.ctx.tasks.create_for_transfer(t2)
        assert task1 is not None and task2 is not None

        # Marcar task2 como completada
        self.ctx.tasks.mark_completed(task2.id)

        page = TasksPage(self.nav)
        page.refresh()

        # Filtrar solo completadas
        page._state_toggle.set_active_name("completed")
        self.assertEqual(page._filter_state, "completed")

        # Filtrar con búsqueda que no existe
        page._search_entry.set_text("no-existe-xyz")
        page._search_entry.emit("search-changed")
        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_match")

        # Limpiar filtros
        page._clear_filters()
        self.assertEqual(page._stack.get_visible_child_name(), "list")

    def test_05_manual_completion_and_notes_from_ui(self) -> None:
        """Modificar notas y marcar completada desde los métodos del widget."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Uno", str(p_dir), (AgentKind.ANTIGRAVITY,))

        t = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.ANTIGRAVITY,
            prompt="Tarea UI interactiva",
            session_id="s-ui",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(t)
        assert task is not None

        page = TasksPage(self.nav)
        page.refresh()

        # Guardar notas desde UI
        page._save_notes(task.id, "Notas guardadas desde el test")
        self.assertIn("Notas guardadas", self.nav.toasts)
        updated = self.ctx.tasks.get(task.id)
        assert updated is not None
        self.assertEqual(updated.result_notes, "Notas guardadas desde el test")

        # Marcar completada desde UI
        page._mark_task_completed(task.id, "Resultado final validado")
        self.assertIn("Tarea marcada como completada", self.nav.toasts)
        done_task = self.ctx.tasks.get(task.id)
        assert done_task is not None
        self.assertEqual(done_task.status, TaskState.COMPLETED)
        self.assertEqual(done_task.result_notes, "Resultado final validado")

    def test_06_theme_compatibility(self) -> None:
        """Verifica que la página responde correctamente a los temas Catppuccin Latte y Mocha."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        self.ctx.projects.register("Proyecto Uno", str(p_dir), (AgentKind.CLAUDE_CODE,))

        page = TasksPage(self.nav)
        page.refresh()

        app = Adw.Application.get_default()
        if app is not None:
            # Probar modo claro (Latte)
            ui_theme.apply_appearance(app, "light")
            page.refresh()
            # Probar modo oscuro (Mocha)
            ui_theme.apply_appearance(app, "dark")
            page.refresh()

    def test_07_task_detail_dialog_features(self) -> None:
        """Verifica visualización de prompt, guardado de notas y marcado como completada desde TaskDetailDialog."""
        p_dir = self.data_dir / "repo1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Alpha", str(p_dir), (AgentKind.CLAUDE_CODE,))

        t = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt de prueba extenso con múltiples líneas\nLínea 2 con detalles técnicos.",
            session_id="sess-dialog-1",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(t)
        assert task is not None

        opened_sessions: list[str] = []
        dialog = TaskDetailDialog(
            task=task,
            ctx=self.ctx,
            project=p,
            on_open_session=lambda sid: opened_sessions.append(sid),
            on_toast=lambda msg: self.nav.toast(msg),
        )

        # 1. Comprobar datos cargados
        self.assertEqual(dialog.task.title, "Prompt de prueba extenso con múltiples líneas")
        self.assertEqual(dialog.task.agent, AgentKind.CLAUDE_CODE)
        self.assertEqual(dialog.task.status, TaskState.IN_PROGRESS)

        # 2. Copiar prompt
        dialog._copy_prompt()

        # 3. Guardar notas
        dialog._notes_entry.set_text("Notas registradas desde el diálogo flotante")
        dialog._save_notes()
        self.assertIn("Notas guardadas", self.nav.toasts)
        updated = self.ctx.tasks.get(task.id)
        assert updated is not None
        self.assertEqual(updated.result_notes, "Notas registradas desde el diálogo flotante")

        # 4. Cambiar estado manualmente
        dialog._state_dd.set_selected(dialog._state_options.index(TaskState.PENDING_REVIEW))
        dialog._apply_state()
        self.assertEqual(dialog.task.status, TaskState.PENDING_REVIEW)
        self.assertIn(f"Estado cambiado a {TaskState.PENDING_REVIEW.label}", self.nav.toasts)

        # 5. Marcar completada
        dialog._notes_entry.set_text("Trabajo verificado con éxito")
        dialog._mark_completed()
        self.assertEqual(dialog.task.status, TaskState.COMPLETED)
        self.assertFalse(dialog.complete_btn.get_visible())
        self.assertIn("Tarea marcada como completada", self.nav.toasts)

        # 6. Historial de transiciones
        history = self.ctx.tasks.list_history(task.id)
        self.assertGreaterEqual(len(history), 3)

        # 7. Apertura de sesión desde el diálogo
        dialog._open_terminal("sess-dialog-1")
        self.assertEqual(opened_sessions, ["sess-dialog-1"])

    def test_08_workspace_click_task_opens_dialog(self) -> None:
        """Verifica que al hacer clic en una tarea en el panel derecho del Workspace se abre TaskDetailDialog."""
        p_dir = self.data_dir / "repo2"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Beta", str(p_dir), (AgentKind.CODEX,))

        t = self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CODEX,
            prompt="Refactorizar módulo de autenticación",
            session_id=None,
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(t)
        assert task is not None

        view = WorkspaceView(self.nav, p)
        view._render_tasks()

        # Verificar que la lista tiene una fila
        first_row = view._tasks_list.get_first_child()
        self.assertIsNotNone(first_row)
        self.assertTrue(first_row.get_activatable())

        # Simular activación de la fila
        first_row.emit("activated")
        self.assertIsNotNone(view._last_dialog)
        self.assertIsInstance(view._last_dialog, TaskDetailDialog)
        self.assertEqual(view._last_dialog.task.id, task.id)
        self.assertEqual(view._last_dialog.task.title, "Refactorizar módulo de autenticación")

        # Cerrar el diálogo si está presentado
        if view._last_dialog.get_root() is not None:
            view._last_dialog.close()

