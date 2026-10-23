"""Entidades del dominio."""

from __future__ import annotations

import re
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


class AutonomyMode(StrEnum):
    """Nivel de autonomía y autorización de un agente en un proyecto (fase 4)."""

    NORMAL = "normal"
    AUTO = "auto"
    DANGER = "danger"

    @property
    def label(self) -> str:
        if self is AutonomyMode.AUTO:
            return "Automático"
        if self is AutonomyMode.DANGER:
            return "Sin confirmaciones"
        return "Normal"

    @property
    def description(self) -> str:
        if self is AutonomyMode.AUTO:
            return "Confirmaciones reducidas según políticas del agente."
        if self is AutonomyMode.DANGER:
            return "Ejecución sin confirmaciones ni sandbox (avanzado)."
        return "Políticas predeterminadas de la CLI."


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


class TaskState(StrEnum):
    """Estados del ciclo de vida de una tarea vinculada a un agente (fase 6B.1)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def label(self) -> str:
        _TASK_LABELS = {
            TaskState.PENDING: "Pendiente",
            TaskState.IN_PROGRESS: "En progreso",
            TaskState.PENDING_REVIEW: "Pendiente de revisión",
            TaskState.COMPLETED: "Completada",
            TaskState.FAILED: "Fallida",
            TaskState.CANCELED: "Cancelada",
        }
        return _TASK_LABELS[self]

    @property
    def style(self) -> str:
        _TASK_STYLES = {
            TaskState.PENDING: "idle",
            TaskState.IN_PROGRESS: "working",
            TaskState.PENDING_REVIEW: "wait",
            TaskState.COMPLETED: "done",
            TaskState.FAILED: "error",
            TaskState.CANCELED: "off",
        }
        return _TASK_STYLES[self]


@dataclass(frozen=True)
class TaskHistoryEntry:
    """Registro de trazabilidad de cambios de estado de una tarea."""

    id: int
    task_id: int
    from_status: str
    to_status: str
    note: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class Task:
    """Tarea ejecutada por un agente en un proyecto a partir de un prompt transferido."""

    id: int
    project_id: int
    title: str
    transfer_id: int
    prompt: str
    agent: AgentKind
    session_id: str | None
    status: TaskState
    result_notes: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ-]+)\s*\}\}")


def extract_template_variables(content: str) -> list[str]:
    """Extrae nombres únicos de variables {{variable}} en orden de aparición."""
    seen: set[str] = set()
    variables: list[str] = []
    for match in _TEMPLATE_VAR_RE.finditer(content):
        name = match.group(1).strip()
        if name not in seen:
            seen.add(name)
            variables.append(name)
    return variables


def render_template_content(content: str, values: dict[str, str]) -> str:
    """Reemplaza los marcadores {{variable}} por sus valores proporcionados."""
    def repl(m: re.Match[str]) -> str:
        var = m.group(1).strip()
        return values.get(var, m.group(0))

    return _TEMPLATE_VAR_RE.sub(repl, content)


class PromptTemplateScope(StrEnum):
    """Ámbito de visibilidad de una plantilla de prompt (fase 6B.2)."""

    GLOBAL = "global"
    PROJECT = "project"

    @property
    def label(self) -> str:
        return "Global" if self == PromptTemplateScope.GLOBAL else "Proyecto"


@dataclass(frozen=True)
class PromptTemplate:
    """Plantilla de prompt reutilizable para acelerar el trabajo con agentes."""

    id: int
    name: str
    description: str
    category: str
    scope: PromptTemplateScope
    project_id: int | None
    content: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def variables(self) -> list[str]:
        return extract_template_variables(self.content)


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


class HistoryEventKind(StrEnum):
    """Categoría del evento histórico (fase 6C.1)."""

    TASK = "task"
    PROMPT_TRANSFER = "prompt_transfer"
    BRAIN_PROPOSAL = "brain_proposal"
    CHAT_LINK = "chat_link"

    @property
    def label(self) -> str:
        _LABELS = {
            HistoryEventKind.TASK: "Tareas",
            HistoryEventKind.PROMPT_TRANSFER: "Transferencias de prompts",
            HistoryEventKind.BRAIN_PROPOSAL: "Cerebro y memoria",
            HistoryEventKind.CHAT_LINK: "Conversaciones ChatGPT",
        }
        return _LABELS[self]

    @property
    def icon_name(self) -> str:
        _ICONS = {
            HistoryEventKind.TASK: "view-list-bullet-symbolic",
            HistoryEventKind.PROMPT_TRANSFER: "document-send-symbolic",
            HistoryEventKind.BRAIN_PROPOSAL: "user-bookmarks-symbolic",
            HistoryEventKind.CHAT_LINK: "web-browser-symbolic",
        }
        return _ICONS[self]


@dataclass(frozen=True)
class HistoryEvent:
    """Evento registrado en el historial de actividad de IA Workspace."""

    id: str
    kind: HistoryEventKind
    project_id: int
    project_name: str
    title: str
    description: str
    created_at: datetime
    agent_kind: str | None = None
    status: str | None = None
    status_label: str = ""
    status_style: str = "dim-label"
    target_type: str = ""  # "workspace", "tasks", "memory"
    target_id: str | int | None = None
