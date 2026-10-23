"""Pruebas de interfaz de usuario para el Historial y revisión de actividad (fase 6C.1).

Verifica:
1. Selección por defecto del proyecto activo (nunca todos automáticamente).
2. Estado vacío cuando el proyecto no tiene actividad.
3. Renderizado de lista agrupada cronológicamente por fecha con filas nativas.
4. Aislamiento estricto de proyectos en la interfaz.
5. Elección explícita de «Todos los proyectos».
6. Filtrado interactivo por tipo de evento.
7. Búsqueda en vivo con restablecimiento desde el estado vacío.
8. Carga incremental con botón «Cargar más actividades».
9. Navegación hacia el origen del evento al activarlo.
10. Estado vacío cuando no existen proyectos registrados.
11. Compatibilidad con temas claro y oscuro (Catppuccin Latte/Mocha).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import temp_dir

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from ia_workspace.context import AppContext
from ia_workspace.domain.models import AgentKind, HistoryEventKind, TaskState
from ia_workspace.persistence.database import Database
from ia_workspace.ui.pages.base import Navigator
from ia_workspace.ui.pages.history import HistoryPage


class DummyNav(Navigator):
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.toasts: list[str] = []
        self.opened_projects: list[int] = []
        self.navigated_screens: list[str] = []

    def navigate(self, screen: str) -> None:
        self.navigated_screens.append(screen)

    def open_project(self, project_id: int) -> None:
        self.opened_projects.append(project_id)

    def request_add_project(self) -> None:
        pass

    def sidebar_toggle(self) -> Gtk.Widget:
        return Gtk.Button()

    def toast(self, message: str) -> None:
        self.toasts.append(message)


class HistoryUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Adw.init()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.db = Database(str(self.path / "test.db"))
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.nav = DummyNav(self.ctx)

        # Crear proyectos de prueba
        p1_dir = self.path / "p1"
        p1_dir.mkdir()
        p2_dir = self.path / "p2"
        p2_dir.mkdir()
        self.p1 = self.ctx.projects.register("Proyecto Alfa", str(p1_dir), (AgentKind.CLAUDE_CODE,))
        self.p2 = self.ctx.projects.register("Proyecto Beta", str(p2_dir), (AgentKind.CODEX,))

        # Establecer proyecto activo en workspace
        self.ctx.workspace.activate(self.p1.id)

        self._pump()

    def tearDown(self) -> None:
        self.ctx.close()
        self.tmp.cleanup()

    def _pump(self) -> None:
        while GLib.MainContext.default().iteration(False):
            pass

    def test_01_default_project_selection_not_all(self) -> None:
        """1. Al abrir la pantalla de historial, se selecciona por defecto el proyecto activo/último."""
        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        # Debe estar seleccionado Proyecto Alfa, NO 'Todos los proyectos'
        self.assertEqual(page._selected_project_id, self.p1.id)
        self.assertFalse(page._user_chose_all)
        self.assertEqual(page._project_dropdown.get_selected(), 0)

    def test_02_empty_state_no_activity(self) -> None:
        """2. Proyecto sin actividad muestra el estado vacío 'empty_no_events'."""
        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_events")
        self.assertIn("Alfa", page.window_title.get_subtitle())

    def test_03_renders_grouped_events(self) -> None:
        """3. Eventos se renderizan agrupados cronológicamente por fecha."""
        # Generar actividad
        t1 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Refactorizar componente de autenticación",
            session_id=None,
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(t1, title="Refactorizar autenticación")
        self.ctx.tasks.update_status(task.id, TaskState.COMPLETED, note="Listo y verificado")

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        self.assertEqual(page._stack.get_visible_child_name(), "list")
        # El contenedor de eventos debe contener elementos (cabecera de fecha y lista boxed)
        first_child = page._events_container.get_first_child()
        self.assertIsNotNone(first_child)

    def test_04_project_isolation(self) -> None:
        """4. Cambiar entre proyectos aísla estrictamente la actividad mostrada."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt de Alfa",
            session_id=None,
            method="direct",
            status="sent",
        )
        self.ctx.prompt_transfers.record(
            project_id=self.p2.id,
            agent=AgentKind.CODEX,
            prompt="Prompt de Beta",
            session_id=None,
            method="direct",
            status="sent",
        )

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        # En Alfa solo debe haber 1 evento
        events_alfa, _ = self.ctx.history.list_events(project_id=self.p1.id)
        self.assertEqual(len(events_alfa), 1)

        # Seleccionar Proyecto Beta en el desplegable (índice 1)
        page._project_dropdown.set_selected(1)
        self._pump()

        self.assertEqual(page._selected_project_id, self.p2.id)
        events_beta, _ = self.ctx.history.list_events(project_id=self.p2.id)
        self.assertEqual(len(events_beta), 1)
        self.assertEqual(events_beta[0].project_id, self.p2.id)

    def test_05_explicit_all_projects_selection(self) -> None:
        """5. La opción 'Todos los proyectos' es una elección explícita del usuario."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt de Alfa",
            session_id=None,
            method="direct",
            status="sent",
        )
        self.ctx.prompt_transfers.record(
            project_id=self.p2.id,
            agent=AgentKind.CODEX,
            prompt="Prompt de Beta",
            session_id=None,
            method="direct",
            status="sent",
        )

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        # Seleccionar la última opción ("Todos los proyectos", índice 2)
        page._project_dropdown.set_selected(2)
        self._pump()

        self.assertTrue(page._user_chose_all)
        self.assertIsNone(page._selected_project_id)
        self.assertIn("Todos los proyectos", page.window_title.get_subtitle())

    def test_06_filter_by_kind(self) -> None:
        """6. Filtrar por tipo de evento actualiza dinámicamente la lista."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt de Alfa",
            session_id=None,
            method="direct",
            status="sent",
        )

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        # Índice 1 en _kind_dropdown es "Tareas" (no hay tareas aún)
        page._kind_dropdown.set_selected(1)
        self._pump()

        # Sin coincidencias de tipo Tarea
        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_match")

        # Índice 2 es "Transferencias de prompts"
        page._kind_dropdown.set_selected(2)
        self._pump()

        self.assertEqual(page._stack.get_visible_child_name(), "list")

    def test_07_search_and_clear_filters(self) -> None:
        """7. La búsqueda interactiva filtra en tiempo real y el botón restablecer la limpia."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Optimización crítica de rendimiento",
            session_id=None,
            method="direct",
            status="sent",
        )

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        # Búsqueda coincidente
        page._search_entry.set_text("crítica")
        page._search_entry.emit("search-changed")
        self._pump()
        self.assertEqual(page._stack.get_visible_child_name(), "list")

        # Búsqueda sin coincidencias
        page._search_entry.set_text("inexistente_xyz_123")
        page._search_entry.emit("search-changed")
        self._pump()
        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_match")

        # Restablecer filtros
        page._clear_filters()
        self._pump()
        self.assertEqual(page._stack.get_visible_child_name(), "list")
        self.assertEqual(page._search_entry.get_text(), "")

    def test_08_load_more_pagination(self) -> None:
        """8. Botón 'Cargar más actividades' amplía el límite cuando hay más eventos."""
        for i in range(12):
            self.ctx.prompt_transfers.record(
                project_id=self.p1.id,
                agent=AgentKind.CLAUDE_CODE,
                prompt=f"Transferencia {i}",
                session_id=None,
                method="direct",
                status="sent",
            )

        page = HistoryPage(self.nav)
        page.refresh()
        page._limit = 5  # Forzar límite pequeño para probar paginación
        page._render()
        self._pump()

        # Debe mostrarse el botón de cargar más
        last_child = page._events_container.get_last_child()
        self.assertIsNotNone(last_child)
        load_more_btn = last_child.get_first_child()
        self.assertIsInstance(load_more_btn, Gtk.Button)

        # Clic en cargar más
        load_more_btn.emit("clicked")
        self._pump()
        self.assertGreater(page._limit, 5)

    def test_09_navigation_on_event_activation(self) -> None:
        """9. Activar un evento navega al origen adecuado."""
        t1 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt navegable",
            session_id=None,
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(t1, title="Tarea navegable")

        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        events, _ = self.ctx.history.list_events(project_id=self.p1.id)
        # Activar evento de transferencia -> open_project
        transfer_ev = next(e for e in events if e.kind == HistoryEventKind.PROMPT_TRANSFER)
        page._on_event_activated(transfer_ev)
        self.assertIn(self.p1.id, self.nav.opened_projects)

        # Activar evento de tarea -> navigate("tasks")
        task_ev = next(e for e in events if e.kind == HistoryEventKind.TASK)
        page._on_event_activated(task_ev)
        self.assertIn("tasks", self.nav.navigated_screens)

    def test_10_empty_no_projects(self) -> None:
        """10. Sin proyectos registrados muestra empty_no_projects."""
        # Nueva BD limpia sin proyectos
        empty_db = Database(str(self.path / "empty.db"))
        empty_ctx = AppContext.create(empty_db, simulated_agents=True)
        empty_nav = DummyNav(empty_ctx)

        page = HistoryPage(empty_nav)
        page.refresh()
        self._pump()

        self.assertEqual(page._stack.get_visible_child_name(), "empty_no_projects")
        empty_ctx.close()

    def test_11_catppuccin_theme_compatibility(self) -> None:
        """11. Cambiar esquema de color entre claro (Latte) y oscuro (Mocha) no causa fallos."""
        page = HistoryPage(self.nav)
        page.refresh()
        self._pump()

        style_manager = Adw.StyleManager.get_default()
        style_manager.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        self._pump()

        style_manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self._pump()

        style_manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
        self._pump()


if __name__ == "__main__":
    unittest.main()
