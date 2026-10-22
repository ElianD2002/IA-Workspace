"""Conector oficial para consultar cuotas de Google Antigravity mediante 'agy -p /usage'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import re
import shutil
import subprocess

from ia_workspace.domain.models import AgentKind
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)
from ia_workspace.quota.process import run_bounded

Which = Callable[[str], str | None]
Runner = Callable[[list[str], float], tuple[int, str, str]]


def _default_cmd_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Ejecuta un comando y captura stdout y stderr con un plazo que se cumple siempre
    (finaliza el grupo de procesos entero, también los nietos que dejen tuberías abiertas)."""
    return run_bounded(argv, timeout)


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", cleaned)


class AntigravityQuotaConnector:
    """Conector para Google Antigravity mediante el comando no interactivo oficial."""

    def __init__(
        self,
        which: Which = shutil.which,
        runner: Runner = _default_cmd_runner,
        timeout: float = 8.0,
    ) -> None:
        self._which = which
        self._runner = runner
        self._timeout = timeout

    def fetch(self) -> ProviderQuotaSnapshot:
        """Consulta administrativa de cuotas de Antigravity sin consumir inferencia."""
        executable = self._which("agy")
        if not executable:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail="Google Antigravity CLI (agy) no está instalada en el PATH.",
                updated_at=datetime.now(timezone.utc),
            )

        cmd = [executable, "-p", "/usage", "--output-format", "json"]

        try:
            returncode, stdout, stderr = self._runner(cmd, self._timeout)
        except subprocess.TimeoutExpired:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail="Tiempo de espera agotado al consultar límites de Google Antigravity.",
                updated_at=datetime.now(timezone.utc),
            )
        except (OSError, ValueError) as exc:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail=f"Fallo al ejecutar 'agy': {exc}",
                updated_at=datetime.now(timezone.utc),
            )

        if returncode != 0:
            err_snip = stderr.strip()[:200]
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail=f"agy salió con código {returncode}: {err_snip}",
                updated_at=datetime.now(timezone.utc),
            )

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail=f"Respuesta JSON no válida de agy: {exc}",
                updated_at=datetime.now(timezone.utc),
            )

        if not isinstance(data, dict):
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail="La respuesta emitida por agy no es un objeto JSON.",
                updated_at=datetime.now(timezone.utc),
            )

        # Política preventiva y de fallo seguro: verificar estado y ausencia total de inferencia
        top_status = str(data.get("status") or "").upper()
        if top_status and top_status not in ("SUCCESS", "OK", "COMPLETED"):
            err_msg = str(data.get("error") or data.get("detail") or f"estado '{top_status}'")
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail=f"Fallo seguro: agy devolvió estado no exitoso ({err_msg}).",
                updated_at=datetime.now(timezone.utc),
            )

        num_turns = data.get("num_turns", 0)
        usage_info = data.get("usage", {})
        total_tokens = usage_info.get("total_tokens", 0) if isinstance(usage_info, dict) else 0

        if num_turns > 0 or total_tokens > 0:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.ERROR,
                detail=f"Fallo seguro: se detectó consumo de inferencia ({total_tokens} tokens, {num_turns} turnos); lectura abortada.",
                updated_at=datetime.now(timezone.utc),
            )

        # Extraer grupos soportando estructuras anidadas (ej. command.data, response.command.data, response.data)
        raw_groups = self._extract_groups(data)
        if not raw_groups:
            return ProviderQuotaSnapshot(
                provider=AgentKind.ANTIGRAVITY,
                status=QuotaStatus.NO_DATA,
                detail="No se encontraron grupos de cuota en la salida de agy.",
                updated_at=datetime.now(timezone.utc),
            )

        groups: list[QuotaGroup] = []
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                continue
            group_name = str(raw_group.get("name") or "Antigravity")
            group_id = _slugify(group_name)
            description = str(raw_group.get("description") or "")
            raw_buckets = raw_group.get("buckets", [])
            windows: list[QuotaWindow] = []

            if isinstance(raw_buckets, list):
                for bucket in raw_buckets:
                    if not isinstance(bucket, dict):
                        continue
                    bucket_id = str(bucket.get("id") or "")
                    bucket_name = str(bucket.get("name") or bucket_id or "Límite")
                    window_str = bucket.get("window")
                    window_minutes = None
                    if window_str == "5h":
                        window_minutes = 300
                    elif window_str == "weekly":
                        window_minutes = 10080
                    elif isinstance(window_str, int):
                        window_minutes = window_str

                    rem_fraction = bucket.get("remaining_fraction")
                    percentage = (
                        float(rem_fraction) * 100.0
                        if rem_fraction is not None and isinstance(rem_fraction, (int, float))
                        else None
                    )

                    reset_time_str = bucket.get("reset_time")
                    resets_at = None
                    if reset_time_str and isinstance(reset_time_str, str):
                        try:
                            resets_at = datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            resets_at = None

                    windows.append(
                        QuotaWindow(
                            window_id=bucket_id,
                            name=bucket_name,
                            metric_type=QuotaMetricType.REMAINING_PERCENT,
                            percentage=percentage,
                            window_minutes=window_minutes,
                            resets_at=resets_at,
                        )
                    )

            groups.append(
                QuotaGroup(
                    group_id=group_id,
                    name=group_name,
                    description=description,
                    windows=tuple(windows),
                )
            )

        status = QuotaStatus.AVAILABLE if any(g.windows for g in groups) else QuotaStatus.NO_DATA
        return ProviderQuotaSnapshot(
            provider=AgentKind.ANTIGRAVITY,
            plan_type="Google AI Pro",
            status=status,
            groups=tuple(groups),
            updated_at=datetime.now(timezone.utc),
            detail="",
        )

    def _extract_groups(self, data: dict) -> list[dict]:
        """Extrae la lista de grupos soportando posibles variantes de la estructura JSON."""
        # Candidato 1: data["command"]["data"]["groups"]
        cmd = data.get("command")
        if isinstance(cmd, dict):
            c_data = cmd.get("data")
            if isinstance(c_data, dict) and isinstance(c_data.get("groups"), list):
                return c_data["groups"]

        # Candidato 2: data["response"] (puede contener command.data.groups o data.groups o groups)
        resp = data.get("response")
        if isinstance(resp, dict):
            if isinstance(resp.get("groups"), list):
                return resp["groups"]
            r_data = resp.get("data")
            if isinstance(r_data, dict) and isinstance(r_data.get("groups"), list):
                return r_data["groups"]
            r_cmd = resp.get("command")
            if isinstance(r_cmd, dict):
                rc_data = r_cmd.get("data")
                if isinstance(rc_data, dict) and isinstance(rc_data.get("groups"), list):
                    return rc_data["groups"]

        # Candidato 3: data["data"]["groups"]
        d = data.get("data")
        if isinstance(d, dict) and isinstance(d.get("groups"), list):
            return d["groups"]

        # Candidato 4: data["groups"]
        if isinstance(data.get("groups"), list):
            return data["groups"]

        return []
