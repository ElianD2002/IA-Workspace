"""Conversaciones de ChatGPT vinculadas a cada proyecto.

Solo se guardan el enlace (validado y normalizado), su tipo y un título. Nunca se
accede al contenido de la conversación ni se piden credenciales: abrirla es tarea del
navegador del usuario, con su sesión habitual.
"""

from __future__ import annotations

from collections.abc import Callable

from ia_workspace.domain.models import ChatLink
from ia_workspace.persistence.repositories import ChatLinkRepository, DuplicateChatLinkError
from ia_workspace.services.chatgpt_links import InvalidChatLinkError, LinkKind, ParsedChatLink, parse_chat_link

MAX_TITLE_LENGTH = 120

# listener(project_id): solo el proyecto afectado necesita volver a dibujar su lista.
Listener = Callable[[int], None]


class ChatLinkError(ValueError):
    """Error apto para mostrarse en la UI."""


class ChatLinkService:
    def __init__(self, repository: ChatLinkRepository) -> None:
        self._repo = repository
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, project_id: int) -> None:
        for listener in list(self._listeners):
            listener(project_id)

    def list(self, project_id: int) -> list[ChatLink]:
        return self._repo.list(project_id)

    def get(self, link_id: int) -> ChatLink | None:
        return self._repo.get(link_id)

    @staticmethod
    def _clean_title(title: str) -> str:
        title = " ".join(title.split())
        if len(title) > MAX_TITLE_LENGTH:
            raise ChatLinkError(f"El título no puede superar {MAX_TITLE_LENGTH} caracteres.")
        return title

    def check(self, project_id: int, url: str) -> ParsedChatLink:
        """Valida el enlace y que no esté ya vinculado a este proyecto (para el diálogo)."""
        try:
            parsed = parse_chat_link(url)
        except InvalidChatLinkError as exc:
            raise ChatLinkError(str(exc)) from exc
        existing = self._repo.find(project_id, parsed.kind.value, parsed.conversation_id)
        if existing is not None:
            raise ChatLinkError(f"Esta conversación ya está vinculada a este proyecto como «{existing.title}».")
        return parsed

    def link(self, project_id: int, url: str, title: str = "") -> ChatLink:
        parsed = self.check(project_id, url)
        clean = self._clean_title(title) or parsed.kind.default_title
        try:
            link = self._repo.add(project_id, parsed.url, parsed.kind.value, parsed.conversation_id, clean)
        except DuplicateChatLinkError as exc:
            raise ChatLinkError("Esta conversación ya está vinculada a este proyecto.") from exc
        self._emit(project_id)
        return link

    def rename(self, link_id: int, title: str) -> None:
        link = self._repo.get(link_id)
        if link is None:
            return
        clean = self._clean_title(title)
        if not clean:
            raise ChatLinkError("Escribe un título para la conversación.")
        self._repo.rename(link_id, clean)
        self._emit(link.project_id)

    def unlink(self, link_id: int) -> None:
        """Quita el vínculo en IA Workspace. La conversación no se borra de ChatGPT."""
        link = self._repo.get(link_id)
        if link is None:
            return
        self._repo.remove(link_id)
        self._emit(link.project_id)


def kind_of(link: ChatLink) -> LinkKind:
    return LinkKind(link.kind)
