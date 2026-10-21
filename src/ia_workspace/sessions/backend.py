"""Contratos que implementará cada integración de agente.

Un backend sabe lanzar la herramienta oficial (p. ej. la CLI ``claude`` o
``codex``) dentro de la carpeta del proyecto y traducir su actividad a estados.
Puede trabajar en hilos o con E/S asíncrona de GLib: el ``SessionManager`` se
encarga de llevar las notificaciones al hilo principal.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions.models import SessionSpec, SessionState


class SessionSink(Protocol):
    """Canal por el que un backend informa al gestor. Seguro desde cualquier hilo."""

    def state_changed(self, state: SessionState, detail: str = "", exit_code: int | None = None) -> None: ...

    def output(self, text: str) -> None: ...


@runtime_checkable
class SessionHandle(Protocol):
    """Proceso vivo asociado a una sesión."""

    def send_input(self, text: str) -> None: ...

    def stop(self, *, force: bool = False) -> None:
        """Solicita la finalización.

        ``force=False``: cierre ordenado (SIGINT/SIGTERM, guardar estado).
        ``force=True``: terminación inmediata (SIGKILL). En ambos casos el backend
        debe terminar informando un estado terminal mediante el ``SessionSink``.
        """
        ...


@runtime_checkable
class SessionBackend(Protocol):
    kind: AgentKind

    def is_available(self) -> bool:
        """``True`` si la herramienta oficial está instalada y utilizable."""
        ...

    def start(self, spec: SessionSpec, sink: SessionSink) -> SessionHandle: ...
