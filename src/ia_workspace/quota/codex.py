"""Conector oficial para consultar cuotas de OpenAI Codex mediante 'codex app-server'."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import shutil
import subprocess
import time

from ia_workspace.domain.models import AgentKind
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaMetricType,
    QuotaStatus,
    QuotaWindow,
)

Which = Callable[[str], str | None]
Runner = Callable[[list[str], list[str], float], tuple[list[str], str]]

MAX_LINE_BYTES = 512 * 1024  # 512 KB límite de seguridad por línea


def _default_stdio_rpc_runner(
    argv: list[str],
    requests: list[str],
    timeout: float,
) -> tuple[list[str], str]:
    """Ejecuta una secuencia de peticiones JSON-RPC sobre stdio y recoge las respuestas."""
    start_time = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    responses: list[str] = []
    stderr_out = ""

    try:
        assert proc.stdin is not None
        assert proc.stdout is not None

        for req in requests:
            elapsed = time.monotonic() - start_time
            remaining = max(0.1, timeout - elapsed)
            proc.stdin.write(req + "\n")
            proc.stdin.flush()

        # Leemos respuestas hasta timeout o EOF
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                raise subprocess.TimeoutExpired(argv, timeout)
            line = proc.stdout.readline(MAX_LINE_BYTES)
            if not line:
                break
            line_str = line.strip()
            if line_str:
                responses.append(line_str)
                # Si hemos obtenido respuestas para todas las solicitudes con id correlacionado
                # verificamos si ya tenemos la respuesta final buscada
                try:
                    obj = json.loads(line_str)
                    if isinstance(obj, dict) and obj.get("id") == len(requests):
                        break
                except (json.JSONDecodeError, ValueError):
                    pass

        try:
            proc.stdin.close()
        except OSError:
            pass

        try:
            proc.wait(timeout=max(0.1, timeout - (time.monotonic() - start_time)))
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=0.5)

    except Exception:
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass
        raise
    finally:
        if proc.stderr:
            try:
                stderr_out = proc.stderr.read()
            except Exception:
                pass

    return responses, stderr_out


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
        req2 = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {},
            }
        )

        try:
            responses, stderr_out = self._runner([executable, "app-server"], [req1, req2], self._timeout)
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
            detail_msg = f"Codex app-server no devolvió respuesta para rateLimits. {stderr_out}".strip()
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
