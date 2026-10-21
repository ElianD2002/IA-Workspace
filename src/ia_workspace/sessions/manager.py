"""Gestor de sesiones concurrentes, con alcance de aplicación.

Vive en ``Application`` (no en la ventana): cambiar de proyecto, minimizar u
ocultar la ventana no afecta a las sesiones. Solo ``shutdown()`` las finaliza,
y se invoca cuando el usuario confirma el cierre completo de IA Workspace.

Todo el estado se muta en el hilo principal. Las notificaciones de los backends
(que pueden venir de otros hilos) se encolan con ``dispatch``; en la aplicación
GTK es ``GLib.idle_add`` y en las pruebas una llamada directa.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime

from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions.backend import SessionBackend, SessionHandle, ViewableHandle
from ia_workspace.sessions.models import SessionEvent, SessionInfo, SessionSpec, SessionState

Callback = Callable[[], None]
Dispatch = Callable[[Callback], None]
# schedule(segundos, callback) -> función para cancelar
Schedule = Callable[[float, Callback], Callback]
Listener = Callable[[SessionEvent], None]


class SessionError(RuntimeError):
    pass


def _direct(fn: Callback) -> None:
    fn()


def _no_timer(_seconds: float, _fn: Callback) -> Callback:
    return lambda: None


class _Sink:
    def __init__(self, manager: SessionManager, session_id: str) -> None:
        self._manager = manager
        self._id = session_id

    def state_changed(self, state: SessionState, detail: str = "", exit_code: int | None = None) -> None:
        self._manager._dispatch(lambda: self._manager._on_state(self._id, state, detail, exit_code))

    def output(self, text: str) -> None:
        self._manager._dispatch(lambda: self._manager._on_output(self._id, text))


class SessionManager:
    def __init__(self, dispatch: Dispatch = _direct, schedule: Schedule = _no_timer) -> None:
        self._dispatch = dispatch
        self._schedule = schedule
        self._backends: dict[AgentKind, SessionBackend] = {}
        self._sessions: dict[str, SessionInfo] = {}
        self._handles: dict[str, SessionHandle] = {}
        self._listeners: list[Listener] = []
        self._ids = itertools.count(1)
        self._shutdown_done: Callback | None = None
        self._shutdown_cancel_timer: Callback | None = None
        self._shutting_down = False

    # ---- registro de backends --------------------------------------------------

    def register_backend(self, backend: SessionBackend) -> None:
        self._backends[backend.kind] = backend

    def backend_for(self, kind: AgentKind) -> SessionBackend | None:
        return self._backends.get(kind)

    # ---- observadores ----------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callback:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, kind: str, info: SessionInfo, text: str = "") -> None:
        event = SessionEvent(kind=kind, session=info, text=text)
        for listener in list(self._listeners):
            listener(event)

    # ---- consultas -------------------------------------------------------------

    def get(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def handle(self, session_id: str) -> SessionHandle | None:
        """Handle de la sesión, incluso ya terminada (para mostrar su salida final)."""
        return self._handles.get(session_id)

    def sessions(self, project_id: int | None = None) -> list[SessionInfo]:
        return [s for s in self._sessions.values() if project_id is None or s.project_id == project_id]

    def active_sessions(self, project_id: int | None = None) -> list[SessionInfo]:
        return [s for s in self.sessions(project_id) if s.state.is_active]

    @property
    def has_active(self) -> bool:
        return any(s.state.is_active for s in self._sessions.values())

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    # ---- ciclo de vida ---------------------------------------------------------

    def start(self, spec: SessionSpec) -> SessionInfo:
        if self._shutting_down:
            raise SessionError("IA Workspace se está cerrando; no se pueden iniciar sesiones.")
        backend = self._backends.get(spec.kind)
        if backend is None:
            raise SessionError(f"No hay integración disponible para {spec.kind.label}.")
        info = SessionInfo(id=f"s{next(self._ids)}", spec=spec)
        self._sessions[info.id] = info
        self._emit("started", info)
        try:
            self._handles[info.id] = backend.start(spec, _Sink(self, info.id))
        except Exception as exc:  # el backend no pudo lanzar el proceso
            self._on_state(info.id, SessionState.ERROR, str(exc), None)
        return info

    def send_input(self, session_id: str, text: str) -> None:
        info = self._sessions.get(session_id)
        handle = self._handles.get(session_id)
        if info is None or handle is None or not info.state.is_active:
            raise SessionError("La sesión no está activa.")
        handle.send_input(text)

    def stop(self, session_id: str, *, force: bool = False) -> None:
        info = self._sessions.get(session_id)
        handle = self._handles.get(session_id)
        if info is None or handle is None or not info.state.is_active:
            return
        if info.state is not SessionState.STOPPING:
            info.state = SessionState.STOPPING
            self._emit("state", info)
        handle.stop(force=force)

    def remove(self, session_id: str) -> None:
        """Descarta del registro una sesión ya terminada."""
        info = self._sessions.get(session_id)
        if info is None:
            return
        if info.state.is_active:
            raise SessionError("Detén la sesión antes de descartarla.")
        del self._sessions[session_id]
        handle = self._handles.pop(session_id, None)
        self._emit("removed", info)
        if isinstance(handle, ViewableHandle):
            handle.dispose()

    def shutdown(self, on_done: Callback, grace_seconds: float = 10.0) -> None:
        """Finalización controlada de todas las sesiones activas.

        Pide un cierre ordenado a cada sesión; si al cabo de ``grace_seconds``
        alguna sigue viva, la fuerza. ``on_done`` se llama exactamente una vez.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        self._shutdown_done = on_done
        active = self.active_sessions()
        if not active:
            self._finish_shutdown()
            return
        self._shutdown_cancel_timer = self._schedule(grace_seconds, self._force_remaining)
        for info in active:
            self.stop(info.id)

    def _force_remaining(self) -> None:
        self._shutdown_cancel_timer = None
        for info in self.active_sessions():
            handle = self._handles.get(info.id)
            if handle is not None:
                handle.stop(force=True)
            # Si el backend no confirma, se da por detenida para no bloquear la salida.
            if info.state.is_active:
                self._on_state(info.id, SessionState.STOPPED, "Finalizada a la fuerza al salir", None)
        self._maybe_finish_shutdown()

    def _maybe_finish_shutdown(self) -> None:
        if self._shutting_down and self._shutdown_done is not None and not self.has_active:
            self._finish_shutdown()

    def _finish_shutdown(self) -> None:
        if self._shutdown_cancel_timer is not None:
            self._shutdown_cancel_timer()
            self._shutdown_cancel_timer = None
        done, self._shutdown_done = self._shutdown_done, None
        if done is not None:
            done()

    # ---- notificaciones de backends (hilo principal) ---------------------------

    def _on_state(self, session_id: str, state: SessionState, detail: str, exit_code: int | None) -> None:
        info = self._sessions.get(session_id)
        if info is None or info.state.is_terminal:
            return
        info.state = state
        info.detail = detail
        if state.is_terminal:
            # El handle se conserva hasta remove(): su salida final sigue visible.
            info.ended_at = datetime.now(UTC)
            info.exit_code = exit_code
        self._emit("state", info)
        if state.is_terminal:
            self._maybe_finish_shutdown()

    def _on_output(self, session_id: str, text: str) -> None:
        info = self._sessions.get(session_id)
        if info is not None:
            self._emit("output", info, text)
