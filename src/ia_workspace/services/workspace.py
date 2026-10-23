"""Proyecto activo y estado visual aislado por proyecto."""

from __future__ import annotations

from collections.abc import Callable

from ia_workspace.domain.models import WorkspaceState
from ia_workspace.persistence.repositories import SettingsRepository, WorkspaceStateRepository

ActiveListener = Callable[[int | None], None]

_LAST_PROJECT_KEY = "workspace.last_project_id"


class WorkspaceController:
    """Coordina qué proyecto se muestra y guarda su estado visual.

    Invariantes:
    * Solo hay un proyecto activo (visible) a la vez.
    * Cada proyecto tiene su propio ``WorkspaceState``; nunca se comparten instancias.
    * Cambiar de proyecto solo cambia lo que se muestra: no afecta a las sesiones
      del ``SessionManager``, que viven al nivel de la aplicación.
    """

    def __init__(self, states: WorkspaceStateRepository, settings: SettingsRepository) -> None:
        self._states_repo = states
        self._settings = settings
        self._states: dict[int, WorkspaceState] = {}
        self._dirty: set[int] = set()
        self._active: int | None = None
        self._listeners: list[ActiveListener] = []

    @property
    def active_project_id(self) -> int | None:
        return self._active

    @property
    def last_project_id(self) -> int | None:
        raw = self._settings.get(_LAST_PROJECT_KEY)
        return int(raw) if raw and raw.isdigit() else None

    def subscribe(self, listener: ActiveListener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def activate(self, project_id: int | None) -> None:
        if project_id == self._active:
            return
        self._active = project_id
        if project_id is not None:
            self._settings.set(_LAST_PROJECT_KEY, str(project_id))
        for listener in list(self._listeners):
            listener(project_id)

    def state_for(self, project_id: int) -> WorkspaceState:
        """Devuelve el estado vivo (mutable) del proyecto; tras mutarlo, llama a ``mark_dirty``."""
        if project_id not in self._states:
            self._states[project_id] = self._states_repo.load(project_id)
        return self._states[project_id]

    @property
    def has_dirty(self) -> bool:
        return bool(self._dirty)

    def mark_dirty(self, project_id: int) -> None:
        self._dirty.add(project_id)

    def flush(self) -> None:
        if not self._dirty:
            return
        for project_id in sorted(self._dirty):
            if project_id in self._states:
                self._states_repo.save(project_id, self._states[project_id])
        self._dirty.clear()

    def forget(self, project_id: int) -> None:
        """Descarta el estado en memoria de un proyecto eliminado del registro."""
        self._states.pop(project_id, None)
        self._dirty.discard(project_id)
        if self._active == project_id:
            self.activate(None)
        if self.last_project_id == project_id:
            self._settings.set(_LAST_PROJECT_KEY, None)
