"""Repositorio SQLite para la persistencia de cuotas y límites de suscripción."""

from __future__ import annotations

from datetime import datetime, timezone

from ia_workspace.domain.models import AgentKind
from ia_workspace.persistence.database import Database
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)


class QuotaRepository:
    """Caché SQLite global de las mediciones de cuotas de suscripciones.

    Almacena exclusivamente metadatos normalizados de cuotas; nunca contiene
    tokens, cookies, credenciales ni contenidos de mensajes.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def save_snapshot(self, snapshot: ProviderQuotaSnapshot) -> None:
        """Guarda o actualiza la fotografía completa de cuotas de un proveedor."""
        provider_id = snapshot.provider.value
        updated_at = (snapshot.updated_at or datetime.now(timezone.utc)).isoformat()
        status = snapshot.status.value
        plan_type = snapshot.plan_type
        detail = snapshot.detail

        with self._db.conn:
            self._db.conn.execute(
                "DELETE FROM subscription_quotas WHERE provider_id = ?",
                (provider_id,),
            )
            # Si no hay grupos o ventanas (p. ej. error o sin datos)
            if not snapshot.groups or all(not g.windows for g in snapshot.groups):
                self._db.conn.execute(
                    """
                    INSERT INTO subscription_quotas (
                        provider_id, plan_type, group_id, group_name,
                        window_id, window_name, window_minutes,
                        metric_type, percentage, resets_at,
                        updated_at, status, detail
                    ) VALUES (?, ?, '', '', '', '', NULL, 'used_percent', NULL, NULL, ?, ?, ?)
                    """,
                    (provider_id, plan_type, updated_at, status, detail),
                )
                return

            for group in snapshot.groups:
                for window in group.windows:
                    resets_at_str = window.resets_at.isoformat() if window.resets_at else None
                    self._db.conn.execute(
                        """
                        INSERT INTO subscription_quotas (
                            provider_id, plan_type, group_id, group_name,
                            window_id, window_name, window_minutes,
                            metric_type, percentage, resets_at,
                            updated_at, status, detail
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider_id,
                            plan_type,
                            group.group_id,
                            group.name,
                            window.window_id,
                            window.name,
                            window.window_minutes,
                            window.metric_type.value,
                            window.percentage,
                            resets_at_str,
                            updated_at,
                            status,
                            detail,
                        ),
                    )

    def get_snapshot(self, provider: AgentKind) -> ProviderQuotaSnapshot | None:
        """Recupera la fotografía almacenada para un proveedor o None si no existe."""
        rows = self._db.conn.execute(
            """
            SELECT plan_type, group_id, group_name, window_id, window_name,
                   window_minutes, metric_type, percentage, resets_at,
                   updated_at, status, detail
            FROM subscription_quotas
            WHERE provider_id = ?
            ORDER BY id ASC
            """,
            (provider.value,),
        ).fetchall()

        if not rows:
            return None

        first = rows[0]
        plan_type = first["plan_type"]
        status = QuotaStatus(first["status"])
        detail = first["detail"]
        updated_at = datetime.fromisoformat(first["updated_at"])

        groups_map: dict[str, tuple[str, list[QuotaWindow]]] = {}
        for row in rows:
            group_id = row["group_id"]
            if not group_id or not row["window_id"]:
                continue
            if group_id not in groups_map:
                groups_map[group_id] = (row["group_name"], [])

            resets_at = (
                datetime.fromisoformat(row["resets_at"])
                if row["resets_at"]
                else None
            )
            window = QuotaWindow(
                window_id=row["window_id"],
                name=row["window_name"],
                metric_type=QuotaMetricType(row["metric_type"]),
                percentage=row["percentage"],
                window_minutes=row["window_minutes"],
                resets_at=resets_at,
            )
            groups_map[group_id][1].append(window)

        groups = tuple(
            QuotaGroup(
                group_id=gid,
                name=gname,
                windows=tuple(windows),
            )
            for gid, (gname, windows) in groups_map.items()
        )

        return ProviderQuotaSnapshot(
            provider=provider,
            plan_type=plan_type,
            status=status,
            groups=groups,
            updated_at=updated_at,
            detail=detail,
        )

    def get_all_snapshots(self) -> dict[AgentKind, ProviderQuotaSnapshot]:
        """Recupera las fotografías disponibles de todos los agentes."""
        result: dict[AgentKind, ProviderQuotaSnapshot] = {}
        for kind in (AgentKind.CLAUDE_CODE, AgentKind.CODEX, AgentKind.ANTIGRAVITY):
            snapshot = self.get_snapshot(kind)
            if snapshot is not None:
                result[kind] = snapshot
            else:
                result[kind] = ProviderQuotaSnapshot(provider=kind, status=QuotaStatus.NO_DATA)
        return result
