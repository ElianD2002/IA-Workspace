"""Modelos de sesión (independientes de GTK)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ia_workspace.domain.models import AgentKind


class SessionState(StrEnum):
    STARTING = "starting"
    IDLE = "idle"  # lista, esperando instrucciones
    WORKING = "working"
    WAITING_APPROVAL = "waiting_approval"
    STOPPING = "stopping"
    DONE = "done"
    ERROR = "error"
    DISCONNECTED = "disconnected"
    STOPPED = "stopped"

    @property
    def is_active(self) -> bool:
        """La sesión tiene un proceso vivo que habría que finalizar al salir."""
        return self in _ACTIVE

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def label(self) -> str:
        return _LABELS[self]


_ACTIVE = frozenset(
    {
        SessionState.STARTING,
        SessionState.IDLE,
        SessionState.WORKING,
        SessionState.WAITING_APPROVAL,
        SessionState.STOPPING,
    }
)
_TERMINAL = frozenset(
    {SessionState.DONE, SessionState.ERROR, SessionState.DISCONNECTED, SessionState.STOPPED}
)
_LABELS = {
    SessionState.STARTING: "Iniciando",
    SessionState.IDLE: "Lista",
    SessionState.WORKING: "Trabajando",
    SessionState.WAITING_APPROVAL: "Esperando aprobación",
    SessionState.STOPPING: "Deteniendo",
    SessionState.DONE: "Finalizado",
    SessionState.ERROR: "Error",
    SessionState.DISCONNECTED: "Desconectado",
    SessionState.STOPPED: "Detenido",
}


@dataclass(frozen=True)
class SessionSpec:
    """Qué lanzar. ``cwd`` siempre es la carpeta del proyecto: aísla el contexto."""

    project_id: int
    kind: AgentKind
    cwd: str
    title: str = ""
    # Argumentos adicionales para la CLI oficial. Nunca credenciales: cada CLI
    # usa su propia autenticación/suscripción ya configurada por el usuario.
    args: tuple[str, ...] = ()


@dataclass
class SessionInfo:
    id: str
    spec: SessionSpec
    state: SessionState = SessionState.STARTING
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    detail: str = ""
    exit_code: int | None = None

    @property
    def project_id(self) -> int:
        return self.spec.project_id


@dataclass(frozen=True)
class SessionEvent:
    """Notificación emitida por el gestor hacia la UI u otros observadores."""

    kind: str  # "started" | "state" | "output" | "removed"
    session: SessionInfo
    text: str = ""
