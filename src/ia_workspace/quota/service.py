"""Servicio unificado de consulta y normalización de límites y cuotas de uso."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import threading
import time

from ia_workspace.domain.models import AgentKind
from ia_workspace.quota.antigravity import AntigravityQuotaConnector
from ia_workspace.quota.codex import CodexQuotaConnector
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaStatus,
)
from ia_workspace.quota.repository import QuotaRepository

Callback = Callable[[], None]
Dispatch = Callable[[Callback], None]
Spawn = Callable[[Callback], None]


def _thread_spawn(fn: Callback) -> None:
    threading.Thread(target=fn, daemon=True, name="iaw-quota-refresh").start()


def _direct_dispatch(fn: Callback) -> None:
    fn()


class QuotaService:
    """Servicio central para la consulta, normalización y persistencia de cuotas de IA.

    Permite consultar en segundo plano los límites reales de OpenAI Codex y
    Google Antigravity, y almacenar pasivamente las lecturas de Claude Code.
    No consume inferencias ni APIs de pago.
    """

    def __init__(
        self,
        repo: QuotaRepository,
        codex_connector: CodexQuotaConnector | None = None,
        antigravity_connector: AntigravityQuotaConnector | None = None,
        dispatch: Dispatch = _direct_dispatch,
        spawn: Spawn = _thread_spawn,
        cooldown_seconds: float = 30.0,
        stale_threshold_seconds: float = 1800.0,
    ) -> None:
        self._repo = repo
        self._codex = codex_connector or CodexQuotaConnector()
        self._antigravity = antigravity_connector or AntigravityQuotaConnector()
        self._dispatch = dispatch
        self._spawn = spawn
        self._cooldown_seconds = cooldown_seconds
        self._stale_threshold_seconds = stale_threshold_seconds
        self._lock = threading.Lock()
        self._last_refresh_attempt: dict[AgentKind, float] = {}
        self._refreshing: set[AgentKind] = set()

    def get_snapshot(self, provider: AgentKind) -> ProviderQuotaSnapshot:
        """Obtiene la última fotografía conocida de un proveedor."""
        snapshot = self._repo.get_snapshot(provider)
        if snapshot is None:
            return ProviderQuotaSnapshot(provider=provider, status=QuotaStatus.NO_DATA)

        # Si el snapshot está disponible pero es muy antiguo, marcarlo como STALE
        if snapshot.is_stale(max_age_seconds=self._stale_threshold_seconds):
            return ProviderQuotaSnapshot(
                provider=snapshot.provider,
                plan_type=snapshot.plan_type,
                status=QuotaStatus.STALE,
                groups=snapshot.groups,
                updated_at=snapshot.updated_at,
                detail=snapshot.detail or "Los datos de cuota superan el umbral de frescura.",
            )
        return snapshot

    def get_all_snapshots(self) -> dict[AgentKind, ProviderQuotaSnapshot]:
        """Obtiene las fotografías de todos los proveedores soportados."""
        return {
            kind: self.get_snapshot(kind)
            for kind in (AgentKind.CLAUDE_CODE, AgentKind.CODEX, AgentKind.ANTIGRAVITY)
        }

    def refresh_codex_sync(self, force: bool = False) -> ProviderQuotaSnapshot:
        """Consulta sincrónica de cuotas para Codex con control de cooldown."""
        now = time.monotonic()
        with self._lock:
            last = self._last_refresh_attempt.get(AgentKind.CODEX, 0.0)
            if not force and (now - last) < self._cooldown_seconds:
                return self.get_snapshot(AgentKind.CODEX)
            self._last_refresh_attempt[AgentKind.CODEX] = now

        snapshot = self._codex.fetch()
        self._repo.save_snapshot(snapshot)
        return snapshot

    def refresh_antigravity_sync(self, force: bool = False) -> ProviderQuotaSnapshot:
        """Consulta sincrónica de cuotas para Google Antigravity con control de cooldown."""
        now = time.monotonic()
        with self._lock:
            last = self._last_refresh_attempt.get(AgentKind.ANTIGRAVITY, 0.0)
            if not force and (now - last) < self._cooldown_seconds:
                return self.get_snapshot(AgentKind.ANTIGRAVITY)
            self._last_refresh_attempt[AgentKind.ANTIGRAVITY] = now

        snapshot = self._antigravity.fetch()
        self._repo.save_snapshot(snapshot)
        return snapshot

    def refresh_provider(
        self,
        provider: AgentKind,
        force: bool = False,
        callback: Callable[[ProviderQuotaSnapshot], None] | None = None,
    ) -> None:
        """Refresca un proveedor en segundo plano y notifica el resultado mediante dispatch."""
        if provider is AgentKind.CLAUDE_CODE:
            # Claude Code es puramente pasivo (vía statusLine); no se le envían prompts
            snap = self.get_snapshot(AgentKind.CLAUDE_CODE)
            if callback:
                self._dispatch(lambda: callback(snap))
            return

        with self._lock:
            if provider in self._refreshing:
                snap = self.get_snapshot(provider)
                if callback:
                    self._dispatch(lambda: callback(snap))
                return
            self._refreshing.add(provider)

        def _worker() -> None:
            try:
                if provider is AgentKind.CODEX:
                    snap = self.refresh_codex_sync(force=force)
                elif provider is AgentKind.ANTIGRAVITY:
                    snap = self.refresh_antigravity_sync(force=force)
                else:
                    snap = self.get_snapshot(provider)
            except Exception as exc:
                snap = ProviderQuotaSnapshot(
                    provider=provider,
                    status=QuotaStatus.ERROR,
                    detail=f"Fallo inesperado al consultar cuotas: {exc}",
                    updated_at=datetime.now(timezone.utc),
                )
                self._repo.save_snapshot(snap)
            finally:
                with self._lock:
                    self._refreshing.discard(provider)

            if callback:
                self._dispatch(lambda: callback(snap))

        self._spawn(_worker)

    def refresh_all(
        self,
        force: bool = False,
        callback: Callable[[], None] | None = None,
    ) -> None:
        """Refresca todos los proveedores consultables de forma asíncrona."""
        providers = (AgentKind.CODEX, AgentKind.ANTIGRAVITY)
        remaining = len(providers)
        lock = threading.Lock()

        def on_done(_: ProviderQuotaSnapshot) -> None:
            nonlocal remaining
            with lock:
                remaining -= 1
                if remaining <= 0 and callback:
                    self._dispatch(callback)

        for p in providers:
            self.refresh_provider(p, force=force, callback=on_done)
