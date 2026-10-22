"""Conector oficial para consultar cuotas de OpenAI Codex mediante 'codex app-server'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
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
from ia_workspace.quota.process import BoundedProcess

Which = Callable[[str], str | None]
Runner = Callable[[list[str], list[str], float], tuple[list[str], str]]

MAX_LINE_BYTES = 512 * 1024  # 512 KB límite de seguridad por línea


def _default_stdio_rpc_runner(
    argv: list[str],
    requests: list[str],
    timeout: float,
) -> tuple[list[str], str]:
    """Envía mensajes JSON-RPC por stdio en orden y recoge las respuestas, dentro del plazo.

    Cada petición con ``id`` espera su respuesta antes de enviar el siguiente mensaje (el
    protocolo exige ``initialize`` → ``initialized`` → peticiones); las notificaciones (sin
    ``id``) se envían sin esperar. Termina al recibir la respuesta de la última petición. El
    plazo se cumple aunque el servidor no responda (``quota.process``); al vencer, se finaliza
    el grupo de procesos y se lanza ``TimeoutExpired``.
    """
    proc = BoundedProcess(argv, timeout)
    responses: list[str] = []
    answered: set[object] = set()
    consumed = 0

    def collect() -> None:
        nonlocal consumed
        lines = proc.stdout[consumed:].split(b"\n")
        for raw in lines[:-1]:  # la última puede estar incompleta
            consumed += len(raw) + 1
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or len(line) > MAX_LINE_BYTES:
                continue
            responses.append(line)
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and "id" in obj and ("result" in obj or "error" in obj):
                answered.add(obj["id"])

    def answered_to(request_id: object) -> Callable[[], bool]:
        def check() -> bool:
            collect()
            return request_id in answered

        return check

    try:
        for req in requests:
            proc.send(req + "\n")
            try:
                request_id = json.loads(req).get("id")
            except (json.JSONDecodeError, ValueError, AttributeError):
                request_id = None
            if request_id is not None:
                proc.pump(answered_to(request_id))
                if request_id not in answered:
                    break  # el servidor cerró sin responder: no tiene sentido seguir
        collect()
    finally:
        proc.finish()
    return responses, proc.stderr.decode("utf-8", errors="replace")


class CodexQuotaConnector:
    """Conector para OpenAI Codex utilizando el protocolo oficial JSON-RPC sobre stdio."""

    def __init__(
        self,
        which: Which = shutil.which,
        runner: Runner = _default_stdio_rpc_runner,
        timeout: float = 6.0,
    ) -> None:
        self._which = which
        self._runner = runner
        self._timeout = timeout

    def fetch(self) -> ProviderQuotaSnapshot:
        """Consulta los límites de cuota vigentes de Codex sin consumir inferencia."""
        executable = self._which("codex")
        if not executable:
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail="Codex CLI (codex) no está instalada en el PATH.",
                updated_at=datetime.now(timezone.utc),
            )

        req1 = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "ia-workspace", "version": "1.0.0"}},
            }
        )
        # Notificación obligatoria del protocolo tras la respuesta a «initialize».
        initialized = json.dumps({"jsonrpc": "2.0", "method": "initialized"})
        req2 = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            }
        )

        try:
            responses, stderr_out = self._runner(
                [executable, "app-server"], [req1, initialized, req2], self._timeout
            )
        except subprocess.TimeoutExpired:
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail="Tiempo de espera agotado al consultar límites de Codex vía JSON-RPC.",
                updated_at=datetime.now(timezone.utc),
            )
        except (OSError, ValueError) as exc:
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail=f"Fallo al ejecutar 'codex app-server': {exc}",
                updated_at=datetime.now(timezone.utc),
            )

        # Buscar el mensaje con id == 2
        rate_limit_resp = None
        for raw in responses:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and obj.get("id") == 2:
                    rate_limit_resp = obj
                    break
            except (json.JSONDecodeError, ValueError):
                continue

        if not rate_limit_resp:
            detail_msg = f"Codex app-server no devolvió respuesta para rateLimits. {stderr_out.strip()[:200]}".strip()
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail=detail_msg or "Respuesta incompleta de Codex app-server.",
                updated_at=datetime.now(timezone.utc),
            )

        if "error" in rate_limit_resp:
            err = rate_limit_resp["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail=f"Error en RPC de Codex: {err_msg}",
                updated_at=datetime.now(timezone.utc),
            )

        result = rate_limit_resp.get("result")
        if not isinstance(result, dict):
            return ProviderQuotaSnapshot(
                provider=AgentKind.CODEX,
                status=QuotaStatus.ERROR,
                detail="El resultado de 'account/rateLimits/read' no es un objeto válido.",
                updated_at=datetime.now(timezone.utc),
            )

        plan_type = str(result.get("planType") or "")

        # Aislar específicamente la cuota de Codex si el diccionario rateLimitsByLimitId la contiene
        by_id = result.get("rateLimitsByLimitId")
        if isinstance(by_id, dict) and "codex" in by_id and isinstance(by_id["codex"], dict):
            limits_data = by_id["codex"]
        else:
            limits_data = result.get("rateLimits", {})

        if not isinstance(limits_data, dict):
            limits_data = {}

        windows: list[QuotaWindow] = []
        for key, default_label in (("primary", "Ventana principal"), ("secondary", "Ventana secundaria")):
            win_raw = limits_data.get(key)
            if isinstance(win_raw, dict):
                used_pct = win_raw.get("usedPercent")
                duration_mins = win_raw.get("windowDurationMins")
                resets_at_raw = win_raw.get("resetsAt")
                resets_at = None
                if isinstance(resets_at_raw, (int, float)):
                    try:
                        resets_at = datetime.fromtimestamp(resets_at_raw, tz=timezone.utc)
                    except (ValueError, OverflowError, OSError):
                        resets_at = None

                if duration_mins:
                    if duration_mins >= 1440 and duration_mins % 1440 == 0:
                        w_name = f"Ventana {duration_mins // 1440}d ({key})"
                    elif duration_mins >= 60 and duration_mins % 60 == 0:
                        w_name = f"Ventana {duration_mins // 60}h ({key})"
                    else:
                        w_name = f"Ventana {duration_mins}m ({key})"
                else:
                    w_name = default_label

                windows.append(
                    QuotaWindow(
                        window_id=key,
                        name=w_name,
                        metric_type=QuotaMetricType.USED_PERCENT,
                        percentage=float(used_pct) if used_pct is not None else None,
                        window_minutes=duration_mins,
                        resets_at=resets_at,
                    )
                )

        group = QuotaGroup(
            group_id="codex",
            name="Codex",
            description="Límites asignados a Codex en la suscripción ChatGPT",
            windows=tuple(windows),
        )

        status = QuotaStatus.AVAILABLE if windows else QuotaStatus.NO_DATA
        return ProviderQuotaSnapshot(
            provider=AgentKind.CODEX,
            plan_type=plan_type,
            status=status,
            groups=(group,),
            updated_at=datetime.now(timezone.utc),
            detail="",
        )
