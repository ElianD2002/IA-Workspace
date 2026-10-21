"""Estado de autenticación de cada herramienta, con sus comandos oficiales (sin GTK).

Privacidad: la salida de los comandos de estado puede incluir el correo o la
organización del usuario. Solo se interpreta si hay sesión iniciada; la salida se
descarta y nunca se devuelve, registra ni guarda. IA Workspace no lee los archivos
de credenciales de ninguna herramienta.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ia_workspace.agents.tools import TOOLS, AgentTool, Which
from ia_workspace.domain.models import AgentKind

STATUS_TIMEOUT_SECONDS = 15


class AuthState(StrEnum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    UNKNOWN = "unknown"  # sin comando de estado, o no se pudo determinar
    NOT_INSTALLED = "not_installed"

    @property
    def label(self) -> str:
        return {
            AuthState.LOGGED_IN: "Sesión iniciada",
            AuthState.LOGGED_OUT: "Requiere iniciar sesión",
            AuthState.UNKNOWN: "Se verifica al iniciar",
            AuthState.NOT_INSTALLED: "No instalado",
        }[self]


@dataclass(frozen=True)
class CompletedCommand:
    returncode: int
    stdout: str


# run(argv) -> CompletedCommand. Inyectable para las pruebas (dobles sin red ni IA).
Runner = Callable[[list[str]], CompletedCommand]


def run_command(argv: list[str]) -> CompletedCommand:
    env = {k: v for k, v in os.environ.items() if not k.startswith("IAW_")}
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return CompletedCommand(returncode=-1, stdout="")
    return CompletedCommand(returncode=proc.returncode, stdout=proc.stdout)


def parse_claude_status(result: CompletedCommand) -> AuthState:
    """``claude auth status --json`` → ``{"loggedIn": bool, ...}``."""
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return AuthState.UNKNOWN
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return AuthState.UNKNOWN
    return AuthState.LOGGED_IN if data["loggedIn"] else AuthState.LOGGED_OUT


def parse_codex_status(result: CompletedCommand) -> AuthState:
    """``codex login status`` → «Logged in using …» (código 0) o «Not logged in»."""
    text = result.stdout.strip().lower()
    if result.returncode == 0 and text.startswith("logged in"):
        return AuthState.LOGGED_IN
    if "not logged in" in text or (result.returncode not in (0, -1) and text):
        return AuthState.LOGGED_OUT
    return AuthState.UNKNOWN


PARSERS: dict[AgentKind, Callable[[CompletedCommand], AuthState]] = {
    AgentKind.CLAUDE_CODE: parse_claude_status,
    AgentKind.CODEX: parse_codex_status,
}


def check_auth(tool: AgentTool, which: Which = shutil.which, run: Runner = run_command) -> AuthState:
    path = tool.find(which)
    if path is None:
        return AuthState.NOT_INSTALLED
    parser = PARSERS.get(tool.kind)
    if tool.status_args is None or parser is None:
        return AuthState.UNKNOWN
    return parser(run([path, *tool.status_args]))


def check_kind(kind: AgentKind, which: Which = shutil.which, run: Runner = run_command) -> AuthState:
    return check_auth(TOOLS[kind], which, run)
