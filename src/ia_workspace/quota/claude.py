"""Conector e integración pasiva para Claude Code mediante el mecanismo 'statusLine'."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ia_workspace.domain.models import AgentKind
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)


def detect_claude_statusline_conflict(
    home_dir: Path | None = None,
    project_dir: Path | None = None,
) -> tuple[bool, str]:
    """Comprueba si el usuario o el proyecto ya tienen configurado un 'statusLine'.

    Preserva cualquier configuración preexistente para evitar sobrescribirla.
    Devuelve (hay_conflicto, motivo_explicativo).
    """
    candidates: list[Path] = []
    home = home_dir or Path.home()
    candidates.append(home / ".claude" / "settings.json")

    if project_dir is not None:
        candidates.append(project_dir / ".claude" / "settings.json")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict) and data.get("statusLine"):
                return (
                    True,
                    f"Configuración previa detectada en {path}: el usuario ya tiene definido 'statusLine'.",
                )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return (
                True,
                f"No se pudo analizar de forma segura {path}; se desactiva la inyección automática de statusLine.",
            )

    return False, ""


def get_claude_launch_settings(
    helper_path: Path | str,
    home_dir: Path | None = None,
    project_dir: Path | None = None,
) -> tuple[str, ...]:
    """Devuelve las opciones oficiales de CLI para configurar la recepción de statusLine.

    Si existe una configuración previa del usuario o proyecto, devuelve una tupla vacía
    para no sobrescribir sus preferencias.
    """
    has_conflict, _ = detect_claude_statusline_conflict(home_dir=home_dir, project_dir=project_dir)
    if has_conflict:
        return ()

    settings_payload = json.dumps(
        {
            "statusLine": {
                "type": "command",
                "command": str(helper_path),
            }
        }
    )
    return ("--settings", settings_payload)


def parse_claude_statusline_payload(raw_json: str) -> ProviderQuotaSnapshot:
    """Procesa el JSON emitido por Claude Code por stdin hacia la statusLine."""
    if not raw_json.strip():
        return ProviderQuotaSnapshot(
            provider=AgentKind.CLAUDE_CODE,
            status=QuotaStatus.NO_DATA,
            detail="Esperando primera respuesta del modelo en la sesión activa.",
            updated_at=datetime.now(timezone.utc),
        )

    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError) as exc:
        return ProviderQuotaSnapshot(
            provider=AgentKind.CLAUDE_CODE,
            status=QuotaStatus.ERROR,
            detail=f"Carga JSON de statusLine no válida: {exc}",
            updated_at=datetime.now(timezone.utc),
        )

    if not isinstance(data, dict):
        return ProviderQuotaSnapshot(
            provider=AgentKind.CLAUDE_CODE,
            status=QuotaStatus.ERROR,
            detail="El payload de statusLine no es un objeto JSON.",
            updated_at=datetime.now(timezone.utc),
        )

    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict) or not rate_limits:
        # La sesión aún no ha generado respuestas o la cuenta no usa límites de suscripción
        return ProviderQuotaSnapshot(
            provider=AgentKind.CLAUDE_CODE,
            status=QuotaStatus.NO_DATA,
            detail="Esperando primera respuesta del modelo en la sesión activa.",
            updated_at=datetime.now(timezone.utc),
        )

    windows: list[QuotaWindow] = []

    # Ventana corta de 5 horas
    five_hour = rate_limits.get("five_hour")
    if isinstance(five_hour, dict):
        used_pct = five_hour.get("used_percentage")
        resets_at_raw = five_hour.get("resets_at")
        resets_at = None
        if isinstance(resets_at_raw, (int, float)):
            try:
                resets_at = datetime.fromtimestamp(resets_at_raw, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                resets_at = None

        windows.append(
            QuotaWindow(
                window_id="five_hour",
                name="Límite 5 horas",
                metric_type=QuotaMetricType.USED_PERCENT,
                percentage=float(used_pct) if used_pct is not None else None,
                window_minutes=300,
                resets_at=resets_at,
            )
        )

    # Ventana semanal de 7 días
    seven_day = rate_limits.get("seven_day")
    if isinstance(seven_day, dict):
        used_pct = seven_day.get("used_percentage")
        resets_at_raw = seven_day.get("resets_at")
        resets_at = None
        if isinstance(resets_at_raw, (int, float)):
            try:
                resets_at = datetime.fromtimestamp(resets_at_raw, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                resets_at = None

        windows.append(
            QuotaWindow(
                window_id="seven_day",
                name="Límite semanal",
                metric_type=QuotaMetricType.USED_PERCENT,
                percentage=float(used_pct) if used_pct is not None else None,
                window_minutes=10080,
                resets_at=resets_at,
            )
        )

    group = QuotaGroup(
        group_id="claude-code",
        name="Claude Code",
        description="Límites asignados a Claude Code en la suscripción Claude Pro/Team",
        windows=tuple(windows),
    )

    status = QuotaStatus.AVAILABLE if windows else QuotaStatus.NO_DATA
    plan_type = "Claude Pro/Team" if windows else ""

    return ProviderQuotaSnapshot(
        provider=AgentKind.CLAUDE_CODE,
        plan_type=plan_type,
        status=status,
        groups=(group,),
        updated_at=datetime.now(timezone.utc),
        detail="",
    )
