#!/usr/bin/env bash
# Capturas de pantalla en un servidor Broadway aislado. Salida: var/screenshots/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/_broadway.sh"
start_broadway || true
export PYTHONWARNINGS="ignore::DeprecationWarning:gi.events"
for scheme in "${@:-oscuro claro}"; do
  for s in $scheme; do
    GDK_BACKEND=broadway python3 "$ROOT/scripts/screenshots.py" "$s"
  done
done
