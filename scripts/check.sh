#!/usr/bin/env bash
# Validaciones de la fase: entorno, compilación, pruebas y arranque de la UI.
# No instala nada ni escribe fuera del repositorio.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONWARNINGS="ignore::DeprecationWarning:gi.events"
export PYTHONPYCACHEPREFIX="$ROOT/var/pycache"

echo "== Entorno"
python3 - <<'EOF'
import sqlite3, sys
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk
assert sys.version_info >= (3, 12), "Se requiere Python 3.12+"
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 8), "Se requiere Libadwaita 1.8+"
print(f"Python {sys.version.split()[0]} · PyGObject {gi.__version__} · "
      f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()} · "
      f"Libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()} · "
      f"SQLite {sqlite3.sqlite_version}")
EOF

echo "== Compilación"
python3 -m compileall -q src tests scripts && echo "ok"

if python3 -m ruff --version >/dev/null 2>&1; then
  echo "== Ruff"
  python3 -m ruff check src tests scripts
fi

echo "== Pruebas de lógica"
(cd tests && python3 -m unittest test_persistence test_services test_sessions)

echo "== Prueba de interfaz (Broadway aislado)"
source "$ROOT/scripts/_broadway.sh"
if start_broadway; then
  export GDK_BACKEND=broadway
fi
LOG="$ROOT/var/ui-test.log"
mkdir -p "$ROOT/var"
(cd tests && python3 -m unittest -v test_ui) 2>&1 | tee "$LOG" | tail -3
if grep -E "(CRITICAL|Gtk-WARNING|Adwaita-WARNING|Traceback)" "$LOG"; then
  echo "La prueba de interfaz emitió avisos de GTK" >&2
  exit 1
fi
grep -q "^OK" "$LOG"
echo "Todo correcto."
