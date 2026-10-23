"""Pruebas unitarias para el módulo de Historial y revisión de actividad (Fase 6C.1).

Verifica:
1. Consulta inicial vacía (sin eventos).
2. Trazabilidad de transferencias de prompts.
3. Trazabilidad de tareas (creación y transiciones de estado en task_history).
4. Trazabilidad de propuestas del Cerebro Compartido (creación y resolución).
5. Trazabilidad de conversaciones ChatGPT vinculadas.
6. Aislamiento estricto entre proyectos.
7. Orden cronológico descendente estricto (más reciente primero).
8. Filtros por tipo de evento (HistoryEventKind).
9. Búsqueda por texto (título, descripción, agente, estado).
10. Paginación y control de has_more.
11. Conversión de fecha y hora a zona horaria America/Tegucigalpa.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from helpers import temp_dir

from ia_workspace.context import AppContext
from ia_workspace.domain.models import AgentKind, HistoryEventKind, TaskState
from ia_workspace.persistence.database import Database
from ia_workspace.ui.pages.history import TZ_TEGUCIGALPA, _date_group_title, _to_local_datetime


class TestHistoryModule(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.path = Path(self.tmp.name)
        (self.path / "p1").mkdir()
        (self.path / "p2").mkdir()
        self.db = Database(self.path / "test.db")
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.p1 = self.ctx.projects.register("Proyecto Alfa", str(self.path / "p1"), (AgentKind.CLAUDE_CODE,))
        self.p2 = self.ctx.projects.register("Proyecto Beta", str(self.path / "p2"), (AgentKind.CODEX,))

    def tearDown(self) -> None:
        self.ctx.close()
        self.tmp.cleanup()

    def test_history_empty(self) -> None:
        """1. Consulta inicial sin actividad debe retornar lista vacía y has_more=False."""
        events, has_more = self.ctx.history.list_events(project_id=self.p1.id)
        self.assertEqual(events, [])
        self.assertFalse(has_more)

    def test_history_prompt_transfers(self) -> None:
        """2. Transferencia de prompt genera un evento de tipo PROMPT_TRANSFER."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Refactorizar módulo de autenticación\nSegunda línea de prueba",
            session_id="sim_session_1",
            method="direct",
            status="sent",
        )
        events, has_more = self.ctx.history.list_events(project_id=self.p1.id)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, HistoryEventKind.PROMPT_TRANSFER)
        self.assertEqual(ev.project_id, self.p1.id)
        self.assertEqual(ev.project_name, "Proyecto Alfa")
        self.assertIn("Claude Code", ev.title)
        self.assertEqual(ev.description, "Refactorizar módulo de autenticación")
        self.assertEqual(ev.status, "sent")
        self.assertEqual(ev.status_label, "Enviado")
        self.assertEqual(ev.target_type, "workspace")

    def test_history_tasks_and_updates(self) -> None:
        """3. Tarea creada y transición de estado generan eventos respectivos."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Crear componente de UI",
            session_id="sim_1",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer, title="Crear componente de UI")

        # Actualizar estado de la tarea
        self.ctx.tasks.update_status(
            task.id,
            new_status=TaskState.COMPLETED,
            note="Iniciando desarrollo del componente",
        )

        events, has_more = self.ctx.history.list_events(
            project_id=self.p1.id,
            kind=HistoryEventKind.TASK,
        )
        # Debe haber exactamente 2 eventos de tarea: creación y actualización
        self.assertEqual(len(events), 2)
        update_ev = events[0]  # Más reciente: actualización a completada
        create_ev = events[1]  # Creación original

        self.assertEqual(update_ev.kind, HistoryEventKind.TASK)
        self.assertIn("Actualización de tarea", update_ev.title)
        self.assertIn("Iniciando desarrollo", update_ev.description)
        self.assertEqual(update_ev.status_label, "Completada")

        self.assertEqual(create_ev.kind, HistoryEventKind.TASK)
        self.assertIn("Tarea creada: Crear componente", create_ev.title)
        self.assertEqual(create_ev.status, "created")
        self.assertEqual(create_ev.status_label, "Creada")

    def test_regression_task_created_and_completed_preserves_historical_states(self) -> None:
        """Regresión: una tarea creada y completada conserva ambos eventos con sus estados históricos (Creada y Completada)."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tarea con trazabilidad cronológica",
            session_id="sim_seq_1",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer, title="Trazabilidad histórica")

        # Inmediatamente tras creación: 1 solo evento de tarea en Historial
        events_initial, _ = self.ctx.history.list_events(project_id=self.p1.id, kind=HistoryEventKind.TASK)
        self.assertEqual(len(events_initial), 1)
        self.assertEqual(events_initial[0].title, "Tarea creada: Trazabilidad histórica")
        self.assertEqual(events_initial[0].status, "created")
        self.assertEqual(events_initial[0].status_label, "Creada")

        # Completar la tarea posteriormente
        self.ctx.tasks.update_status(
            task.id,
            new_status=TaskState.COMPLETED,
            note="Verificado satisfactoriamente",
        )

        # Ahora deben existir exactamente 2 eventos de tarea
        events_after, _ = self.ctx.history.list_events(project_id=self.p1.id, kind=HistoryEventKind.TASK)
        self.assertEqual(len(events_after), 2)

        ev_completed = events_after[0]  # Más reciente
        ev_created = events_after[1]    # Anterior

        # El evento posterior debe reflejar la actualización a Completada
        self.assertEqual(ev_completed.title, "Actualización de tarea: Trazabilidad histórica")
        self.assertEqual(ev_completed.status, "completed")
        self.assertEqual(ev_completed.status_label, "Completada")
        self.assertIn("En progreso → Completada", ev_completed.description)
        self.assertIn("Verificado satisfactoriamente", ev_completed.description)

        # El evento de creación DEBE conservar "Creada" y NO mostrar "Completada"
        self.assertEqual(ev_created.title, "Tarea creada: Trazabilidad histórica")
        self.assertEqual(ev_created.status, "created")
        self.assertEqual(ev_created.status_label, "Creada")
        self.assertNotEqual(ev_created.status_label, "Completada")

    def test_history_brain_proposals(self) -> None:
        """4. Propuestas del Cerebro Compartido registran evento de creación y de resolución."""
        # Insertar propuesta directamente en la tabla brain_proposals
        self.db.conn.execute(
            """
            INSERT INTO brain_proposals (id, project_id, project_key, agent_kind, operation, relpath, title, category, status, created_at, reviewed_at, review_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prop-123",
                self.p1.id,
                "alfa",
                "claude_code",
                "create",
                "docs/architecture.md",
                "Documentación de arquitectura",
                "contexto",
                "approved",
                "2026-10-22T10:00:00+00:00",
                "2026-10-22T11:00:00+00:00",
                "Aprobado por el desarrollador",
            ),
        )

        events, has_more = self.ctx.history.list_events(
            project_id=self.p1.id,
            kind=HistoryEventKind.BRAIN_PROPOSAL,
        )
        self.assertEqual(len(events), 2)
        rev_ev = events[0]
        create_ev = events[1]

        self.assertEqual(rev_ev.kind, HistoryEventKind.BRAIN_PROPOSAL)
        self.assertIn("Propuesta aprobada", rev_ev.title)
        self.assertIn("Aprobado por el desarrollador", rev_ev.description)
        self.assertEqual(rev_ev.target_type, "memory")

        self.assertEqual(create_ev.kind, HistoryEventKind.BRAIN_PROPOSAL)
        self.assertIn("Propuesta de memoria", create_ev.title)
        self.assertIn("docs/architecture.md", create_ev.description)

    def test_history_chat_links(self) -> None:
        """5. Enlace a conversación de ChatGPT registra evento CHAT_LINK."""
        self.ctx.chat_links.link(
            project_id=self.p1.id,
            url="https://chatgpt.com/share/6715f2a1-0000-8000-9999-000000000001",
            title="Diseño del esquema de base de datos",
        )
        events, has_more = self.ctx.history.list_events(
            project_id=self.p1.id,
            kind=HistoryEventKind.CHAT_LINK,
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.kind, HistoryEventKind.CHAT_LINK)
        self.assertIn("Diseño del esquema", ev.title)
        self.assertIn("https://chatgpt.com/share/6715f2a1-0000-8000-9999-000000000001", ev.description)
        self.assertEqual(ev.target_type, "workspace")

    def test_history_project_isolation(self) -> None:
        """6. Aislamiento entre proyectos: filtrar por Proyecto 1 nunca muestra eventos de Proyecto 2."""
        # Actividad en proyecto 1
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt de Alfa",
            session_id=None,
            method="direct",
            status="sent",
        )
        # Actividad en proyecto 2
        self.ctx.prompt_transfers.record(
            project_id=self.p2.id,
            agent=AgentKind.CODEX,
            prompt="Prompt de Beta",
            session_id=None,
            method="direct",
            status="sent",
        )

        ev_p1, _ = self.ctx.history.list_events(project_id=self.p1.id)
        ev_p2, _ = self.ctx.history.list_events(project_id=self.p2.id)
        ev_all, _ = self.ctx.history.list_events(project_id=None)

        self.assertEqual(len(ev_p1), 1)
        self.assertEqual(ev_p1[0].project_id, self.p1.id)
        self.assertIn("Prompt de Alfa", ev_p1[0].description)

        self.assertEqual(len(ev_p2), 1)
        self.assertEqual(ev_p2[0].project_id, self.p2.id)
        self.assertIn("Prompt de Beta", ev_p2[0].description)

        self.assertEqual(len(ev_all), 2)

    def test_history_chronological_order(self) -> None:
        """7. Eventos se ordenan en orden cronológico estricto descendente (más reciente primero)."""
        # Registrar 3 transferencias espaciadas
        t1 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Primer evento",
            session_id=None,
            method="direct",
            status="sent",
        )
        t2 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Segundo evento",
            session_id=None,
            method="direct",
            status="sent",
        )
        t3 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tercer evento",
            session_id=None,
            method="direct",
            status="sent",
        )
        events, _ = self.ctx.history.list_events(project_id=self.p1.id)
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0].created_at >= events[1].created_at >= events[2].created_at)
        self.assertIn("Tercer evento", events[0].description)
        self.assertIn("Primer evento", events[2].description)

    def test_history_kind_filter(self) -> None:
        """8. Filtrado por kind devuelve exclusivamente eventos del tipo solicitado."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Prompt transfer",
            session_id=None,
            method="direct",
            status="sent",
        )
        self.ctx.chat_links.link(
            project_id=self.p1.id,
            url="https://chatgpt.com/share/6715f2a1-0000-8000-9999-000000000002",
            title="Chat link",
        )

        tasks_ev, _ = self.ctx.history.list_events(project_id=self.p1.id, kind=HistoryEventKind.TASK)
        transfers_ev, _ = self.ctx.history.list_events(project_id=self.p1.id, kind=HistoryEventKind.PROMPT_TRANSFER)
        chats_ev, _ = self.ctx.history.list_events(project_id=self.p1.id, kind=HistoryEventKind.CHAT_LINK)

        self.assertEqual(len(tasks_ev), 0)
        self.assertEqual(len(transfers_ev), 1)
        self.assertEqual(transfers_ev[0].kind, HistoryEventKind.PROMPT_TRANSFER)
        self.assertEqual(len(chats_ev), 1)
        self.assertEqual(chats_ev[0].kind, HistoryEventKind.CHAT_LINK)

    def test_history_search(self) -> None:
        """9. Búsqueda por texto filtra correctamente por coincidencia parcial."""
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Optimizar consultas SQL complejas",
            session_id=None,
            method="direct",
            status="sent",
        )
        self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Diseñar esquema de estilos CSS",
            session_id=None,
            method="direct",
            status="sent",
        )

        match_sql, _ = self.ctx.history.list_events(project_id=self.p1.id, search="consultas SQL")
        self.assertEqual(len(match_sql), 1)
        self.assertIn("Optimizar consultas SQL", match_sql[0].description)

        match_none, _ = self.ctx.history.list_events(project_id=self.p1.id, search="invalido_inexistente")
        self.assertEqual(len(match_none), 0)

    def test_history_pagination(self) -> None:
        """10. Paginación respeta limit y offset y reporta has_more correctamente."""
        for i in range(5):
            self.ctx.prompt_transfers.record(
                project_id=self.p1.id,
                agent=AgentKind.CLAUDE_CODE,
                prompt=f"Evento número {i}",
                session_id=None,
                method="direct",
                status="sent",
            )

        page1, has_more1 = self.ctx.history.list_events(project_id=self.p1.id, limit=3, offset=0)
        self.assertEqual(len(page1), 3)
        self.assertTrue(has_more1)

        page2, has_more2 = self.ctx.history.list_events(project_id=self.p1.id, limit=3, offset=3)
        self.assertEqual(len(page2), 2)
        self.assertFalse(has_more2)

    def test_history_timezone_tegucigalpa(self) -> None:
        """11. Las fechas se convierten correctamente a America/Tegucigalpa (UTC-6)."""
        # 14:00 UTC = 08:00 America/Tegucigalpa
        dt_utc = datetime(2026, 10, 22, 14, 0, 0, tzinfo=timezone.utc)
        dt_local = _to_local_datetime(dt_utc)
        self.assertEqual(dt_local.tzinfo, TZ_TEGUCIGALPA)
        self.assertEqual(dt_local.hour, 8)
        self.assertEqual(dt_local.minute, 0)
        self.assertEqual(dt_local.strftime("%H:%M"), "08:00")


if __name__ == "__main__":
    unittest.main()
