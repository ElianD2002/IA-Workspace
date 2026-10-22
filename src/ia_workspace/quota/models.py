"""Modelos unificados de límites y cuotas de uso de suscripciones de IA.

Representa de forma tipada, normalizada y segura el consumo de los proveedores
oficiales (OpenAI Codex, Google Antigravity y Claude Code) utilizando sus
suscripciones existentes sin APIs de pago.

Regla de seguridad: nunca almacena tokens, cookies, credenciales ni información privada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from ia_workspace.domain.models import AgentKind


class QuotaStatus(StrEnum):
    """Estado de disponibilidad de la medición de cuota."""

    AVAILABLE = "available"  # Datos válidos recibidos del proveedor
    PENDING = "pending"  # Solicitud o lectura en curso
    NO_DATA = "no_data"  # Sin datos aún (p. ej. sin respuesta en la sesión activa)
    STALE = "stale"  # Datos obsoletos que superan el umbral de frescura
    ERROR = "error"  # Error al consultar, timeout o respuesta no válida


class QuotaMetricType(StrEnum):
    """Naturaleza de la métrica comunicada por el proveedor."""

    USED_PERCENT = "used_percent"  # Porcentaje consumido (0.0 a 100.0)
    REMAINING_PERCENT = "remaining_percent"  # Porcentaje restante (0.0 a 100.0)


@dataclass(frozen=True)
class QuotaWindow:
    """Ventana específica de cuota (ej. 5 horas, 7 días)."""

    window_id: str
    name: str
    metric_type: QuotaMetricType
    percentage: float | None = None
    window_minutes: int | None = None
    resets_at: datetime | None = None

    @property
    def used_percentage(self) -> float | None:
        """Porcentaje consumido normalizado (0.0 a 100.0) o None si el valor está ausente."""
        if self.percentage is None:
            return None
        val = float(self.percentage)
        if self.metric_type == QuotaMetricType.USED_PERCENT:
            return max(0.0, min(100.0, val))
        return max(0.0, min(100.0, 100.0 - val))

    @property
    def remaining_percentage(self) -> float | None:
        """Porcentaje restante normalizado (0.0 a 100.0) o None si el valor está ausente."""
        if self.percentage is None:
            return None
        val = float(self.percentage)
        if self.metric_type == QuotaMetricType.REMAINING_PERCENT:
            return max(0.0, min(100.0, val))
        return max(0.0, min(100.0, 100.0 - val))

    def seconds_until_reset(self, now: datetime | None = None) -> float | None:
        """Segundos restantes hasta la fecha de reinicio en UTC, o None si no hay fecha."""
        if self.resets_at is None:
            return None
        current = now or datetime.now(timezone.utc)
        resets = self.resets_at if self.resets_at.tzinfo else self.resets_at.replace(tzinfo=timezone.utc)
        return max(0.0, (resets - current).total_seconds())


@dataclass(frozen=True)
class QuotaGroup:
    """Familia de modelos o categoría de cuota (ej. 'Gemini Models', 'Gemini 4 Argon', 'Codex')."""

    group_id: str
    name: str
    description: str = ""
    windows: tuple[QuotaWindow, ...] = ()


@dataclass(frozen=True)
class ProviderQuotaSnapshot:
    """Fotografía normalizada y global de las cuotas de un proveedor."""

    provider: AgentKind
    plan_type: str = ""
    status: QuotaStatus = QuotaStatus.NO_DATA
    groups: tuple[QuotaGroup, ...] = ()
    updated_at: datetime | None = None
    detail: str = ""

    def is_stale(self, max_age_seconds: float = 1800.0, now: datetime | None = None) -> bool:
        """Comprueba si la medición supera el umbral de antigüedad especificado."""
        if self.updated_at is None or self.status != QuotaStatus.AVAILABLE:
            return False
        current = now or datetime.now(timezone.utc)
        updated = self.updated_at if self.updated_at.tzinfo else self.updated_at.replace(tzinfo=timezone.utc)
        return (current - updated).total_seconds() > max_age_seconds
