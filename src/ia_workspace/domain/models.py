"""Entidades del dominio."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

# Paleta de colores de proyecto tomada del prototipo de diseño.
PROJECT_COLORS: tuple[str, ...] = (
    "#1c63c4",
    "#b5501f",
    "#22714f",
    "#7d47a8",
    "#1c6f80",
    "#a51d6b",
    "#8a6a00",
    "#505057",
)


class AgentKind(StrEnum):
    """Herramientas que IA Workspace podrá orquestar.

    Cada una usa la autenticación oficial de su propia CLI/aplicación; IA Workspace
    nunca guarda credenciales ni tokens.
    """

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"
    SHELL = "shell"

    @property
    def label(self) -> str:
        return _AGENT_LABELS[self]

    @property
    def mono(self) -> str:
        return _AGENT_MONOS[self]


_AGENT_LABELS = {
    AgentKind.CLAUDE_CODE: "Claude Code",
    AgentKind.CODEX: "Codex",
    AgentKind.ANTIGRAVITY: "Google Antigravity",
    AgentKind.SHELL: "Terminal",
}
_AGENT_MONOS = {
    AgentKind.CLAUDE_CODE: "CC",
    AgentKind.CODEX: "CX",
    AgentKind.ANTIGRAVITY: "AG",
    AgentKind.SHELL: "$_",
}

# Agentes que se pueden habilitar por proyecto (la terminal siempre está disponible).
SELECTABLE_AGENTS: tuple[AgentKind, ...] = (
    AgentKind.CLAUDE_CODE,
    AgentKind.CODEX,
    AgentKind.ANTIGRAVITY,
)


def initials_for(name: str) -> str:
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    path: str
    color_index: int = 0
    agents: tuple[AgentKind, ...] = SELECTABLE_AGENTS
    created_at: datetime | None = None
    last_opened_at: datetime | None = None

    @property
    def initials(self) -> str:
        return initials_for(self.name)

    @property
    def color(self) -> str:
        return PROJECT_COLORS[self.color_index % len(PROJECT_COLORS)]


@dataclass(frozen=True)
class NewProject:
    name: str
    path: str
    agents: tuple[AgentKind, ...] = SELECTABLE_AGENTS


@dataclass(frozen=True)
class FolderIdentity:
    """Referencias para reconocer una carpeta más allá de su ruta (``None`` si no existía)."""

    device: int | None = None
    inode: int | None = None
    is_git: bool | None = None

    @property
    def known(self) -> bool:
        return self.device is not None and self.inode is not None


@dataclass(frozen=True)
class RetiredMemory:
    """Memoria de un proyecto quitado del registro, pendiente de recuperar."""

    id: int
    memory_key: str
    project_name: str
    path: str
    identity: FolderIdentity
    retired_at: datetime | None = None


@dataclass(frozen=True)
class ChatLink:
    """Conversación de ChatGPT vinculada a un proyecto (solo enlace y título)."""

    id: int
    project_id: int
    url: str
    kind: str  # "private" | "shared" (services.chatgpt_links.LinkKind)
    conversation_id: str
    title: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class PromptTransfer:
    """Registro de un intento de transferencia de un prompt a un agente.

    ``status`` solo describe el resultado inmediato de la transferencia (si el
    comando se lanzó o se copió), nunca si el agente completó la tarea: eso lo
    sigue decidiendo el estado real de la sesión (``session_id``), mientras exista
    en esta ejecución de IA Workspace.
    """

    id: int
    project_id: int
    agent: AgentKind
    prompt: str
    session_id: str | None
    method: str  # "direct" (mecanismo oficial de la CLI) | "manual" (copiar y pegar)
    status: str  # "sent" | "copied" | "error"
    detail: str
    created_at: datetime | None = None


@dataclass
class WorkspaceState:
    """Estado visual de un workspace. Se guarda por proyecto y nunca se comparte."""

    show_chat: bool = True
    show_prompt: bool = True
    show_context: bool = True
    context_tab: str = "tasks"  # "tasks" | "memory"
    prompt_draft: str = ""
    selected_session: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def copy(self) -> WorkspaceState:
        return replace(self, extra=dict(self.extra))

    def to_dict(self) -> dict[str, object]:
        return {
            "show_chat": self.show_chat,
            "show_prompt": self.show_prompt,
            "show_context": self.show_context,
            "context_tab": self.context_tab,
            "prompt_draft": self.prompt_draft,
            "selected_session": self.selected_session,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WorkspaceState:
        state = cls()
        for key in ("show_chat", "show_prompt", "show_context"):
            if isinstance(data.get(key), bool):
                setattr(state, key, data[key])
        if data.get("context_tab") in ("tasks", "memory"):
            state.context_tab = data["context_tab"]  # type: ignore[assignment]
        if isinstance(data.get("prompt_draft"), str):
            state.prompt_draft = data["prompt_draft"]  # type: ignore[assignment]
        if isinstance(data.get("selected_session"), str):
            state.selected_session = data["selected_session"]  # type: ignore[assignment]
        if isinstance(data.get("extra"), dict):
            state.extra = dict(data["extra"])  # type: ignore[arg-type]
        return state
