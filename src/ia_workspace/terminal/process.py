"""Procesos de una terminal gestionada (sin GTK).

VTE lanza cada terminal en una sesión POSIX nueva (``setsid``): el proceso hijo es
líder de sesión y su PID es el identificador de sesión. Todo proceso que la
terminal cree hereda ese identificador, incluidos los trabajos en segundo plano
y los que ignoran SIGHUP. Así se finalizan exactamente los procesos gestionados
por IA Workspace, nunca otros procesos del usuario.

Limitación conocida: un proceso que llame a ``setsid()`` por su cuenta (un
demonio) sale de la sesión. Se sigue localizando por ascendencia mientras su
padre exista.
"""

from __future__ import annotations

import os
import pwd
import shlex
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
FALLBACK_SHELL = "/bin/sh"

# Variables de configuración de IA Workspace: no se filtran a las terminales.
_DROPPED_PREFIX = "IAW_"


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    sid: int
    state: str
    name: str

    @property
    def alive(self) -> bool:
        return self.state not in ("Z", "X")


def resolve_shell_argv(env: Mapping[str, str] | None = None) -> list[str]:
    """Shell interactivo del usuario.

    ``IAW_TERMINAL_SHELL`` (solo desarrollo y pruebas) permite fijar un comando
    concreto, p. ej. ``/bin/bash --noprofile --norc``.
    """
    env = os.environ if env is None else env
    if override := env.get("IAW_TERMINAL_SHELL", "").strip():
        return shlex.split(override)
    candidates = [env.get("SHELL", "")]
    try:
        candidates.append(pwd.getpwuid(os.getuid()).pw_shell)
    except KeyError:
        pass
    for shell in candidates:
        if shell and os.path.isabs(shell) and os.access(shell, os.X_OK):
            return [shell]
    return [FALLBACK_SHELL]


def build_env(project_id: int, cwd: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Entorno heredado del usuario más marcas para identificar la terminal.

    No añade credenciales: cada herramienta usa su propia configuración.
    """
    base = {k: v for k, v in (os.environ if env is None else env).items() if not k.startswith(_DROPPED_PREFIX)}
    base.update(
        {
            "PWD": cwd,
            "COLORTERM": "truecolor",
            "IAW_PROJECT_ID": str(project_id),
        }
    )
    base.pop("TERM", None)  # VTE fija TERM=xterm-256color
    return [f"{k}={v}" for k, v in sorted(base.items())]


def read_proc(pid: int, proc: Path = PROC) -> ProcInfo | None:
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    # El nombre va entre paréntesis y puede contener espacios o paréntesis.
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end < 0:
        return None
    fields = raw[end + 2 :].split()
    try:
        return ProcInfo(
            pid=pid,
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            sid=int(fields[3]),
            state=fields[0],
            name=raw[start + 1 : end],
        )
    except (IndexError, ValueError):
        return None


def all_processes(proc: Path = PROC) -> list[ProcInfo]:
    result = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return result
    for entry in entries:
        if entry.name.isdigit() and (info := read_proc(int(entry.name), proc)) is not None:
            result.append(info)
    return result


def session_members(leader: int, processes: Iterable[ProcInfo] | None = None) -> list[ProcInfo]:
    """Procesos vivos de la terminal: misma sesión que el líder o descendientes suyos."""
    procs = list(all_processes() if processes is None else processes)
    by_parent: dict[int, list[ProcInfo]] = {}
    for p in procs:
        by_parent.setdefault(p.ppid, []).append(p)
    members = {p.pid: p for p in procs if p.sid == leader and p.alive}
    pending = [leader]
    while pending:
        for child in by_parent.get(pending.pop(), []):
            if child.alive and child.pid not in members:
                members[child.pid] = child
            if child.pid not in pending and child.pid != leader:
                pending.append(child.pid)
    return sorted(members.values(), key=lambda p: p.pid)


def _is_safe_target(pid: int) -> bool:
    own = os.getpid()
    return pid > 1 and pid != own and os.getsid(own) != pid


def signal_session(leader: int, sig: int, processes: Iterable[ProcInfo] | None = None) -> list[int]:
    """Envía ``sig`` a cada proceso de la terminal. Devuelve los PID señalizados."""
    if not _is_safe_target(leader):
        raise ValueError(f"PID {leader} no es una terminal gestionada")
    signalled = []
    for member in session_members(leader, processes):
        if not _is_safe_target(member.pid):
            continue
        try:
            os.kill(member.pid, sig)
            signalled.append(member.pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass  # no es nuestro (no debería ocurrir dentro de la sesión)
    return signalled


def kill_session(leader: int) -> list[int]:
    return signal_session(leader, signal.SIGKILL)


def has_foreground_work(leader: int) -> bool:
    """``True`` si la terminal ejecuta algo además de su shell (para confirmar al cerrar)."""
    return any(p.pid != leader for p in session_members(leader))
