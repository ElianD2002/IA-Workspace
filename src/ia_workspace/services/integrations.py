"""Detección de las herramientas oficiales instaladas.

Solo comprueba si el ejecutable está en el ``PATH``: no lo ejecuta, no lee sus
archivos de configuración y no toca credenciales. Cada herramienta mantiene su
propio inicio de sesión y su suscripción; IA Workspace no pide claves API.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass

from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind

# Ejecutables oficiales conocidos por herramienta, en orden de preferencia.
EXECUTABLES: dict[AgentKind, tuple[str, ...]] = {
    AgentKind.CLAUDE_CODE: ("claude",),
    AgentKind.CODEX: ("codex",),
    AgentKind.ANTIGRAVITY: ("antigravity", "agy"),
}


@dataclass(frozen=True)
class ToolStatus:
    kind: AgentKind
    executable: str | None  # ruta absoluta si se encontró

    @property
    def installed(self) -> bool:
        return self.executable is not None


def detect_tools(which: Callable[[str], str | None] = shutil.which) -> list[ToolStatus]:
    statuses = []
    for kind in SELECTABLE_AGENTS:
        found = next((path for name in EXECUTABLES[kind] if (path := which(name))), None)
        statuses.append(ToolStatus(kind=kind, executable=found))
    return statuses


def summary(statuses: list[ToolStatus]) -> str:
    installed = sum(s.installed for s in statuses)
    return f"{installed} de {len(statuses)} detectadas"
