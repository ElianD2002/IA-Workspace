#!/usr/bin/env bash
# Ejecuta IA Workspace desde el código fuente. Los datos quedan en var/ del repositorio.
#   scripts/run.sh               # normal
#   scripts/run.sh --simulated   # con agentes simulados (desarrollo)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "${1:-}" == "--simulated" ]]; then
  export IAW_SIMULATED_AGENTS=1
  shift
fi
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::DeprecationWarning:gi.events}"
exec python3 -m ia_workspace "$@"
