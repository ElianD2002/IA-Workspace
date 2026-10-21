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

# Incorpora directorios de usuario al PATH si existen y no están ya presentes.
# Permite encontrar agentes y herramientas instaladas en el espacio de usuario (~/.local/bin, etc.)
# cuando la aplicación es iniciada desde un lanzador .desktop o entorno de escritorio con PATH mínimo.
if [[ -n "${HOME:-}" ]]; then
  NEW_PATHS=""
  for candidate in \
    "$HOME/.local/bin" \
    "$HOME/.opencode/bin" \
    "$HOME/.dotnet/tools"; do
    if [[ -d "$candidate" ]]; then
      case ":${PATH:-}:${NEW_PATHS}:" in
        *":$candidate:"*) ;;
        *) NEW_PATHS="${NEW_PATHS:+$NEW_PATHS:}$candidate" ;;
      esac
    fi
  done
  if [[ -n "$NEW_PATHS" ]]; then
    export PATH="$NEW_PATHS${PATH:+:$PATH}"
  fi
  unset candidate NEW_PATHS
fi

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::DeprecationWarning:gi.events}"
exec python3 -m ia_workspace "$@"
