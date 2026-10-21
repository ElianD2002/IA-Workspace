"""Backend simulado, solo para desarrollo y pruebas.

No lanza procesos ni contacta servicios: emite estados y salida ficticios desde
un hilo, igual que lo hará un backend real. Se activa con
``IAW_SIMULATED_AGENTS=1`` y la UI lo marca como «SIMULADA».
"""

from __future__ import annotations

import threading

from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions.backend import SessionSink
from ia_workspace.sessions.models import SessionSpec, SessionState

_SCRIPT = (
    "Leyendo el contexto del proyecto…",
    "Analizando la estructura de carpetas…",
    "Esperando instrucciones (sesión simulada)",
)


class _SimulatedHandle:
    def __init__(self, sink: SessionSink, step_seconds: float) -> None:
        self._sink = sink
        self._step = step_seconds
        self._stop = threading.Event()
        self._killed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._sink.state_changed(SessionState.WORKING)
        for line in _SCRIPT:
            if self._stop.wait(self._step):
                break
            self._sink.output(line)
        else:
            self._sink.state_changed(SessionState.IDLE)
            self._stop.wait()
        self._killed.wait(self._step)  # simula un cierre ordenado, salvo si se fuerza
        self._sink.state_changed(SessionState.STOPPED, "Detenida", 0 if not self._killed.is_set() else -9)

    def send_input(self, text: str) -> None:
        self._sink.output(f"› {text}")

    def stop(self, *, force: bool = False) -> None:
        if force:
            self._killed.set()
        self._stop.set()


class SimulatedBackend:
    def __init__(self, kind: AgentKind, step_seconds: float = 0.6) -> None:
        self.kind = kind
        self._step = step_seconds

    def is_available(self) -> bool:
        return True

    def start(self, spec: SessionSpec, sink: SessionSink) -> _SimulatedHandle:
        return _SimulatedHandle(sink, self._step)
