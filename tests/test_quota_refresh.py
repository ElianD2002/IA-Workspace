"""Actualización de cuotas en segundo plano (corrección de la fase 5B.2).

Reproduce el bloqueo «Actualizando…»: el hilo de consulta escribía en SQLite con la
conexión del hilo principal, la excepción se repetía dentro del ``except`` y el aviso de
finalización nunca llegaba. Aquí los hilos son reales y la base de datos es un archivo, como
en la aplicación; ``dispatch`` encola en una cola que vacía el hilo principal (el papel de
``GLib.idle_add``). Las CLI son scripts falsos: nunca se ejecuta ``codex`` ni ``agy`` reales.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import threading
import time
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import temp_dir

from ia_workspace.domain.models import AgentKind
from ia_workspace.persistence.database import Database
from ia_workspace.quota import (
    AntigravityQuotaConnector,
    CodexQuotaConnector,
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaRepository,
    QuotaService,
    QuotaStatus,
    QuotaWindow,
    parse_claude_statusline_payload,
)

CODEX_RESULT = {
    "planType": "plus",
    "rateLimits": {
        "primary": {"usedPercent": 42.0, "windowDurationMins": 300, "resetsAt": 1893456000},
        "secondary": {"usedPercent": 10.0, "windowDurationMins": 10080, "resetsAt": 1894000000},
    },
}

AGY_RESULT = {
    "status": "SUCCESS",
    "num_turns": 0,
    "usage": {"total_tokens": 0},
    "models": [{"name": "Gemini Pro", "remainingFraction": 0.75, "resetTime": "2030-01-01T00:00:00Z"}],
}

# app-server falso: exige «initialized» antes de leer los límites, como el protocolo real.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys, time
mode = os.environ.get("FAKE_QUOTA_MODE", "ok")
if sys.argv[1:] != ["app-server"]:
    sys.exit(64)
if mode == "crash":
    sys.exit(3)
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"userAgent": "fake"}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "account/rateLimits/read":
        if mode == "hang":
            time.sleep(120)
        if mode == "error":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": "sin sesión"}}), flush=True)
        elif not initialized:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -2, "message": "not initialized"}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": @RESULT@}), flush=True)
'''.replace("@RESULT@", json.dumps(CODEX_RESULT))

# agy falso. «grandchild_hang» deja un proceso nieto con stdout abierto: con subprocess.run
# la espera no terminaría nunca aunque venciera el timeout.
FAKE_AGY = r'''#!/bin/bash
[ "$1 $2 $3 $4" = "-p /usage --output-format json" ] || exit 64
case "${FAKE_QUOTA_MODE:-ok}" in
  ok) printf '%s\n' '@RESULT@' ;;
  fail) echo "error de autenticación" >&2; exit 1 ;;
  grandchild_hang) sleep 120 & echo $! > "$FAKE_QUOTA_PIDFILE"; sleep 120 ;;
esac
'''.replace("@RESULT@", json.dumps(AGY_RESULT))


def write_exe(folder: Path, name: str, content: str) -> Path:
    path = folder / name
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class FakeConnector:
    """Conector controlable desde la prueba (cuenta llamadas, bloquea o falla)."""

    def __init__(self, provider: AgentKind, behaviour: Callable[[], ProviderQuotaSnapshot] | None = None) -> None:
        self.provider = provider
        self.calls = 0
        self.threads: list[threading.Thread] = []
        self.behaviour = behaviour or self.available

    def available(self) -> ProviderQuotaSnapshot:
        window = QuotaWindow(window_id="primary", name="5h", metric_type=QuotaMetricType.USED_PERCENT,
                             percentage=25.0, window_minutes=300)
        return ProviderQuotaSnapshot(provider=self.provider, status=QuotaStatus.AVAILABLE, plan_type="plus",
                                     groups=(QuotaGroup(group_id="g", name="G", windows=(window,)),),
                                     updated_at=datetime.now(timezone.utc))

    def fetch(self) -> ProviderQuotaSnapshot:
        self.calls += 1
        self.threads.append(threading.current_thread())
        return self.behaviour()


def failing(detail: str = "fallo simulado") -> Callable[[], ProviderQuotaSnapshot]:
    def run() -> ProviderQuotaSnapshot:
        raise RuntimeError(detail)

    return run


class RefreshCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.db = Database(Path(self.tmp.name) / "ia-workspace.db")  # archivo, como en la app
        self.queue: queue.Queue = queue.Queue()
        self.dispatched_on: list[threading.Thread] = []
        self.codex = FakeConnector(AgentKind.CODEX)
        self.agy = FakeConnector(AgentKind.ANTIGRAVITY)
        self.service = self.make_service()
        self.release = threading.Event()

    def tearDown(self) -> None:
        self.release.set()  # libera conectores bloqueados a propósito
        self.db.close()
        self.tmp.cleanup()

    def make_service(self, **kw) -> QuotaService:
        def dispatch(fn) -> None:  # como GLib.idle_add: se ejecuta luego en el hilo principal
            self.queue.put(fn)

        return QuotaService(QuotaRepository(self.db), self.codex, self.agy, dispatch=dispatch, **kw)

    def pump(self, until: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return True
            try:
                fn = self.queue.get(timeout=0.02)
            except queue.Empty:
                continue
            self.dispatched_on.append(threading.current_thread())
            fn()
        return until()

    def refresh_both(self, timeout: float = 5.0) -> dict[AgentKind, ProviderQuotaSnapshot]:
        results: dict[AgentKind, ProviderQuotaSnapshot] = {}
        finished: list[bool] = []
        for kind in (AgentKind.CODEX, AgentKind.ANTIGRAVITY):
            self.service.refresh_provider(kind, force=True, callback=lambda s, k=kind: results.__setitem__(k, s))
        self.service_done = finished
        self.assertTrue(self.pump(lambda: len(results) == 2, timeout), f"no concluyó: {results}")
        return results

    def assert_main_thread_only(self) -> None:
        self.assertTrue(self.dispatched_on)
        self.assertTrue(all(t is threading.main_thread() for t in self.dispatched_on))
        for connector in (self.codex, self.agy):
            for t in connector.threads:
                self.assertIsNot(t, threading.main_thread(), "el conector bloquearía GTK")


class ServiceRefreshTests(RefreshCase):
    def test_both_providers_respond(self) -> None:
        results = self.refresh_both()
        self.assertEqual({k: s.status for k, s in results.items()},
                         {AgentKind.CODEX: QuotaStatus.AVAILABLE, AgentKind.ANTIGRAVITY: QuotaStatus.AVAILABLE})
        self.assert_main_thread_only()
        self.assertFalse(self.service.is_refreshing(AgentKind.CODEX) or self.service.is_refreshing(AgentKind.ANTIGRAVITY))
        stored = self.service.get_all_snapshots()  # persistido en SQLite
        self.assertEqual(stored[AgentKind.CODEX].groups[0].windows[0].used_percentage, 25.0)

    def test_codex_ok_antigravity_fails(self) -> None:
        self.agy.behaviour = failing("agy caído")
        results = self.refresh_both()
        self.assertEqual(results[AgentKind.CODEX].status, QuotaStatus.AVAILABLE)
        self.assertEqual(results[AgentKind.ANTIGRAVITY].status, QuotaStatus.ERROR)
        self.assertIn("agy caído", results[AgentKind.ANTIGRAVITY].detail)

    def test_antigravity_ok_codex_fails(self) -> None:
        self.codex.behaviour = lambda: ProviderQuotaSnapshot(provider=AgentKind.CODEX, status=QuotaStatus.ERROR,
                                                             detail="RPC fallida",
                                                             updated_at=datetime.now(timezone.utc))
        results = self.refresh_both()
        self.assertEqual(results[AgentKind.ANTIGRAVITY].status, QuotaStatus.AVAILABLE)
        self.assertEqual(results[AgentKind.CODEX].status, QuotaStatus.ERROR)

    def test_one_provider_never_returns(self) -> None:
        self.service = self.make_service(deadline_seconds=0.4)
        self.agy.behaviour = lambda: (self.release.wait(30), self.agy.available())[1]
        started = time.monotonic()
        results = self.refresh_both(timeout=3)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(results[AgentKind.CODEX].status, QuotaStatus.AVAILABLE)
        self.assertEqual(results[AgentKind.ANTIGRAVITY].status, QuotaStatus.ERROR)
        self.assertIn("no respondió a tiempo", results[AgentKind.ANTIGRAVITY].detail)
        # El resultado tardío no reabre ni duplica la consulta ya concluida.
        self.release.set()
        self.pump(lambda: False, timeout=0.3)
        self.assertEqual(self.service.get_snapshot(AgentKind.ANTIGRAVITY).status, QuotaStatus.ERROR)

    def test_both_providers_never_return(self) -> None:
        self.service = self.make_service(deadline_seconds=0.3)
        for connector in (self.codex, self.agy):
            connector.behaviour = lambda c=connector: (self.release.wait(30), c.available())[1]
        done: list[int] = []
        self.service.refresh_all(force=True, callback=lambda: done.append(1))
        self.assertTrue(self.pump(lambda: bool(done), timeout=3), "refresh_all no terminó")
        self.assertEqual(done, [1])
        for kind in (AgentKind.CODEX, AgentKind.ANTIGRAVITY):
            self.assertEqual(self.service.get_snapshot(kind).status, QuotaStatus.ERROR)

    def test_exception_in_worker_thread_still_notifies(self) -> None:
        self.codex.behaviour = failing("explota en el hilo")
        self.agy.behaviour = lambda: None  # resultado no válido
        results = self.refresh_both()
        self.assertEqual(results[AgentKind.CODEX].status, QuotaStatus.ERROR)
        self.assertIn("explota en el hilo", results[AgentKind.CODEX].detail)
        self.assertEqual(results[AgentKind.ANTIGRAVITY].status, QuotaStatus.ERROR)
        self.assert_main_thread_only()

    def test_storage_failure_still_notifies(self) -> None:
        original = self.service._repo.save_snapshot
        self.service._repo.save_snapshot = lambda _s: (_ for _ in ()).throw(RuntimeError("disco lleno"))
        results = self.refresh_both()
        self.service._repo.save_snapshot = original
        self.assertTrue(all(s.status is QuotaStatus.ERROR for s in results.values()))
        self.assertIn("disco lleno", results[AgentKind.CODEX].detail)

    def test_retry_after_error(self) -> None:
        self.codex.behaviour = failing()
        self.assertEqual(self.refresh_both()[AgentKind.CODEX].status, QuotaStatus.ERROR)
        self.codex.behaviour = self.codex.available
        self.assertEqual(self.refresh_both()[AgentKind.CODEX].status, QuotaStatus.AVAILABLE)
        self.assertEqual(self.codex.calls, 2)

    def test_failure_keeps_previous_measurements_as_stale(self) -> None:
        self.refresh_both()
        before = self.service.get_snapshot(AgentKind.CODEX)
        self.codex.behaviour = failing("sin red")
        after = self.refresh_both()[AgentKind.CODEX]
        self.assertEqual(after.status, QuotaStatus.STALE)
        self.assertEqual(after.groups, before.groups)
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertIn("sin red", after.detail)
        self.assertEqual(self.service.get_snapshot(AgentKind.CODEX).status, QuotaStatus.STALE)

    def test_concurrent_requests_are_deduplicated(self) -> None:
        self.codex.behaviour = lambda: (self.release.wait(5), self.codex.available())[1]
        got: list[ProviderQuotaSnapshot] = []
        for _ in range(3):
            self.service.refresh_provider(AgentKind.CODEX, force=True, callback=got.append)
        self.assertTrue(self.service.is_refreshing(AgentKind.CODEX))
        self.release.set()
        self.assertTrue(self.pump(lambda: len(got) == 3))
        self.assertEqual(self.codex.calls, 1, "se lanzó más de una consulta simultánea")
        self.assertEqual({s.status for s in got}, {QuotaStatus.AVAILABLE})

    def test_cooldown_without_force_uses_cache(self) -> None:
        self.service = self.make_service(cooldown_seconds=60)
        self.refresh_both()
        got: list[ProviderQuotaSnapshot] = []
        self.service.refresh_provider(AgentKind.CODEX, callback=got.append)
        self.assertTrue(self.pump(lambda: bool(got)))
        self.assertEqual((self.codex.calls, got[0].status), (1, QuotaStatus.AVAILABLE))

    def test_quotas_are_global_not_per_project(self) -> None:
        columns = [r[1] for r in self.db.conn.execute("PRAGMA table_info(subscription_quotas)")]
        self.assertFalse([c for c in columns if "project" in c], columns)
        self.refresh_both()
        other = QuotaService(QuotaRepository(self.db), FakeConnector(AgentKind.CODEX), FakeConnector(AgentKind.ANTIGRAVITY))
        self.assertEqual(other.get_snapshot(AgentKind.CODEX).groups, self.service.get_snapshot(AgentKind.CODEX).groups)

    def test_claude_stays_passive(self) -> None:
        payload = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 12.0,
                                                            "resets_at": int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())}}})
        self.service._repo.save_snapshot(parse_claude_statusline_payload(payload))
        got: list[ProviderQuotaSnapshot] = []
        self.service.refresh_provider(AgentKind.CLAUDE_CODE, force=True, callback=got.append)
        self.refresh_both()
        self.assertTrue(self.pump(lambda: bool(got)))
        self.assertEqual(got[0].status, QuotaStatus.AVAILABLE)
        self.assertEqual(got[0].groups[0].windows[0].used_percentage, 12.0)
        self.assertEqual(self.service.get_snapshot(AgentKind.CLAUDE_CODE).groups[0].windows[0].used_percentage, 12.0,
                         "refrescar Codex y Antigravity alteró la lectura pasiva de Claude")


class ConnectorProcessTests(unittest.TestCase):
    """Conectores reales con ejecutables falsos encontrados por el PATH."""

    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        write_exe(self.bin, "codex", FAKE_CODEX)
        write_exe(self.bin, "agy", FAKE_AGY)
        self.which = lambda name: shutil.which(name, path=str(self.bin))
        self.saved = {k: os.environ.get(k) for k in ("FAKE_QUOTA_MODE", "FAKE_QUOTA_PIDFILE")}

    def tearDown(self) -> None:
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def mode(self, value: str) -> None:
        os.environ["FAKE_QUOTA_MODE"] = value

    def test_codex_protocol_sequence(self) -> None:
        self.mode("ok")
        snap = CodexQuotaConnector(which=self.which, timeout=10).fetch()
        self.assertEqual(snap.status, QuotaStatus.AVAILABLE, snap.detail)
        self.assertEqual([w.used_percentage for w in snap.groups[0].windows], [42.0, 10.0])

    def test_codex_rpc_error_and_crash(self) -> None:
        self.mode("error")
        snap = CodexQuotaConnector(which=self.which, timeout=10).fetch()
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("sin sesión", snap.detail)
        self.mode("crash")
        started = time.monotonic()
        snap = CodexQuotaConnector(which=self.which, timeout=10).fetch()
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertLess(time.monotonic() - started, 5)

    def test_codex_timeout_is_effective(self) -> None:
        self.mode("hang")
        started = time.monotonic()
        snap = CodexQuotaConnector(which=self.which, timeout=1.0).fetch()
        self.assertLess(time.monotonic() - started, 4, "el timeout de Codex no se respetó")
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("Tiempo de espera", snap.detail)

    def test_antigravity_ok_and_failure(self) -> None:
        self.mode("ok")
        snap = AntigravityQuotaConnector(which=self.which, timeout=10).fetch()
        self.assertNotEqual(snap.status, QuotaStatus.ERROR, snap.detail)
        self.mode("fail")
        snap = AntigravityQuotaConnector(which=self.which, timeout=10).fetch()
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("autenticación", snap.detail)

    def test_antigravity_timeout_kills_grandchildren(self) -> None:
        self.mode("grandchild_hang")
        pidfile = Path(self.tmp.name) / "nieto.pid"
        os.environ["FAKE_QUOTA_PIDFILE"] = str(pidfile)
        started = time.monotonic()
        snap = AntigravityQuotaConnector(which=self.which, timeout=1.0).fetch()
        self.assertLess(time.monotonic() - started, 4, "el timeout de Antigravity no se respetó")
        self.assertEqual(snap.status, QuotaStatus.ERROR)
        self.assertIn("Tiempo de espera", snap.detail)
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("el proceso nieto de agy sigue vivo")

    def test_missing_executables_report_errors(self) -> None:
        none = lambda _name: None  # noqa: E731
        self.assertEqual(CodexQuotaConnector(which=none).fetch().status, QuotaStatus.ERROR)
        self.assertEqual(AntigravityQuotaConnector(which=none).fetch().status, QuotaStatus.ERROR)

    def test_service_end_to_end_with_fake_clis(self) -> None:
        self.mode("ok")
        with temp_dir() as tmp:
            db = Database(Path(tmp) / "q.db")
            pending: queue.Queue = queue.Queue()
            service = QuotaService(QuotaRepository(db), CodexQuotaConnector(which=self.which, timeout=10),
                                   AntigravityQuotaConnector(which=self.which, timeout=10), dispatch=pending.put)
            done: list[int] = []
            service.refresh_all(force=True, callback=lambda: done.append(1))
            deadline = time.monotonic() + 15
            while not done and time.monotonic() < deadline:
                try:
                    pending.get(timeout=0.05)()
                except queue.Empty:
                    pass
            self.assertEqual(done, [1])
            self.assertEqual(service.get_snapshot(AgentKind.CODEX).status, QuotaStatus.AVAILABLE)
            self.assertNotEqual(service.get_snapshot(AgentKind.ANTIGRAVITY).status, QuotaStatus.ERROR)
            db.close()


if __name__ == "__main__":
    unittest.main()
