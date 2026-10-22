"""Pruebas unitarias y de integración para el módulo Tareas y prompts (Fase 6B.1).

Verifica:
1. Creación automática tras transferencia exitosa.
2. Cancelación / omisión de transferencia sin crear tarea.
3. Fallo de transferencia (status="error") no crea tarea.
4. Prevención de duplicados por reintentos (mismo transfer_id).
5. Persistencia y migración incremental SQLite (versión 9, índices, claves foráneas, borrado en cascada).
6. Aislamiento estricto entre proyectos (sin mezclar tareas).
7. Estados y transiciones válidas (historial de trazabilidad).
8. Sesión finalizada con código 0 transiciona a PENDING_REVIEW, nunca a COMPLETED.
9. Actualización manual de estado y notas.
10. Consulta de tareas históricas tras sesión finalizada.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest

from helpers import temp_dir

from ia_workspace.context import AppContext
from ia_workspace.domain.models import AgentKind, NewProject, TaskState
from ia_workspace.persistence.database import Database, SCHEMA_VERSION
from ia_workspace.sessions.models import SessionEvent, SessionInfo, SessionSpec, SessionState


class TestTasksModule(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.path = Path(self.tmp.name)
        (self.path / "p1").mkdir()
        (self.path / "p2").mkdir()
        self.db = Database(self.path / "test.db")
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.p1 = self.ctx.projects.register("Proyecto 1", str(self.path / "p1"), (AgentKind.CLAUDE_CODE,))
        self.p2 = self.ctx.projects.register("Proyecto 2", str(self.path / "p2"), (AgentKind.CODEX,))

    def tearDown(self) -> None:
        self.ctx.close()
        self.tmp.cleanup()

    def test_schema_version_and_tables(self) -> None:
        """Comprueba que la migración incremental a versión 9 o superior existe con las tablas tasks y task_history."""
        self.assertGreaterEqual(SCHEMA_VERSION, 9)
        # Comprobar existencia de tablas
        tables = [
            row["name"]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        self.assertIn("tasks", tables)
        self.assertIn("task_history", tables)

    def test_create_task_on_successful_transfer(self) -> None:
        """1. Creación automática tras transferencia exitosa con prompt íntegro y sesión vinculada."""
        prompt_text = "Implementar componente de autenticación con OAuth2\nDetalles adicionales..."
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt=prompt_text,
            session_id="sess-001",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.project_id, self.p1.id)
        self.assertEqual(task.agent, AgentKind.CLAUDE_CODE)
        self.assertEqual(task.session_id, "sess-001")
        self.assertEqual(task.status, TaskState.IN_PROGRESS)
        self.assertEqual(task.title, "Implementar componente de autenticación con OAuth2")
        self.assertEqual(task.prompt, prompt_text)
        self.assertEqual(task.transfer_id, transfer.id)

        # Historial inicial
        history = self.ctx.tasks.list_history(task.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].to_status, TaskState.IN_PROGRESS.value)

    def test_no_task_on_error_transfer(self) -> None:
        """2 & 3. Fallo de transferencia o error previo no crea tarea ficticia."""
        transfer_err = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CODEX,
            prompt="Prompt que falló",
            session_id=None,
            method="direct",
            status="error",
            detail="Error de autenticación",
        )
        task = self.ctx.tasks.create_for_transfer(transfer_err)
        self.assertIsNone(task)
        self.assertEqual(len(self.ctx.tasks.list_for_project(self.p1.id)), 0)

    def test_prevent_duplicates_on_retry(self) -> None:
        """4. Prevención de duplicados por reintentos con el mismo transfer_id."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.ANTIGRAVITY,
            prompt="Generar tests e2e",
            session_id="sess-ag-1",
            method="manual",
            status="copied",
        )
        task1 = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task1)
        # Reintento con el mismo transfer
        task2 = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task2)
        assert task1 is not None and task2 is not None
        self.assertEqual(task1.id, task2.id)
        self.assertEqual(len(self.ctx.tasks.list_for_project(self.p1.id)), 1)

    def test_project_isolation(self) -> None:
        """6. Aislamiento entre proyectos: las tareas nunca se mezclan."""
        t1 = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tarea de P1",
            session_id="s1",
            method="direct",
            status="sent",
        )
        t2 = self.ctx.prompt_transfers.record(
            project_id=self.p2.id,
            agent=AgentKind.CODEX,
            prompt="Tarea de P2",
            session_id="s2",
            method="direct",
            status="sent",
        )
        task_p1 = self.ctx.tasks.create_for_transfer(t1)
        task_p2 = self.ctx.tasks.create_for_transfer(t2)
        self.assertIsNotNone(task_p1)
        self.assertIsNotNone(task_p2)

        tasks_1 = self.ctx.tasks.list_for_project(self.p1.id)
        tasks_2 = self.ctx.tasks.list_for_project(self.p2.id)
        self.assertEqual(len(tasks_1), 1)
        self.assertEqual(len(tasks_2), 1)
        self.assertEqual(tasks_1[0].project_id, self.p1.id)
        self.assertEqual(tasks_2[0].project_id, self.p2.id)

    def test_foreign_key_cascade_deletion(self) -> None:
        """5. Persistencia y borrado en cascada al eliminar un proyecto."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p2.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tarea a eliminar",
            session_id="s_del",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task)
        assert task is not None

        # Al eliminar el proyecto p2, la tarea se borra en cascada
        self.ctx.projects.unregister(self.p2.id)
        self.assertIsNone(self.ctx.tasks.get(task.id))
        self.assertEqual(len(self.ctx.tasks.list_for_project(self.p2.id)), 0)

    def test_session_done_transitions_to_pending_review_not_completed(self) -> None:
        """8. REGLA FUNDAMENTAL: Sesión terminada con código 0 pasa a PENDING_REVIEW, no a COMPLETED."""
        spec = SessionSpec(
            project_id=self.p1.id,
            kind=AgentKind.CLAUDE_CODE,
            cwd=str(self.path / "p1"),
            title="Tarea de prueba de ciclo",
        )
        info = self.ctx.sessions.start(spec)
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Hacer refactor del módulo",
            session_id=info.id,
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, TaskState.IN_PROGRESS)

        # Simulamos que la sesión finaliza exitosamente con código 0
        info.state = SessionState.DONE
        info.exit_code = 0
        self.ctx.sessions._emit("state", info)

        updated_task = self.ctx.tasks.get(task.id)
        assert updated_task is not None
        # DEBE QUEDAR PENDIENTE DE REVISIÓN, NUNCA COMPLETADA AUTOMÁTICAMENTE
        self.assertEqual(updated_task.status, TaskState.PENDING_REVIEW)
        self.assertNotEqual(updated_task.status, TaskState.COMPLETED)

        # Historial de trazabilidad
        hist = self.ctx.tasks.list_history(task.id)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1].from_status, TaskState.IN_PROGRESS.value)
        self.assertEqual(hist[1].to_status, TaskState.PENDING_REVIEW.value)

    def test_session_failed_transitions_to_failed(self) -> None:
        """Evento de sesión fallida pasa la tarea a FAILED."""
        spec = SessionSpec(
            project_id=self.p1.id,
            kind=AgentKind.CODEX,
            cwd=str(self.path / "p1"),
            title="Sesión a fallar",
        )
        info = self.ctx.sessions.start(spec)
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CODEX,
            prompt="Tarea que fallará",
            session_id=info.id,
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        assert task is not None

        info.state = SessionState.FAILED
        info.exit_code = 1
        info.detail = "Command failed with exit code 1"
        self.ctx.sessions._emit("state", info)

        updated = self.ctx.tasks.get(task.id)
        assert updated is not None
        self.assertEqual(updated.status, TaskState.FAILED)

    def test_manual_update_and_notes(self) -> None:
        """7 & 9. Actualización manual de estado, notas y marcado como completada."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.ANTIGRAVITY,
            prompt="Generar documentación de arquitectura",
            session_id="sess-doc",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        assert task is not None

        # Guardar notas intermedias
        self.ctx.tasks.update_notes(task.id, "Analizando ficheros de arquitectura...")
        t_notes = self.ctx.tasks.get(task.id)
        assert t_notes is not None
        self.assertEqual(t_notes.result_notes, "Analizando ficheros de arquitectura...")

        # Marcar como completada con notas finales
        completed = self.ctx.tasks.mark_completed(task.id, result_notes="Documentación generada en docs/ARCH.md")
        assert completed is not None
        self.assertEqual(completed.status, TaskState.COMPLETED)
        self.assertEqual(completed.result_notes, "Documentación generada en docs/ARCH.md")

        # Historial registra la confirmación manual
        hist = self.ctx.tasks.list_history(task.id)
        last_entry = hist[-1]
        self.assertEqual(last_entry.to_status, TaskState.COMPLETED.value)
        self.assertIn("completada por el usuario", last_entry.note)

    def test_historical_tasks_consultation(self) -> None:
        """10 & 11. Consulta de tareas históricas sin sesión activa y continuidad."""
        transfer = self.ctx.prompt_transfers.record(
            project_id=self.p1.id,
            agent=AgentKind.CLAUDE_CODE,
            prompt="Tarea ya terminada hace días",
            session_id="sess-old-999",
            method="direct",
            status="sent",
        )
        task = self.ctx.tasks.create_for_transfer(transfer)
        assert task is not None
        # La sesión sess-old-999 no existe en ctx.sessions
        self.assertIsNone(self.ctx.sessions.get("sess-old-999"))
        # La tarea sigue siendo perfectamente consultable con su prompt íntegro
        fetched = self.ctx.tasks.get(task.id)
        assert fetched is not None
        self.assertEqual(fetched.prompt, "Tarea ya terminada hace días")
        self.assertEqual(fetched.session_id, "sess-old-999")
