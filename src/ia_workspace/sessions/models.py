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
    DONE = "done"  # el proceso terminó con código 0
    FAILED = "failed"  # el proceso terminó con código distinto de 0 o por una señal ajena
    ERROR = "error"  # no se pudo iniciar o falló la integración"
    DISCONNECTED = "disconnected"
    STOPPED = "stopped"  # detenido por el usuario (o al salir de IA Workspace)

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
    {
        SessionState.DONE,
        SessionState.FAILED,
        SessionState.ERROR,
        SessionState.DISCONNECTED,
        SessionState.STOPPED,
    }
)
_LABELS = {
    SessionState.STARTING: "Iniciando",
    SessionState.IDLE: "Lista",
    SessionState.WORKING: "Trabajando",
    SessionState.WAITING_APPROVAL: "Esperando aprobación",
    SessionState.STOPPING: "Deteniendo",
    SessionState.DONE: "Finalizado correctamente",
    SessionState.FAILED: "Finalizado con error",
    SessionState.ERROR: "Error al iniciar",
    SessionState.DISCONNECTED: "Desconectado",
    SessionState.STOPPED: "Detenido por el usuario",
}


class BrainLinkStatus(StrEnum):
    APPLIED = "applied"  # la sesión recibió la referencia al iniciarse
    COPY = "copy"  # no hay mecanismo seguro: la referencia se ofrece para copiar
    UNAVAILABLE = "unavailable"  # no hay memoria o no hay Cerebro


@dataclass(frozen=True)
class BrainLink:
    """Cómo se presenta el Cerebro a una sesión nueva de agente (fase 3B).

    ``reference`` es la referencia mínima (índice y orden de consulta), nunca el contenido
    de la memoria. Con ``APPLIED`` y ``via_launch`` se pasa una sola vez en la línea de
    órdenes al lanzar la CLI; ``via_launch=False`` indica que se incluyó explícitamente al
    principio del prompt transferido.
    """

    status: BrainLinkStatus
    detail: str
    reference: str | None = None
    via_launch: bool = False

    @property
    def launch_reference(self) -> str | None:
        return self.reference if self.status is BrainLinkStatus.APPLIED and self.via_launch else None


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
    # "interactive": sesión de trabajo. "login": flujo oficial de inicio de sesión.
    mode: str = "interactive"
    # Prompt inicial para una sesión nueva, transmitido por el mecanismo oficial de
    # la CLI (nunca escrito después en una sesión ya en marcha). ``None`` si la
    # sesión no transfiere ningún prompt (terminal simple, inicio de sesión, etc.).
    initial_prompt: str | None = None
    # Integración con el Cerebro (solo sesiones de agente). ``None`` en terminales e
    # inicios de sesión.
    brain: BrainLink | None = None


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
