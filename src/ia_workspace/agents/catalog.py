"""Catálogo de agentes: instalación y estado de autenticación (sin GTK).

La detección del ejecutable es inmediata (``PATH``). La autenticación se comprueba
en un hilo, porque los comandos de estado pueden tardar, y el resultado se entrega
en el hilo principal mediante ``dispatch``. Solo se guarda en memoria el estado
(``AuthState``), nunca la salida de los comandos.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass

from ia_workspace.agents import auth
from ia_workspace.agents.tools import TOOLS, AgentTool, Which
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind

Callback = Callable[[], None]
Dispatch = Callable[[Callback], None]
Spawn = Callable[[Callback], None]
Listener = Callable[[AgentKind], None]


def _thread(fn: Callback) -> None:
    threading.Thread(target=fn, daemon=True, name="iaw-auth-status").start()


def _direct(fn: Callback) -> None:
    fn()


@dataclass(frozen=True)
class AgentStatus:
    kind: AgentKind
    executable: str | None
    auth: auth.AuthState
    checking: bool = False

    @property
    def installed(self) -> bool:
        return self.executable is not None

    @property
    def tool(self) -> AgentTool:
        return TOOLS[self.kind]

    @property
    def summary(self) -> str:
        if not self.installed:
            return "No instalado"
        if self.checking:
            return "Comprobando…"
        return self.auth.label


class AgentCatalog:
    def __init__(
        self,
        dispatch: Dispatch = _direct,
        which: Which = shutil.which,
        run: auth.Runner = auth.run_command,
        spawn: Spawn = _thread,
    ) -> None:
        self._dispatch = dispatch
        self._which = which
        self._run = run
        self._spawn = spawn
        self._listeners: list[Listener] = []
        self._status: dict[AgentKind, AgentStatus] = {
            kind: self._initial(kind) for kind in SELECTABLE_AGENTS
        }
        self._checked: set[AgentKind] = set()

    def _initial(self, kind: AgentKind) -> AgentStatus:
        path = TOOLS[kind].find(self._which)
        return AgentStatus(kind, path, auth.AuthState.UNKNOWN if path else auth.AuthState.NOT_INSTALLED)

    def subscribe(self, listener: Listener) -> Callback:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, kind: AgentKind) -> None:
        for listener in list(self._listeners):
            listener(kind)

    def status(self, kind: AgentKind) -> AgentStatus:
        return self._status[kind]

    def statuses(self) -> list[AgentStatus]:
        return [self._status[k] for k in SELECTABLE_AGENTS]

    def which(self, name: str) -> str | None:
        return self._which(name)

    def detect(self) -> None:
        """Vuelve a buscar los ejecutables (p. ej. tras instalar una herramienta)."""
        for kind in SELECTABLE_AGENTS:
            current = self._status[kind]
            path = TOOLS[kind].find(self._which)
            if path != current.executable:
                state = current.auth if path else auth.AuthState.NOT_INSTALLED
                self._status[kind] = AgentStatus(kind, path, state)
                self._checked.discard(kind)
                self._emit(kind)

    def ensure_checked(self, kind: AgentKind) -> None:
        if kind not in self._checked:
            self.refresh(kind)

    def refresh(self, kind: AgentKind) -> None:
        """Comprueba (en segundo plano) si hay sesión iniciada en la herramienta."""
        current = self._status[kind]
        if current.checking:
            return
        path = TOOLS[kind].find(self._which)
        if path is None:
            self._status[kind] = AgentStatus(kind, None, auth.AuthState.NOT_INSTALLED)
            self._checked.add(kind)
            self._emit(kind)
            return
        self._status[kind] = AgentStatus(kind, path, current.auth, checking=True)
        self._emit(kind)

        def work() -> None:
            state = auth.check_kind(kind, self._which, self._run)
            self._dispatch(lambda: self._finish(kind, path, state))

        self._spawn(work)

    def _finish(self, kind: AgentKind, path: str, state: auth.AuthState) -> None:
        self._status[kind] = AgentStatus(kind, path, state)
        self._checked.add(kind)
        self._emit(kind)
