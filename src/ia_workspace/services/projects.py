"""Registro de proyectos locales."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ia_workspace.domain.models import AgentKind, NewProject, Project
from ia_workspace.persistence.repositories import DuplicateProjectError, ProjectRepository
from ia_workspace.services.detection import folder_identity


class ProjectValidationError(ValueError):
    """Datos inválidos al registrar o editar un proyecto. El mensaje es apto para la UI."""


Listener = Callable[[], None]


class ProjectService:
    """Valida y registra proyectos. Notifica a los observadores ante cualquier cambio.

    Registrar un proyecto no copia, mueve ni escribe archivos en su carpeta.
    """

    def __init__(self, repository: ProjectRepository) -> None:
        self._repo = repository
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _emit(self) -> None:
        for listener in list(self._listeners):
            listener()

    @staticmethod
    def normalize_path(path: str) -> str:
        return str(Path(path).expanduser().resolve())

    def list(self) -> list[Project]:
        return self._repo.list()

    def get(self, project_id: int) -> Project | None:
        return self._repo.get(project_id)

    def register(
        self,
        name: str,
        path: str,
        agents: tuple[AgentKind, ...],
        prepare: Callable[[Project], None] | None = None,
    ) -> Project:
        """Registra la carpeta. ``prepare`` se ejecuta con el proyecto recién creado antes de
        notificar a nadie (p. ej. recuperar su memoria anterior); si falla, el registro se
        deshace y la excepción se propaga."""
        name = name.strip()
        if not name:
            raise ProjectValidationError("Escribe un nombre para el proyecto.")
        if len(name) > 80:
            raise ProjectValidationError("El nombre no puede superar 80 caracteres.")
        if not path.strip():
            raise ProjectValidationError("Elige la carpeta del proyecto.")
        folder = Path(path).expanduser()
        if not folder.is_dir():
            raise ProjectValidationError("La carpeta no existe o no es un directorio.")
        normalized = self.normalize_path(path)
        try:
            project = self._repo.add(NewProject(name=name, path=normalized, agents=agents))
        except DuplicateProjectError as exc:
            existing = self._repo.get_by_path(normalized)
            label = f"«{existing.name}»" if existing else "otro proyecto"
            raise ProjectValidationError(f"Esta carpeta ya está registrada como {label}.") from exc
        if prepare is not None:
            try:
                prepare(project)
            except BaseException:
                self._repo.remove(project.id)
                raise
        self._emit()
        return project

    def rename(self, project_id: int, name: str) -> None:
        if not name.strip():
            raise ProjectValidationError("Escribe un nombre para el proyecto.")
        self._repo.rename(project_id, name)
        self._emit()

    def mark_opened(self, project_id: int) -> None:
        self._repo.touch_opened(project_id)
        self._emit()

    def unregister(self, project_id: int) -> None:
        """Quita el proyecto del registro. No toca los archivos de la carpeta. Si tenía memoria
        en el Cerebro, se conserva como recuperable junto con referencias de su carpeta."""
        project = self._repo.get(project_id)
        identity = folder_identity(project.path) if project is not None else None
        self._repo.remove(project_id, identity)
        self._emit()
