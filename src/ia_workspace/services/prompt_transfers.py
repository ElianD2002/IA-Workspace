"""Registro de las transferencias de prompts a agentes.

Guarda cada intento con su proyecto, agente, prompt completo, sesión (mientras
exista en esta ejecución) y el resultado inmediato de la transferencia. Nunca
guarda si la tarea se completó: eso lo decide el estado real de la sesión
(``sessions.models.SessionState``), que se consulta por separado.
"""

from __future__ import annotations

from collections.abc import Callable

from ia_workspace.domain.models import AgentKind, PromptTransfer
from ia_workspace.persistence.repositories import PromptTransferRepository

# listener(project_id): solo el proyecto afectado necesita volver a dibujar su lista.
Listener = Callable[[int], None]


class PromptTransferService:
    def __init__(self, repository: PromptTransferRepository) -> None:
        self._repo = repository
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, project_id: int) -> None:
        for listener in list(self._listeners):
            listener(project_id)

    def list(self, project_id: int) -> list[PromptTransfer]:
        return self._repo.list(project_id)

    def record(
        self,
        project_id: int,
        agent: AgentKind,
        prompt: str,
        session_id: str | None,
        method: str,
        status: str,
        detail: str = "",
    ) -> PromptTransfer:
        transfer = self._repo.add(project_id, agent, prompt, session_id, method, status, detail)
        self._emit(project_id)
        return transfer
