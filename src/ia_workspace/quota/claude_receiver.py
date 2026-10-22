"""Receptor local para la statusLine de Claude Code.

Se ejecuta cuando Claude Code invoca el comando configurado en 'statusLine',
recibiendo por stdin la telemetría en formato JSON. Extrae exclusivamente los
límites de uso de la suscripción y los persiste en la base de datos local SQLite.

Regla de seguridad: nunca guarda prompts, mensajes, tokens de autenticación ni
información sensible.
"""

from __future__ import annotations

import sys

from ia_workspace.config import resolve_paths
from ia_workspace.persistence.database import Database
from ia_workspace.quota.claude import parse_claude_statusline_payload
from ia_workspace.quota.models import QuotaStatus
from ia_workspace.quota.repository import QuotaRepository

MAX_PAYLOAD_BYTES = 512 * 1024


def main() -> int:
    try:
        raw = sys.stdin.read(MAX_PAYLOAD_BYTES)
    except Exception:
        raw = ""

    snapshot = parse_claude_statusline_payload(raw)

    # Persistir en la base de datos si es accesible
    try:
        db_path = resolve_paths().database
        if db_path.exists():
            db = Database(db_path)
            repo = QuotaRepository(db)
            repo.save_snapshot(snapshot)
            db.close()
    except Exception:
        # La lectura de cuota nunca debe impedir la operativa normal de Claude Code
        pass

    # Generar salida visible para la línea de estado de Claude
    status_text = ""
    if snapshot.status == QuotaStatus.AVAILABLE and snapshot.groups:
        for group in snapshot.groups:
            parts = []
            for win in group.windows:
                if win.used_percentage is not None:
                    parts.append(f"{win.name}: {win.used_percentage:.0f}%")
            if parts:
                status_text = " · ".join(parts)

    print(status_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
