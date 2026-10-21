"""Herramientas oficiales de agentes y cómo lanzarlas (sin GTK).

Cada herramienta se lanza con su CLI oficial, en modo interactivo, dentro de la
carpeta del proyecto. IA Workspace no añade opciones de permisos: se aplican la
configuración y las aprobaciones que el usuario tenga en cada herramienta.

Opciones verificadas con la ayuda local de cada ejecutable (``--help``):

* Claude Code 2.1 — ``claude`` (interactivo por defecto), ``claude auth status --json``
  y ``claude auth login``.
* Codex CLI 0.163 — ``codex --no-daemon`` (TUI interactiva), ``codex login status`` y
  ``codex login``. Sin ``--no-daemon`` la TUI se conecta a un servidor compartido en
  segundo plano que exige el paquete local del instalador independiente; si Codex se
  instaló como servidor de aplicaciones de un plugin, falla con «this CLI has no complete
  local package». ``--no-daemon`` (opción oficial: «Run without the shared background
  server») ejecuta el servidor dentro del propio proceso: funciona en ambos tipos de
  instalación y mantiene todo dentro de la sesión de la terminal, de modo que el cierre
  controlado de IA Workspace también lo finaliza. No omite permisos ni aprobaciones.
* Google Antigravity — ``agy`` (CLI interactiva por defecto). No tiene comando de
  estado ni de inicio de sesión: la autenticación ocurre dentro de la propia sesión.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass

from ia_workspace.domain.models import AgentKind
from ia_workspace.services.integrations import EXECUTABLES

Which = Callable[[str], str | None]

# Opciones que omiten permisos, aprobaciones o el sandbox. Nunca se pasan
# automáticamente; ``build_launch_argv`` las rechaza aunque lleguen por error.
FORBIDDEN_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--permission-mode",
    "--ask-for-approval",
    "--sandbox",
    "--mode",
)


class ToolNotInstalledError(FileNotFoundError):
    """La herramienta oficial no está en el PATH. El mensaje es apto para la UI."""


class UnsafeArgumentError(ValueError):
    """Se intentó lanzar una herramienta con una opción que omite permisos."""


@dataclass(frozen=True)
class AgentTool:
    kind: AgentKind
    executables: tuple[str, ...]
    # Argumentos tras el ejecutable para cada uso (todos oficiales).
    interactive_args: tuple[str, ...] = ()
    status_args: tuple[str, ...] | None = None
    login_args: tuple[str, ...] | None = None
    install_hint: str = ""

    @property
    def label(self) -> str:
        return self.kind.label

    def find(self, which: Which = shutil.which) -> str | None:
        return next((path for name in self.executables if (path := which(name))), None)

    @property
    def supports_login(self) -> bool:
        return self.login_args is not None


TOOLS: dict[AgentKind, AgentTool] = {
    AgentKind.CLAUDE_CODE: AgentTool(
        kind=AgentKind.CLAUDE_CODE,
        executables=EXECUTABLES[AgentKind.CLAUDE_CODE],
        status_args=("auth", "status", "--json"),
        login_args=("auth", "login"),
        install_hint="Instala Claude Code desde https://claude.com/claude-code",
    ),
    AgentKind.CODEX: AgentTool(
        kind=AgentKind.CODEX,
        executables=EXECUTABLES[AgentKind.CODEX],
        interactive_args=("--no-daemon",),
        status_args=("login", "status"),
        login_args=("login",),
        install_hint="Instala Codex CLI desde https://developers.openai.com/codex",
    ),
    AgentKind.ANTIGRAVITY: AgentTool(
        kind=AgentKind.ANTIGRAVITY,
        executables=EXECUTABLES[AgentKind.ANTIGRAVITY],
        install_hint="Instala la CLI de Google Antigravity (agy)",
    ),
}


def describe_install(kind: AgentKind, executable: str | None) -> str | None:
    """Nota de diagnóstico sobre el tipo de instalación (solo lectura de rutas)."""
    if executable is None:
        return None
    resolved = os.path.realpath(executable)
    if kind is AgentKind.CODEX and "/.plugin-appserver/" in resolved:
        return (
            "Instalado como servidor de aplicaciones de un plugin, sin el paquete local del "
            "instalador independiente. IA Workspace lo inicia con --no-daemon."
        )
    return None


def _check_safe(argv: list[str]) -> list[str]:
    for arg in argv[1:]:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS or "dangerously" in flag:
            raise UnsafeArgumentError(f"Opción no permitida al lanzar un agente: {flag}")
    return argv


def _resolve(tool: AgentTool, which: Which) -> str:
    path = tool.find(which)
    if path is None:
        names = " o ".join(f"`{n}`" for n in tool.executables)
        raise ToolNotInstalledError(f"{tool.label} no está instalado (no se encontró {names} en el PATH).")
    return path


def build_launch_argv(kind: AgentKind, extra: tuple[str, ...] = (), which: Which = shutil.which) -> list[str]:
    """Comando de la sesión interactiva oficial. El directorio lo fija la terminal."""
    tool = TOOLS[kind]
    return _check_safe([_resolve(tool, which), *tool.interactive_args, *extra])


def build_login_argv(kind: AgentKind, which: Which = shutil.which) -> list[str]:
    """Comando del flujo oficial de inicio de sesión (lo completa el usuario en su navegador)."""
    tool = TOOLS[kind]
    if tool.login_args is None:
        raise ValueError(f"{tool.label} no tiene un comando de inicio de sesión separado.")
    return _check_safe([_resolve(tool, which), *tool.login_args])
