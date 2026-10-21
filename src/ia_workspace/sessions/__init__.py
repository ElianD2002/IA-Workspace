"""Contratos del futuro gestor de sesiones concurrentes de agentes.

En esta fase no hay integraciones reales: se definen las interfaces y un
``SessionManager`` funcional que los backends (Claude Code, Codex, Antigravity,
terminal) implementarán en fases posteriores.
"""

from ia_workspace.sessions.backend import SessionBackend, SessionHandle, SessionSink
from ia_workspace.sessions.manager import SessionManager
from ia_workspace.sessions.models import SessionEvent, SessionInfo, SessionSpec, SessionState

__all__ = [
    "SessionBackend",
    "SessionEvent",
    "SessionHandle",
    "SessionInfo",
    "SessionManager",
    "SessionSink",
    "SessionSpec",
    "SessionState",
]
