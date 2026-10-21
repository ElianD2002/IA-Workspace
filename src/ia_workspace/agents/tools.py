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

Transferencia de un prompt inicial a una sesión nueva (fase 2B.2), verificada con
``--help`` y, lanzando cada CLI en un terminal real hasta su primera pantalla de
confirmación (sin aceptarla, sin enviar nada al modelo):

* Claude Code y Codex aceptan ``[prompt]`` como argumento posicional al final de la
  línea de órdenes (confirmado en ambas ayudas: «Arguments: prompt  Your prompt» /
  «[PROMPT]  Optional user prompt to start the session»). Se antepone un ``--``
  literal, la forma estándar de Commander.js (Claude Code) y clap (Codex, en Rust)
  para que un prompt que empezara por «-» nunca se confunda con una opción; el
  propio mensaje de error de Codex para una opción no reconocida lo indica
  explícitamente: «tip: to pass '...' as a value, use '-- ...'». Comprobado en un
  terminal real con un prompt que empezaba por «-»: Codex mostró su diálogo de
  confianza de carpeta en vez de un error de opción desconocida.
* Antigravity (``agy``) acepta ``--prompt-interactive <texto>`` (alias corto
  ``-i``): «Run an initial prompt interactively and continue the session».
  Comprobado en un terminal real: con esa opción, ``agy`` llega a su propio diálogo
  de confianza de carpeta con normalidad.

En los tres casos, el texto del prompt viaja como un único elemento de ``argv`` al
proceso (``Vte.Terminal.spawn_async`` usa ``execve``, no un intérprete de órdenes),
así que nunca pasa por un intérprete de shell: son seguros los saltos de línea,
las comillas, las barras invertidas y cualquier carácter especial.
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
    # Mecanismo oficial para pasar un prompt inicial a una sesión nueva. Como mucho
    # uno de los dos está activo: posicional (precedido de ``--``) o una opción con
    # valor (p. ej. ``--prompt-interactive``). Ninguno de los dos implica omitir
    # aprobaciones: el agente sigue pidiendo confirmación igual que si el texto se
    # escribiera a mano.
    prompt_positional: bool = False
    prompt_flag: str | None = None

    @property
    def label(self) -> str:
        return self.kind.label

    def find(self, which: Which = shutil.which) -> str | None:
        return next((path for name in self.executables if (path := which(name))), None)

    @property
    def supports_login(self) -> bool:
        return self.login_args is not None

    @property
    def supports_direct_prompt(self) -> bool:
        return self.prompt_positional or self.prompt_flag is not None


TOOLS: dict[AgentKind, AgentTool] = {
    AgentKind.CLAUDE_CODE: AgentTool(
        kind=AgentKind.CLAUDE_CODE,
        executables=EXECUTABLES[AgentKind.CLAUDE_CODE],
        status_args=("auth", "status", "--json"),
        login_args=("auth", "login"),
        install_hint="Instala Claude Code desde https://claude.com/claude-code",
        prompt_positional=True,
    ),
    AgentKind.CODEX: AgentTool(
        kind=AgentKind.CODEX,
        executables=EXECUTABLES[AgentKind.CODEX],
        interactive_args=("--no-daemon",),
        status_args=("login", "status"),
        login_args=("login",),
        install_hint="Instala Codex CLI desde https://developers.openai.com/codex",
        prompt_positional=True,
    ),
    AgentKind.ANTIGRAVITY: AgentTool(
        kind=AgentKind.ANTIGRAVITY,
        executables=EXECUTABLES[AgentKind.ANTIGRAVITY],
        install_hint="Instala la CLI de Google Antigravity (agy)",
        prompt_flag="--prompt-interactive",
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


def _check_safe(flags: list[str]) -> list[str]:
    """Rechaza opciones que omiten permisos, aprobaciones o el sandbox.

    Solo recibe las *opciones* que IA Workspace elige añadir (nunca el texto de un
    prompt): así un prompt que mencione la palabra «dangerously» en una frase
    normal jamás se confunde con una opción peligrosa.
    """
    for arg in flags:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS or "dangerously" in flag:
            raise UnsafeArgumentError(f"Opción no permitida al lanzar un agente: {flag}")
    return flags


def _resolve(tool: AgentTool, which: Which) -> str:
    path = tool.find(which)
    if path is None:
        names = " o ".join(f"`{n}`" for n in tool.executables)
        raise ToolNotInstalledError(f"{tool.label} no está instalado (no se encontró {names} en el PATH).")
    return path


def build_launch_argv(
    kind: AgentKind,
    extra: tuple[str, ...] = (),
    prompt: str | None = None,
    which: Which = shutil.which,
) -> list[str]:
    """Comando de la sesión interactiva oficial. El directorio lo fija la terminal.

    Si ``prompt`` no es ``None``, se añade como instrucción inicial mediante el
    mecanismo oficial de la herramienta (ver el docstring del módulo): un
    argumento posicional precedido de ``--``, o una opción con valor. El texto del
    prompt se añade después de validar las opciones, así que nunca puede disparar
    ``UnsafeArgumentError`` por su contenido, y nunca se interpreta como una opción
    aunque empiece por «-».
    """
    tool = TOOLS[kind]
    flags = [*tool.interactive_args, *extra]
    _check_safe(flags)
    argv = [_resolve(tool, which), *flags]
    if prompt is None:
        return argv
    if tool.prompt_flag:
        return [*argv, tool.prompt_flag, prompt]
    if tool.prompt_positional:
        return [*argv, "--", prompt]
    raise ValueError(f"{tool.label} no admite transferencia directa de un prompt inicial.")


def build_login_argv(kind: AgentKind, which: Which = shutil.which) -> list[str]:
    """Comando del flujo oficial de inicio de sesión (lo completa el usuario en su navegador)."""
    tool = TOOLS[kind]
    if tool.login_args is None:
        raise ValueError(f"{tool.label} no tiene un comando de inicio de sesión separado.")
    _check_safe(list(tool.login_args))
    return [_resolve(tool, which), *tool.login_args]
