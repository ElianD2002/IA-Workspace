from __future__ import annotations

import queue
import time
import unittest

import helpers  # noqa: F401  (añade src/ al path)

from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions import SessionBackend, SessionHandle
from ia_workspace.sessions.manager import SessionError, SessionManager
from ia_workspace.sessions.models import SessionEvent, SessionSpec, SessionState
from ia_workspace.sessions.simulated import SimulatedBackend


class FakeHandle:
    def __init__(self, sink, graceful: bool = True) -> None:
        self.sink = sink
        self.graceful = graceful
        self.inputs: list[str] = []
        self.stops: list[bool] = []
        sink.state_changed(SessionState.IDLE)

    def send_input(self, text: str) -> None:
        self.inputs.append(text)

    def stop(self, *, force: bool = False) -> None:
        self.stops.append(force)
        if force or self.graceful:
            self.sink.state_changed(SessionState.STOPPED, "", -9 if force else 0)


class FakeBackend:
    def __init__(self, kind: AgentKind, graceful: bool = True, fail: bool = False) -> None:
        self.kind = kind
        self.graceful = graceful
        self.fail = fail
        self.handles: list[FakeHandle] = []

    def is_available(self) -> bool:
        return True

    def start(self, spec: SessionSpec, sink) -> FakeHandle:
        if self.fail:
            raise OSError("no se pudo lanzar")
        handle = FakeHandle(sink, self.graceful)
        self.handles.append(handle)
        return handle


class FakeTimers:
    def __init__(self) -> None:
        self.pending: list = []

    def schedule(self, _seconds: float, fn):
        entry = [fn, True]
        self.pending.append(entry)
        return lambda: entry.__setitem__(1, False)

    def fire_all(self) -> None:
        for fn, alive in list(self.pending):
            if alive:
                fn()
        self.pending.clear()


def spec(project_id: int, kind: AgentKind = AgentKind.CLAUDE_CODE) -> SessionSpec:
    return SessionSpec(project_id=project_id, kind=kind, cwd=f"/p{project_id}", title=f"t{project_id}")


class SessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timers = FakeTimers()
        self.manager = SessionManager(schedule=self.timers.schedule)
        self.backend = FakeBackend(AgentKind.CLAUDE_CODE)
        self.manager.register_backend(self.backend)
        self.events: list[SessionEvent] = []
        self.manager.subscribe(self.events.append)

    def test_protocols(self) -> None:
        self.assertIsInstance(self.backend, SessionBackend)
        self.assertIsInstance(FakeHandle(_NullSink()), SessionHandle)

    def test_concurrent_sessions_are_scoped_per_project(self) -> None:
        a1 = self.manager.start(spec(1))
        a2 = self.manager.start(spec(1))
        b1 = self.manager.start(spec(2))
        self.assertEqual({s.id for s in self.manager.sessions(1)}, {a1.id, a2.id})
        self.assertEqual([s.id for s in self.manager.sessions(2)], [b1.id])
        self.assertEqual(len(self.manager.active_sessions()), 3)
        self.manager.stop(a1.id)
        self.assertEqual(self.manager.get(a1.id).state, SessionState.STOPPED)
        # Detener una sesión de A no afecta a B.
        self.assertEqual(self.manager.get(b1.id).state, SessionState.IDLE)

    def test_unknown_backend_and_start_failure(self) -> None:
        with self.assertRaises(SessionError):
            self.manager.start(spec(1, AgentKind.CODEX))
        self.manager.register_backend(FakeBackend(AgentKind.CODEX, fail=True))
        info = self.manager.start(spec(1, AgentKind.CODEX))
        self.assertEqual(info.state, SessionState.ERROR)
        self.assertIn("no se pudo lanzar", info.detail)

    def test_input_and_remove(self) -> None:
        info = self.manager.start(spec(1))
        self.manager.send_input(info.id, "hola")
        self.assertEqual(self.backend.handles[0].inputs, ["hola"])
        with self.assertRaises(SessionError):
            self.manager.remove(info.id)
        self.manager.stop(info.id)
        self.manager.remove(info.id)
        self.assertIsNone(self.manager.get(info.id))
        with self.assertRaises(SessionError):
            self.manager.send_input(info.id, "x")
        self.assertEqual(self.events[-1].kind, "removed")

    def test_terminal_state_is_final(self) -> None:
        info = self.manager.start(spec(1))
        self.manager.stop(info.id)
        self.backend.handles[0].sink.state_changed(SessionState.WORKING)
        self.assertEqual(info.state, SessionState.STOPPED)

    def test_shutdown_without_sessions_finishes_immediately(self) -> None:
        done: list[bool] = []
        self.manager.shutdown(lambda: done.append(True))
        self.assertEqual(done, [True])
        with self.assertRaises(SessionError):
            self.manager.start(spec(1))

    def test_graceful_shutdown(self) -> None:
        self.manager.start(spec(1))
        self.manager.start(spec(2))
        done: list[bool] = []
        self.manager.shutdown(lambda: done.append(True))
        self.assertEqual(done, [True])
        self.assertTrue(all(h.stops == [False] for h in self.backend.handles))
        self.timers.fire_all()  # el temporizador se canceló: no se fuerza nada
        self.assertEqual(done, [True])

    def test_forced_shutdown_after_grace(self) -> None:
        stubborn = FakeBackend(AgentKind.CODEX, graceful=False)
        self.manager.register_backend(stubborn)
        self.manager.start(spec(1))
        slow = self.manager.start(spec(2, AgentKind.CODEX))
        done: list[bool] = []
        self.manager.shutdown(lambda: done.append(True))
        self.assertEqual(done, [])
        self.assertTrue(self.manager.has_active)
        self.assertEqual(self.manager.get(slow.id).state, SessionState.STOPPING)
        self.timers.fire_all()
        self.assertEqual(stubborn.handles[0].stops, [False, True])
        self.assertFalse(self.manager.has_active)
        self.assertEqual(done, [True])
        self.manager.shutdown(lambda: done.append(True))  # segunda llamada: sin efecto
        self.assertEqual(done, [True])


class _NullSink:
    def state_changed(self, *args, **kwargs) -> None: ...

    def output(self, text: str) -> None: ...


class SimulatedBackendTests(unittest.TestCase):
    """El backend simulado usa hilos; el gestor recibe todo en «su» hilo vía dispatch."""

    def test_runs_in_background_and_stops(self) -> None:
        inbox: queue.Queue = queue.Queue()
        manager = SessionManager(dispatch=inbox.put)
        manager.register_backend(SimulatedBackend(AgentKind.CODEX, step_seconds=0.01))
        info = manager.start(spec(1, AgentKind.CODEX))

        def pump(until, timeout: float = 3.0) -> None:
            deadline = time.monotonic() + timeout
            while not until() and time.monotonic() < deadline:
                try:
                    inbox.get(timeout=0.05)()
                except queue.Empty:
                    pass

        pump(lambda: info.state is SessionState.IDLE)
        self.assertEqual(info.state, SessionState.IDLE)
        manager.stop(info.id)
        pump(lambda: info.state.is_terminal)
        self.assertEqual(info.state, SessionState.STOPPED)
        self.assertEqual(info.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
