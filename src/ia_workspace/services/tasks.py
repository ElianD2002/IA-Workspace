"""Servicio de tareas vinculadas a proyectos, prompts y agentes (fase 6B.1).

Orquesta el ciclo de vida de cada tarea generada a partir de un prompt
transferido a un agente (Claude Code, OpenAI Codex, Google Antigravity o Shell),
asociándola a su sesión de ejecución real y manteniendo su trazabilidad de estados.
"""

from __future__ import annotations

from collections.abc import Callable

from ia_workspace.domain.models import PromptTransfer, Task, TaskHistoryEntry, TaskState
from ia_workspace.persistence.repositories import TaskRepository
from ia_workspace.sessions.manager import SessionManager
from ia_workspace.sessions.models import SessionEvent, SessionState

TaskListener = Callable[[int], None]  # listener(project_id)


class TaskService:
    def __init__(
        self,
        repo: TaskRepository,
        sessions: SessionManager | None = None,
    ) -> None:
        self._repo = repo
        self._sessions = sessions
        self._listeners: list[TaskListener] = []
        self._unsub_sessions: Callable[[], None] | None = None

        if self._sessions is not None:
            self._unsub_sessions = self._sessions.subscribe(self._on_session_event)

    def close(self) -> None:
        if self._unsub_sessions is not None:
            self._unsub_sessions()
            self._unsub_sessions = None

    def subscribe(self, listener: TaskListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, project_id: int) -> None:
        for listener in list(self._listeners):
            try:
                listener(project_id)
            except Exception:
                pass

    def create_for_transfer(
        self,
        transfer: PromptTransfer,
        title: str | None = None,
    ) -> Task | None:
        """Crea automáticamente una tarea a partir de una transferencia exitosa de prompt.

        Si la transferencia falló (status == 'error') o ya existe una tarea para este
        identificador de transferencia, no crea duplicados y devuelve la tarea existente.
        """
        if transfer.status == "error":
            return None

        # Evitar duplicados por reintentos
        existing = self._repo.get_by_transfer_id(transfer.id)
        if existing is not None:
            return existing

        resolved_title = title.strip() if title and title.strip() else ""
        if not resolved_title:
            first_line = next((line.strip() for line in transfer.prompt.splitlines() if line.strip()), "")
            resolved_title = first_line[:80] if first_line else f"Tarea de {transfer.agent.label}"

        task = self._repo.create(
            project_id=transfer.project_id,
            title=resolved_title,
            transfer_id=transfer.id,
            agent=transfer.agent,
            session_id=transfer.session_id,
            status=TaskState.IN_PROGRESS,
            result_notes="",
            initial_note="Tarea iniciada por transferencia de prompt",
        )
        self._emit(transfer.project_id)
        return task

    def get(self, task_id: int) -> Task | None:
        return self._repo.get(task_id)

    def list_for_project(self, project_id: int, status: TaskState | None = None) -> list[Task]:
        return self._repo.list(project_id=project_id, status=status)

    def list_all(self, status: TaskState | None = None) -> list[Task]:
        return self._repo.list(project_id=None, status=status)

    def list_history(self, task_id: int) -> list[TaskHistoryEntry]:
        return self._repo.list_history(task_id)

    def update_status(self, task_id: int, new_status: TaskState, note: str = "") -> Task | None:
        task = self._repo.get(task_id)
        if task is None:
            return None
        if task.status == new_status:
            return task
        updated = self._repo.update_status(task_id, new_status, note=note)
        if updated is not None:
            self._emit(updated.project_id)
        return updated

    def update_notes(self, task_id: int, notes: str) -> Task | None:
        task = self._repo.get(task_id)
        if task is None:
            return None
        updated = self._repo.update_notes(task_id, notes)
        if updated is not None:
            self._emit(updated.project_id)
        return updated

    def mark_completed(self, task_id: int, result_notes: str | None = None) -> Task | None:
        """Marca manualmente la tarea como completada, registrando opcionalmente el resultado."""
        task = self._repo.get(task_id)
        if task is None:
            return None
        if result_notes is not None and result_notes != task.result_notes:
            self._repo.update_notes(task_id, result_notes)
        updated = self._repo.update_status(
            task_id,
            TaskState.COMPLETED,
            note="Marcada como completada por el usuario",
        )
        if updated is not None:
            self._emit(updated.project_id)
        return updated

    def _on_session_event(self, event: SessionEvent) -> None:
        """Sincroniza el estado de las tareas vinculadas cuando cambia la sesión real."""
        session = event.session
        if not session.id:
            return

        # Solo actuamos ante estados terminales de la sesión
        if not session.state.is_terminal:
            return

        # Buscar tareas en progreso vinculadas a esta sesión
        tasks = self._repo.find_by_session_id(session.id)
        for task in tasks:
            if task.status != TaskState.IN_PROGRESS:
                continue

            if session.state is SessionState.DONE:
                # REGLA FUNDAMENTAL: El cierre con código 0 NO significa completada.
                # Pasa a PENDING_REVIEW hasta que el usuario confirme el resultado.
                self._repo.update_status(
                    task.id,
                    TaskState.PENDING_REVIEW,
                    note="Sesión finalizada con código 0. Pendiente de revisión del usuario.",
                )
                self._emit(task.project_id)
            elif session.state is SessionState.FAILED:
                self._repo.update_status(
                    task.id,
                    TaskState.FAILED,
                    note=f"Sesión finalizada con error (código {session.exit_code}).",
                )
                self._emit(task.project_id)
            elif session.state is SessionState.ERROR:
                self._repo.update_status(
                    task.id,
                    TaskState.FAILED,
                    note=f"Error en la sesión del agente: {session.detail}",
                )
                self._emit(task.project_id)
            elif session.state is SessionState.STOPPED:
                self._repo.update_status(
                    task.id,
                    TaskState.CANCELED,
                    note="Sesión detenida por el usuario.",
                )
                self._emit(task.project_id)
