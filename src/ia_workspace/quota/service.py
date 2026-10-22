"""Servicio unificado de consulta y normalización de límites y cuotas de uso."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import time
import traceback

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


@dataclass
class _Inflight:
    """Consulta en curso de un proveedor y quiénes esperan su resultado."""

    waiters: list[Callable[[ProviderQuotaSnapshot], None]] = field(default_factory=list)
    timer: threading.Timer | None = None


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
        deadline_seconds: float = 20.0,
    ) -> None:
        self._repo = repo
        self._codex = codex_connector or CodexQuotaConnector()
        self._antigravity = antigravity_connector or AntigravityQuotaConnector()
        self._dispatch = dispatch
        self._spawn = spawn
        self._cooldown_seconds = cooldown_seconds
        self._stale_threshold_seconds = stale_threshold_seconds
        self._lock = threading.Lock()
        # Plazo de seguridad por consulta: mayor que el timeout propio de cada conector.
        self._deadline_seconds = deadline_seconds
        self._last_refresh_attempt: dict[AgentKind, float] = {}
        self._inflight: dict[AgentKind, _Inflight] = {}

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

    # ---- consultas ------------------------------------------------------------------------
    #
    # Hilos: el conector (subproceso + normalización) es lo único que corre en segundo plano.
    # La conexión SQLite pertenece al hilo principal, así que guardar el resultado, leer la
    # caché y avisar a los observadores ocurre siempre en el hilo principal, mediante
    # ``dispatch`` (``GLib.idle_add`` en la aplicación). Cada consulta termina en un estado
    # definido (AVAILABLE, NO_DATA, ERROR o STALE con la caché anterior) aunque el conector
    # lance una excepción o no vuelva nunca: un plazo de seguridad la da por fallida.

    def _connector(self, provider: AgentKind):
        if provider is AgentKind.CODEX:
            return self._codex
        if provider is AgentKind.ANTIGRAVITY:
            return self._antigravity
        raise ValueError(f"{provider.label} no admite consultas activas de cuota.")

    @staticmethod
    def _error(provider: AgentKind, detail: str) -> ProviderQuotaSnapshot:
        return ProviderQuotaSnapshot(
            provider=provider,
            status=QuotaStatus.ERROR,
            detail=detail,
            updated_at=datetime.now(timezone.utc),
        )

    def _fetch(self, provider: AgentKind) -> ProviderQuotaSnapshot:
        """Ejecuta el conector. Nunca lanza: cualquier fallo se convierte en ERROR."""
        try:
            snapshot = self._connector(provider).fetch()
        except Exception as exc:  # noqa: BLE001 — el resultado siempre debe llegar
            return self._error(provider, f"Fallo inesperado al consultar cuotas: {exc}")
        if not isinstance(snapshot, ProviderQuotaSnapshot):
            return self._error(provider, "El conector de cuotas devolvió un resultado no válido.")
        return snapshot

    def _store(self, result: ProviderQuotaSnapshot) -> ProviderQuotaSnapshot:
        """Guarda el resultado (hilo principal). Si la consulta falló y había mediciones,
        se conservan marcadas como desactualizadas en lugar de borrarlas."""
        if result.status is QuotaStatus.ERROR:
            previous = self._repo.get_snapshot(result.provider)
            if previous is not None and any(g.windows for g in previous.groups):
                result = ProviderQuotaSnapshot(
                    provider=previous.provider,
                    plan_type=previous.plan_type,
                    status=QuotaStatus.STALE,
                    groups=previous.groups,
                    updated_at=previous.updated_at,
                    detail=f"No se pudo actualizar ({result.detail}). Se muestran los últimos datos conocidos.",
                )
        self._repo.save_snapshot(result)
        return self.get_snapshot(result.provider)

    def _claim(self, provider: AgentKind, force: bool) -> bool:
        """Registra el intento si no está en enfriamiento. ``False`` si debe usarse la caché."""
        now = time.monotonic()
        with self._lock:
            last = self._last_refresh_attempt.get(provider)
            if not force and last is not None and (now - last) < self._cooldown_seconds:
                return False
            self._last_refresh_attempt[provider] = now
            return True

    def refresh_codex_sync(self, force: bool = False) -> ProviderQuotaSnapshot:
        """Consulta sincrónica de cuotas para Codex con control de cooldown (hilo principal)."""
        if not self._claim(AgentKind.CODEX, force):
            return self.get_snapshot(AgentKind.CODEX)
        return self._store(self._fetch(AgentKind.CODEX))

    def refresh_antigravity_sync(self, force: bool = False) -> ProviderQuotaSnapshot:
        """Consulta sincrónica de cuotas para Google Antigravity con control de cooldown (hilo principal)."""
        if not self._claim(AgentKind.ANTIGRAVITY, force):
            return self.get_snapshot(AgentKind.ANTIGRAVITY)
        return self._store(self._fetch(AgentKind.ANTIGRAVITY))

    def is_refreshing(self, provider: AgentKind) -> bool:
        with self._lock:
            return provider in self._inflight

    def refresh_provider(
        self,
        provider: AgentKind,
        force: bool = False,
        callback: Callable[[ProviderQuotaSnapshot], None] | None = None,
    ) -> None:
        """Refresca un proveedor en segundo plano. Llamar desde el hilo principal.

        ``callback`` se ejecuta siempre exactamente una vez, en el hilo principal, con el
        estado final. Si ya hay una consulta en curso para ese proveedor, la petición se une
        a ella (deduplicación) y recibe el mismo resultado.
        """
        if provider is AgentKind.CLAUDE_CODE:
            # Claude Code es puramente pasivo (vía statusLine); no se le envían prompts
            snap = self.get_snapshot(AgentKind.CLAUDE_CODE)
            if callback:
                self._dispatch(lambda: callback(snap))
            return

        with self._lock:
            inflight = self._inflight.get(provider)
            if inflight is not None:
                if callback:
                    inflight.waiters.append(callback)
                return
        if not self._claim(provider, force):
            snap = self.get_snapshot(provider)
            if callback:
                self._dispatch(lambda: callback(snap))
            return

        request = _Inflight(waiters=[callback] if callback else [])
        with self._lock:
            self._inflight[provider] = request

        def finish(result: ProviderQuotaSnapshot) -> None:
            self._dispatch(lambda: self._complete(provider, request, result))

        def worker() -> None:
            finish(self._fetch(provider))

        timer = threading.Timer(
            self._deadline_seconds,
            lambda: finish(self._error(provider, "La consulta de cuotas no respondió a tiempo.")),
        )
        timer.daemon = True
        request.timer = timer
        timer.start()
        try:
            self._spawn(worker)
        except Exception as exc:  # noqa: BLE001 — p. ej. no se pudo crear el hilo
            finish(self._error(provider, f"No se pudo iniciar la consulta de cuotas: {exc}"))

    def _complete(self, provider: AgentKind, request: _Inflight, result: ProviderQuotaSnapshot) -> None:
        """Cierra una consulta en el hilo principal: guarda, libera y avisa una sola vez."""
        with self._lock:
            if self._inflight.get(provider) is not request:
                return  # ya concluyó (por el plazo de seguridad o por el resultado tardío)
            del self._inflight[provider]
            waiters = request.waiters
        if request.timer is not None:
            request.timer.cancel()
        try:
            snapshot = self._store(result)
        except Exception as exc:  # noqa: BLE001 — la notificación no se puede perder
            snapshot = self._error(provider, f"No se pudo guardar la medición: {exc}")
        for waiter in waiters:
            try:
                waiter(snapshot)
            except Exception:  # noqa: BLE001 — un observador no impide avisar a los demás
                traceback.print_exc()

    def refresh_all(
        self,
        force: bool = False,
        callback: Callable[[], None] | None = None,
    ) -> None:
        """Refresca todos los proveedores consultables de forma asíncrona.

        ``callback`` se ejecuta en el hilo principal cuando concluyen todas las consultas."""
        providers = (AgentKind.CODEX, AgentKind.ANTIGRAVITY)
        pending = set(providers)

        def on_done(provider: AgentKind) -> None:
            pending.discard(provider)
            if not pending and callback:
                callback()

        for p in providers:
            self.refresh_provider(p, force=force, callback=lambda _snap, p=p: on_done(p))
