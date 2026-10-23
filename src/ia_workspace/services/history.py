"""Servicio reactivo del historial de actividad (fase 6C.1)."""

from __future__ import annotations

from collections.abc import Callable

from ia_workspace.domain.models import HistoryEvent, HistoryEventKind
from ia_workspace.persistence.repositories import HistoryRepository

Listener = Callable[[], None]


class HistoryService:
    """Coordina el acceso y notificaciones del historial unificado."""

    def __init__(self, repository: HistoryRepository) -> None:
        self._repo = repository
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def notify_changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                pass

    def list_events(
        self,
        project_id: int | None = None,
        kind: HistoryEventKind | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[HistoryEvent], bool]:
        """Consulta eventos del historial de forma eficiente."""
        return self._repo.list_events(
            project_id=project_id,
            kind=kind,
            search=search,
            limit=limit,
            offset=offset,
        )

    def close(self) -> None:
        self._listeners.clear()
