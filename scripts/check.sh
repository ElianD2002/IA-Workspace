#!/usr/bin/env bash
# Validaciones: entorno, compilación, pruebas de lógica, interfaz y terminales VTE reales.
# No instala nada ni escribe fuera del repositorio.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONWARNINGS="ignore::DeprecationWarning:gi.events"
export PYTHONPYCACHEPREFIX="$ROOT/var/pycache"
# Ninguna prueba debe cargar el sitio real de ChatGPT: por defecto, cualquier vista web de las
# pruebas apunta a un puerto local cerrado (test_chatgpt_web_ui usa su propio servidor simulado).
export IAW_CHATGPT_BASE_URL="${IAW_CHATGPT_BASE_URL:-http://127.0.0.1:9}"

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
try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte
    print(f"VTE (GTK 4) {Vte.get_major_version()}.{Vte.get_minor_version()}.{Vte.get_micro_version()}")
except (ValueError, ImportError):
    print("VTE para GTK 4 no disponible: instala gir1.2-vte-3.91 para usar terminales")
EOF

echo "== Compilación"
python3 -m compileall -q src tests scripts && echo "ok"

if python3 -m ruff --version >/dev/null 2>&1; then
  echo "== Ruff"
  python3 -m ruff check src tests scripts
fi

echo "== Iconos generados a partir del original"
python3 scripts/build_icons.py --check

echo "== Pruebas de lógica"
(cd tests && python3 -m unittest test_persistence test_services test_sessions test_process test_agents test_fixes test_prompt_transfer test_icon test_web_policy)

source "$ROOT/scripts/_broadway.sh"
if start_broadway; then
  export GDK_BACKEND=broadway
fi
mkdir -p "$ROOT/var"

run_ui_test() {  # $1 = módulo, $2 = título
  echo "== $2 (Broadway aislado)"
  local log="$ROOT/var/$1.log"
  (cd tests && python3 -m unittest -v "$1") 2>&1 | tee "$log" | tail -3
  if grep -E "(CRITICAL|Gtk-WARNING|Adwaita-WARNING|Vte-WARNING|GLib-WARNING|Warning: g_|Traceback)" "$log"; then
    echo "La prueba $1 emitió avisos de GTK" >&2
    exit 1
  fi
  grep -q "^OK" "$log"
  if grep -q "skipped" "$log"; then
    echo "AVISO: $1 se omitió (¿falta display o gir1.2-vte-3.91?)" >&2
  fi
}

run_ui_test test_ui "Prueba de interfaz"
run_ui_test test_terminals "Terminales VTE reales"
run_ui_test test_agents_ui "Agentes con CLI falsas"
run_ui_test test_workspace_ui "Panel derecho y conversaciones"
run_ui_test test_prompt_transfer_ui "Transferencia de prompts a agentes"
run_ui_test test_icon_ui "Icono oficial en GTK"
run_ui_test test_chatgpt_web_ui "ChatGPT integrado (WebKitGTK, sitio simulado)"
run_ui_test test_clipboard "Portapapeles y menús contextuales"

# Ningún dato de cuenta (de los dobles de prueba) debe acabar en registros.
if grep -rIl -E "persona@example\.invalid|FAKE-ORG-SECRET-123|sim_session_7f3a" "$ROOT/var"/*.log 2>/dev/null; then
  echo "Se filtraron datos de cuenta a los registros" >&2
  exit 1
fi
echo "Todo correcto."
