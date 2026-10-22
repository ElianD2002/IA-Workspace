"""Servicio de biblioteca de plantillas de prompts reutilizables (fase 6B.2).

Permite crear, listar, actualizar y eliminar plantillas con ámbito global o por
proyecto, así como gestionar variables parametrizables {{variable}} sin ejecutar
agentes ni crear tareas de forma no solicitada.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib

from ia_workspace.domain.models import (
    PromptTemplate,
    PromptTemplateScope,
    extract_template_variables,
    render_template_content,
)
from ia_workspace.persistence.repositories import PromptTemplateRepository


class PromptTemplateService:
    """Gestiona el catálogo de plantillas de prompts y notifica a los observadores."""

    def __init__(self, repo: PromptTemplateRepository) -> None:
        self._repo = repo
        self._subscribers: list[Callable[[], None]] = []

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Suscribe un callback para cambios en plantillas. Retorna función para desuscribir."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _notify(self) -> None:
        """Notifica a todos los suscriptores en el hilo principal de GLib."""
        for cb in list(self._subscribers):
            try:
                GLib.idle_add(cb)
            except Exception:  # noqa: BLE001
                pass

    def get(self, template_id: int) -> PromptTemplate | None:
        return self._repo.get(template_id)

    def list_for_project(
        self,
        project_id: int,
        category: str | None = None,
        search: str = "",
    ) -> list[PromptTemplate]:
        """Obtiene plantillas visibles para un proyecto (globales + específicas)."""
        return self._repo.list_for_project(project_id, category=category, search=search)

    def list_all(
        self,
        project_id: int | None = None,
        category: str | None = None,
        search: str = "",
    ) -> list[PromptTemplate]:
        """Obtiene todas las plantillas o filtra por un proyecto específico."""
        return self._repo.list_all(project_id=project_id, category=category, search=search)

    def categories(self, project_id: int | None = None) -> list[str]:
        return self._repo.categories(project_id)

    def create(
        self,
        name: str,
        content: str,
        category: str = "General",
        description: str = "",
        scope: PromptTemplateScope = PromptTemplateScope.GLOBAL,
        project_id: int | None = None,
    ) -> PromptTemplate:
        name = name.strip()
        if not name:
            raise ValueError("El nombre de la plantilla no puede estar vacío")
        content = content.strip()
        if not content:
            raise ValueError("El contenido del prompt no puede estar vacío")

        template = self._repo.create(
            name=name,
            content=content,
            category=category.strip() or "General",
            description=description.strip(),
            scope=scope,
            project_id=project_id,
        )
        self._notify()
        return template

    def update(
        self,
        template_id: int,
        name: str,
        content: str,
        category: str = "General",
        description: str = "",
        scope: PromptTemplateScope = PromptTemplateScope.GLOBAL,
        project_id: int | None = None,
    ) -> PromptTemplate | None:
        name = name.strip()
        if not name:
            raise ValueError("El nombre de la plantilla no puede estar vacío")
        content = content.strip()
        if not content:
            raise ValueError("El contenido del prompt no puede estar vacío")

        template = self._repo.update(
            template_id=template_id,
            name=name,
            content=content,
            category=category.strip() or "General",
            description=description.strip(),
            scope=scope,
            project_id=project_id,
        )
        if template is not None:
            self._notify()
        return template

    def delete(self, template_id: int) -> bool:
        success = self._repo.delete(template_id)
        if success:
            self._notify()
        return success

    @staticmethod
    def extract_variables(content: str) -> list[str]:
        return extract_template_variables(content)

    @staticmethod
    def render(content: str, values: dict[str, str]) -> str:
        return render_template_content(content, values)

    def close(self) -> None:
        self._subscribers.clear()
