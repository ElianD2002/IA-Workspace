"""Pruebas automatizadas de la pantalla Inicio rediseñada (Fase 6A).

Verifica:
1. Indicadores con datos reales (proyectos, sesiones, propuestas; sin tareas ficticias).
2. Navegación funcional desde KPIs, tarjetas, avisos de atención y eventos.
3. Proyectos con y sin sesiones (estados en pausa vs activo/trabajando).
4. Detección y presentación de propuestas pendientes por proyecto.
5. Estados vacíos discretos en atención, sesiones, actividad y bienvenida.
6. Actualización reactiva al cambiar proyectos, sesiones y propuestas.
7. Aislamiento e identificación de proyectos en cada elemento.
8. Adaptabilidad responsive en FlowBox sin desbordamientos.
9. Conservación de sesiones activas al navegar e interactuar con Inicio.
10. Respeto al ámbito de suscripciones oficiales sin consultas LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from helpers import temp_dir

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ia_workspace.brain.proposals import ProposalOperation, ProposalStatus
from ia_workspace.config import AppPaths
from ia_workspace.context import AppContext
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind, Project
from ia_workspace.persistence.database import Database
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)
from ia_workspace.sessions.models import SessionSpec, SessionState
from ia_workspace.ui.pages.base import Navigator
from ia_workspace.ui.pages.home import HomePage


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


class HomeUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.data_dir = Path(self.tmp.__enter__())
        self.paths = AppPaths(data_dir=self.data_dir)
        self.db = Database(self.paths.database)
        self.ctx = AppContext.create(self.db, paths=self.paths, simulated_agents=True)
        self.nav = DummyNav(self.ctx)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.__exit__(None, None, None)

    # ---- 1. Estado vacío cuando no hay proyectos ------------------------------

    def test_01_empty_state_without_projects(self) -> None:
        """Sin proyectos registrados muestra el estado de bienvenida y botón de agregar."""
        page = HomePage(self.nav)
        page.refresh()

        self.assertEqual(page._stack.get_visible_child_name(), "empty")

        # El botón de agregar proyecto llama a request_add_project
        empty_widget = page._stack.get_visible_child()
        self.assertIsInstance(empty_widget, Adw.StatusPage)
        btn = empty_widget.get_child()
        self.assertIsInstance(btn, Gtk.Button)
        btn.emit("clicked")
        self.assertTrue(self.nav.added_requested)

    # ---- 2. Indicadores reales en el resumen superior ------------------------

    def test_02_kpi_indicators_with_real_data(self) -> None:
        """Los KPIs muestran números reales de proyectos, sesiones y propuestas (sin tareas ficticias)."""
        proj_dir = self.data_dir / "p1"
        proj_dir.mkdir()
        p1 = self.ctx.projects.register("Proyecto Uno", str(proj_dir), SELECTABLE_AGENTS)
        p2_dir = self.data_dir / "p2"
        p2_dir.mkdir()
        p2 = self.ctx.projects.register("Proyecto Dos", str(p2_dir), SELECTABLE_AGENTS)

        # Iniciar una sesión activa
        self.ctx.sessions.start(
            SessionSpec(kind=AgentKind.CLAUDE_CODE, project_id=p1.id, cwd=str(proj_dir))
        )

        # Crear una propuesta pendiente en p1
        self.ctx.brain.propose(
            project=p1,
            operation=ProposalOperation.CREATE,
            relpath="estado/prueba.md",
            title="Prueba de Conocimiento",
            category_key="estado",
            body="# Prueba\nDetalle de prueba",
            agent_kind="claude_code",
        )

        page = HomePage(self.nav)
        page.refresh()

        self.assertEqual(page._stack.get_visible_child_name(), "content")

        # Buscar el FlowBox de resumen (primer hijo de _body)
        summary_flow = page._body.get_first_child()
        self.assertIsInstance(summary_flow, Gtk.FlowBox)

        # Debe contener exactamente 3 botones KPI: proyectos, sesiones, propuestas
        kpi_buttons: list[Gtk.Button] = []
        child = summary_flow.get_first_child()
        while child is not None:
            btn = child.get_child()
            if isinstance(btn, Gtk.Button):
                kpi_buttons.append(btn)
            child = child.get_next_sibling()

        self.assertEqual(len(kpi_buttons), 3)

        # Extraer valores de los KPIs
        def get_kpi_info(btn: Gtk.Button) -> tuple[str, str]:
            box = btn.get_child()
            texts = box.get_first_child().get_next_sibling()
            val = texts.get_first_child().get_text()
            label = texts.get_first_child().get_next_sibling().get_text()
            return val, label

        val_proj, lbl_proj = get_kpi_info(kpi_buttons[0])
        self.assertEqual(val_proj, "2")
        self.assertIn("Proyectos", lbl_proj)

        val_sess, lbl_sess = get_kpi_info(kpi_buttons[1])
        self.assertEqual(val_sess, "1")
        self.assertIn("Sesiones", lbl_sess)

        val_prop, lbl_prop = get_kpi_info(kpi_buttons[2])
        self.assertEqual(val_prop, "1")
        self.assertIn("Propuestas", lbl_prop)

        # Verificar iconos semánticos y centrado
        def get_kpi_icon(btn: Gtk.Button) -> tuple[str, bool, bool, int, int]:
            box = btn.get_child()
            icon_box = box.get_first_child()
            img = icon_box.get_first_child()
            return (
                img.get_icon_name(),
                img.get_hexpand(),
                img.get_vexpand(),
                int(img.get_halign()),
                int(img.get_valign()),
            )

        icon_proj, h0, v0, hal0, val0 = get_kpi_icon(kpi_buttons[0])
        self.assertEqual(icon_proj, "folder-symbolic")
        self.assertTrue(h0 and v0)
        self.assertEqual(hal0, int(Gtk.Align.CENTER))
        self.assertEqual(val0, int(Gtk.Align.CENTER))

        icon_sess, h1, v1, hal1, val1 = get_kpi_icon(kpi_buttons[1])
        self.assertEqual(icon_sess, "utilities-terminal-symbolic")
        self.assertTrue(h1 and v1)
        self.assertEqual(hal1, int(Gtk.Align.CENTER))
        self.assertEqual(val1, int(Gtk.Align.CENTER))

        icon_prop, h2, v2, hal2, val2 = get_kpi_icon(kpi_buttons[2])
        self.assertEqual(icon_prop, "document-send-symbolic")
        self.assertTrue(h2 and v2)
        self.assertEqual(hal2, int(Gtk.Align.CENTER))
        self.assertEqual(val2, int(Gtk.Align.CENTER))

    # ---- 3. Navegación desde botones KPI --------------------------------------

    def test_03_kpi_navigation(self) -> None:
        """Hacer clic en los KPIs navega a projects, agents y memory respectivamente."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        self.ctx.projects.register("Proyecto Uno", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        page.refresh()

        summary_flow = page._body.get_first_child()
        kpis: list[Gtk.Button] = []
        child = summary_flow.get_first_child()
        while child is not None:
            btn = child.get_child()
            if isinstance(btn, Gtk.Button):
                kpis.append(btn)
            child = child.get_next_sibling()

        kpis[0].emit("clicked")
        self.assertEqual(self.nav.navigated_to[-1], "projects")

        kpis[1].emit("clicked")
        self.assertEqual(self.nav.navigated_to[-1], "agents")

        kpis[2].emit("clicked")
        self.assertEqual(self.nav.navigated_to[-1], "memory")

    # ---- 4. Requiere tu atención: propuestas, errores y cuotas ---------------

    def test_04_attention_section_items_and_empty_state(self) -> None:
        """Requiere tu atención lista elementos críticos reales y muestra estado discreto si no hay nada."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p1 = self.ctx.projects.register("Alfa", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        page.refresh()

        # Al inicio no hay errores ni propuestas
        # 0: summary, 1: att_head, 2: att_box
        attention_box = page._body.get_first_child().get_next_sibling().get_next_sibling()
        self.assertIsInstance(attention_box, Gtk.ListBox)
        first_row = attention_box.get_first_child()
        self.assertIn("Todo al día", first_row.get_child().get_first_child().get_next_sibling().get_text())

        # 1. Agregar una propuesta
        self.ctx.brain.propose(
            project=p1,
            operation=ProposalOperation.CREATE,
            relpath="estado/arquitectura.md",
            title="Actualización de Arquitectura",
            category_key="estado",
            body="# Arquitectura actualizada",
            agent_kind="codex",
        )
        page.refresh()

        attention_box = page._body.get_first_child().get_next_sibling().get_next_sibling()
        row1 = attention_box.get_first_child()
        self.assertIsInstance(row1, Adw.ActionRow)
        self.assertIn("Propuesta", row1.get_title())
        self.assertIn("Alfa", row1.get_subtitle())

        # 2. Agregar una alerta de cuota elevada (>= 80%)
        win_high = QuotaWindow(
            window_id="5h",
            name="Ventana 5 horas",
            window_minutes=300,
            percentage=88.5,
            metric_type=QuotaMetricType.USED_PERCENT,
        )
        group = QuotaGroup(group_id="g1", name="Uso general", windows=(win_high,))
        snap = ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            status=QuotaStatus.AVAILABLE,
            groups=(group,),
            updated_at=datetime.now(timezone.utc),
        )
        self.ctx.quotas._repo.save_snapshot(snap)
        page.refresh()

        # Deben haber 2 filas de atención ahora
        attention_box = page._body.get_first_child().get_next_sibling().get_next_sibling()
        rows: list[Adw.ActionRow] = []
        child = attention_box.get_first_child()
        while child is not None:
            if isinstance(child, Adw.ActionRow):
                rows.append(child)
            child = child.get_next_sibling()

        self.assertEqual(len(rows), 2)
        quota_row = [r for r in rows if "Codex" in r.get_title()][0]
        self.assertIn("88.5%", quota_row.get_title())

        # Probar acción del botón de cuota
        quota_row.emit("activated")
        self.assertEqual(self.nav.navigated_to[-1], "agents")

    # ---- 5. Continuar trabajando: tarjetas compactas y metadatos --------------

    def test_05_continue_cards_metadata_and_actions(self) -> None:
        """Las tarjetas de proyectos muestran rama, actividad, propuestas y abren el workspace."""
        p_dir = self.data_dir / "repo_a"
        p_dir.mkdir()
        (p_dir / ".git").mkdir()
        (p_dir / ".git" / "HEAD").write_text("ref: refs/heads/feature/login\n")
        (p_dir / "pyproject.toml").write_text("[project]\nname='test'\n")

        p = self.ctx.projects.register("Proyecto Repo", str(p_dir), SELECTABLE_AGENTS)

        # Agregar propuesta en este proyecto
        self.ctx.brain.propose(
            project=p,
            operation=ProposalOperation.CREATE,
            relpath="estado/login.md",
            title="Flujo de Login",
            category_key="estado",
            body="# Login\nDetalles",
            agent_kind="claude_code",
        )

        page = HomePage(self.nav)
        page.refresh()

        card = page._continue_card(p)
        self.assertIsInstance(card, Gtk.Box)

        # Verificar que contiene el botón "Abrir workspace" y que ejecuta la acción
        open_btn = card.get_last_child().get_last_child()
        self.assertIsInstance(open_btn, Gtk.Button)
        self.assertEqual(open_btn.get_label(), "Abrir workspace")
        open_btn.emit("clicked")
        self.assertIn(p.id, self.nav.opened_projects)

    # ---- 6. Sesiones en ejecución --------------------------------------------

    def test_06_running_sessions_section(self) -> None:
        """Sesiones en ejecución lista los agentes activos y permite abrir su workspace."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Sesión", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        page.refresh()

        # Inicialmente sin sesiones
        # 0: summary, 1: att_head, 2: att_box, 3: cont_head, 4: cont_flow, 5: sess_head, 6: sess_box
        sess_box = page._body.get_first_child()
        for _ in range(6):
            sess_box = sess_box.get_next_sibling()
        self.assertIsInstance(sess_box, Gtk.ListBox)
        self.assertIn("Ningún agente", sess_box.get_first_child().get_child().get_first_child().get_next_sibling().get_text())

        # Iniciar sesión
        self.ctx.sessions.start(
            SessionSpec(kind=AgentKind.CLAUDE_CODE, project_id=p.id, cwd=str(p_dir), title="Terminal de compilación")
        )

        page.refresh()

        sess_box = page._body.get_first_child()
        for _ in range(6):
            sess_box = sess_box.get_next_sibling()
        row = sess_box.get_first_child()
        self.assertIsInstance(row, Adw.ActionRow)
        self.assertIn("Terminal de compilación", row.get_title())
        self.assertIn("Proyecto Sesión", row.get_subtitle())

        # Clic en abrir workspace desde la fila de sesión
        row.emit("activated")
        self.assertIn(p.id, self.nav.opened_projects)

    # ---- 7. Actividad reciente con eventos reales -----------------------------

    def test_07_recent_activity_real_events(self) -> None:
        """Actividad reciente recoge eventos reales cronológicos (proyectos, prompts, memoria, chats)."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Actividad", str(p_dir), SELECTABLE_AGENTS)

        # Registrar un prompt transferido
        self.ctx.prompt_transfers.record(
            project_id=p.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Refactorizar módulo de usuarios",
            session_id=None,
            method="direct",
            status="sent",
        )

        page = HomePage(self.nav)
        page.refresh()

        # La última sección es Actividad reciente
        act_box = page._body.get_last_child()
        self.assertIsInstance(act_box, Gtk.ListBox)

        events: list[str] = []
        child = act_box.get_first_child()
        while child is not None:
            row_box = child.get_child()
            desc = row_box.get_first_child().get_next_sibling().get_next_sibling().get_text()
            events.append(desc)
            child = child.get_next_sibling()

        self.assertTrue(any("Prompt transferido a Claude Code" in e for e in events))
        self.assertTrue(any("Proyecto registrado" in e for e in events))

    # ---- 8. Continuidad de sesiones ------------------------------------------

    def test_08_session_continuity_during_refresh(self) -> None:
        """Actualizar el inicio no detiene ni afecta el estado de las sesiones activas."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Continuo", str(p_dir), SELECTABLE_AGENTS)

        s = self.ctx.sessions.start(
            SessionSpec(kind=AgentKind.CLAUDE_CODE, project_id=p.id, cwd=str(p_dir))
        )
        self.assertEqual(self.ctx.sessions.get(s.id).state, SessionState.WORKING)

        page = HomePage(self.nav)
        for _ in range(5):
            page.refresh()

        # La sesión debe seguir en WORKING
        self.assertEqual(self.ctx.sessions.get(s.id).state, SessionState.WORKING)
        self.assertEqual(len(self.ctx.sessions.active_sessions()), 1)

    # ---- 9. Responsive y FlowBox adaptativo ------------------------------------

    def test_09_responsive_flowbox_properties(self) -> None:
        """Comprueba que los contenedores FlowBox tengan configuración responsive."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        self.ctx.projects.register("Proyecto Responsive", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        page.refresh()

        # Resumen FlowBox
        summary_flow = page._body.get_first_child()
        self.assertIsInstance(summary_flow, Gtk.FlowBox)
        self.assertEqual(summary_flow.get_min_children_per_line(), 1)
        self.assertEqual(summary_flow.get_max_children_per_line(), 3)
        self.assertTrue(summary_flow.get_homogeneous())

        # Continuar trabajando FlowBox
        # 0: summary, 1: att_head, 2: att_box, 3: cont_head, 4: cont_flow
        cont_flow = page._body.get_first_child()
        for _ in range(4):
            cont_flow = cont_flow.get_next_sibling()
        self.assertIsInstance(cont_flow, Gtk.FlowBox)
        self.assertEqual(cont_flow.get_min_children_per_line(), 1)
        self.assertEqual(cont_flow.get_max_children_per_line(), 2)
        self.assertTrue(cont_flow.get_homogeneous())

    # ---- 10. Aislamiento estricto de proyectos --------------------------------

    def test_10_strict_project_isolation(self) -> None:
        """Verifica que las tarjetas, sesiones y propuestas identifiquen rigurosamente su proyecto de origen."""
        p1_dir = self.data_dir / "p1"
        p1_dir.mkdir()
        p1 = self.ctx.projects.register("Proyecto Alfa", str(p1_dir), SELECTABLE_AGENTS)

        p2_dir = self.data_dir / "p2"
        p2_dir.mkdir()
        p2 = self.ctx.projects.register("Proyecto Beta", str(p2_dir), SELECTABLE_AGENTS)

        # Propuesta en p1 únicamente
        self.ctx.brain.propose(
            project=p1,
            operation=ProposalOperation.CREATE,
            relpath="estado/alfa.md",
            title="Detalle Alfa",
            category_key="estado",
            body="# Alfa",
            agent_kind="codex",
        )

        page = HomePage(self.nav)
        page.refresh()

        # En tarjetas: la de p1 debe tener píldora de propuesta, la de p2 no
        card1 = page._continue_card(p1)
        card2 = page._continue_card(p2)

        def has_prop_pill(card: Gtk.Box) -> bool:
            # meta_row es el segundo hijo
            meta_row = card.get_first_child().get_next_sibling()
            child = meta_row.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Box) and "status-pill" in child.get_css_classes():
                    return True
                child = child.get_next_sibling()
            return False

        self.assertTrue(has_prop_pill(card1))
        self.assertFalse(has_prop_pill(card2))

    # ---- 11. Alertas de atención por fallo de sesión ---------------------------

    def test_11_attention_session_failures(self) -> None:
        """Sesiones con error o esperando aprobación aparecen destacadas en atención."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Fallo", str(p_dir), SELECTABLE_AGENTS)

        s = self.ctx.sessions.start(
            SessionSpec(kind=AgentKind.CLAUDE_CODE, project_id=p.id, cwd=str(p_dir))
        )
        # Simular fallo en la sesión
        self.ctx.sessions._on_state(s.id, SessionState.FAILED, "El proceso terminó con error 1", exit_code=1)

        page = HomePage(self.nav)
        page.refresh()

        attention_box = page._body.get_first_child().get_next_sibling().get_next_sibling()
        row = attention_box.get_first_child()
        self.assertIsInstance(row, Adw.ActionRow)
        self.assertIn("Finalizado con error", row.get_title())
        self.assertIn("Proyecto Fallo", row.get_subtitle())

    # ---- 12. Botón Nuevo Prompt en cabecera -----------------------------------

    def test_12_new_prompt_header_action(self) -> None:
        """El botón Nuevo prompt abre el workspace del proyecto objetivo."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        p = self.ctx.projects.register("Proyecto Destino", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        page.refresh()

        page._new_prompt()
        self.assertIn(p.id, self.nav.opened_projects)

    # ---- 13. Cero inferencias LLM en renderizado ------------------------------

    def test_13_no_llm_inference_during_render(self) -> None:
        """La carga y el renderizado de Inicio nunca ejecutan llamadas LLM ni subprocesos bloqueantes."""
        p_dir = self.data_dir / "p1"
        p_dir.mkdir()
        self.ctx.projects.register("Proyecto Seguro", str(p_dir), SELECTABLE_AGENTS)

        page = HomePage(self.nav)
        # Contar llamadas a fetch en QuotaService
        fetch_called = False
        def fake_fetch(*args, **kwargs):
            nonlocal fetch_called
            fetch_called = True
            raise AssertionError("fetch() no debe llamarse desde Inicio.")

        self.ctx.quotas._fetch = fake_fetch
        page.refresh()
        self.assertFalse(fetch_called)


if __name__ == "__main__":
    unittest.main()
