#!/usr/bin/env bash
# ==============================================================================
# instalador-local.sh — Instalación nativa reversible en el espacio de usuario (~/.local)
#
# Comandos:
#   scripts/instalador-local.sh install       Instala o actualiza IA Workspace en ~/.local
#   scripts/instalador-local.sh uninstall     Desinstala exactamente los archivos instalados
#   scripts/instalador-local.sh status        Muestra el estado de la instalación
#
# Principios de diseño (Fase 7C.2):
# - 100% espacio de usuario: CERO sudo, CERO pip obligatorio.
# - Idempotente y reversible: no altera archivos ajenos ni deja residuos.
# - Protección de datos: NO toca jamás ~/.local/share/ia-workspace (BD, Cerebro, backups).
# - Exclusividad: No permite instalar/actualizar si la app o el asistente están en ejecución.
# - Soporte para variables HOME, XDG_DATA_HOME y XDG_BIN_HOME (para pruebas aisladas).
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
APP_ID="io.github.elianmartinez.IAWorkspace"
RESTORE_APP_ID="io.github.elianmartinez.IAWorkspace.Restore"

# Rutas de destino (compatibles con entornos de prueba XDG aislados)
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
APPS_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
USER_DATA_DIR="$DATA_HOME/ia-workspace"

SOURCE_ICONS="$ROOT/data/icons/hicolor"
DESKTOP_SRC="$ROOT/data/$APP_ID.desktop"
RESTORE_DESKTOP_SRC="$ROOT/data/$APP_ID.Restore.desktop"

DESKTOP_TARGET="$APPS_DIR/$APP_ID.desktop"
RESTORE_DESKTOP_TARGET="$APPS_DIR/$APP_ID.Restore.desktop"
MANIFEST_FILE="$LIB_DIR/.install-manifest"

SIZES=(16 24 32 48 64 128 256 512)

icon_target() {
  echo "$ICONS_DIR/${1}x${1}/apps/$APP_ID.png"
}

# Comprueba si IA Workspace o RestoreAssistant están corriendo reteniendo bloqueos exclusivos
check_running_instances() {
  local target_data="$USER_DATA_DIR"
  if [[ -n "${IAW_DATA_DIR:-}" ]]; then
    target_data="$IAW_DATA_DIR"
  fi

  if [[ ! -d "$target_data" ]]; then
    return 0
  fi

  # Verificación no invasiva con fcntl.flock en Python
  python3 -c "
import os, sys, fcntl
from pathlib import Path
d = Path('$target_data')
for lock_name, label in [('.app.lock', 'IA Workspace'), ('.restore.lock', 'Asistente de Recuperación')]:
    lp = d / lock_name
    if lp.exists():
        try:
            fd = os.open(lp, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (BlockingIOError, OSError):
                sys.stderr.write(f'No se puede continuar: {label} se encuentra en ejecución.\\n')
                sys.exit(3)
            finally:
                os.close(fd)
        except OSError:
            pass
"
}

refresh_icon_cache() {
  if [[ -f "$ICONS_DIR/icon-theme.cache" ]]; then
    for tool in gtk4-update-icon-cache gtk-update-icon-cache; do
      if command -v "$tool" >/dev/null 2>&1; then
        "$tool" -q -t -f "$ICONS_DIR" 2>/dev/null || true
        break
      fi
    done
  fi
}

cmd_install() {
  echo "== Verificando pre-requisitos..."
  check_running_instances

  # Comprobar iconos oficiales
  for size in "${SIZES[@]}"; do
    if [[ ! -f "$SOURCE_ICONS/${size}x${size}/apps/$APP_ID.png" ]]; then
      echo "Error: Falta el icono oficial para ${size}x${size} en $SOURCE_ICONS" >&2
      exit 1
    fi
  done

  if [[ ! -f "$DESKTOP_SRC" || ! -f "$RESTORE_DESKTOP_SRC" ]]; then
    echo "Error: Faltan archivos .desktop en data/" >&2
    exit 1
  fi

  # Validar que los bindings GI del Python del sistema funcionan
  python3 -c "
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('WebKit', '6.0')
gi.require_version('Vte', '3.91')
from gi.repository import Gtk, Adw, WebKit, Vte
" || {
    echo "Error: Faltan librerías nativas del sistema (GTK4, Libadwaita, WebKitGTK 6.0 o VTE)." >&2
    exit 1
  }

  echo "== Instalando IA Workspace en $LIB_DIR..."

  mkdir -p "$DATA_HOME" "$BIN_DIR" "$APPS_DIR"

  # Comprobar colisiones con archivos ajenos preexistentes
  # Si un archivo existe en una ruta destino pero NO está registrado en $MANIFEST_FILE,
  # significa que pertenece a otro programa o fue colocado manualmente por el usuario.
  local target_files=(
    "$BIN_DIR/ia-workspace"
    "$BIN_DIR/iaw-restore-gui"
    "$BIN_DIR/iaw-restore"
    "$BIN_DIR/iaw-brain"
    "$BIN_DIR/iaw-claude-statusline"
    "$DESKTOP_TARGET"
    "$RESTORE_DESKTOP_TARGET"
  )
  for size in "${SIZES[@]}"; do
    target_files+=("$(icon_target "$size")")
  done

  for tf in "${target_files[@]}"; do
    if [[ -e "$tf" ]]; then
      # Si no hay manifiesto previo o el archivo no consta en él, rechazar colisión
      if [[ ! -f "$MANIFEST_FILE" ]] || ! grep -Fxq "$tf" "$MANIFEST_FILE"; then
        echo "Error de seguridad: El archivo destino '$tf' ya existe y no pertenece a una instalación previa de IA Workspace." >&2
        echo "Operación cancelada para prevenir la sobrescritura de archivos ajenos." >&2
        exit 4
      fi
    fi
  done

  # Directorio temporal de staging y backup para garantizar atomicidad y rollback ante fallos
  local staging_dir
  staging_dir="$(mktemp -d "${DATA_HOME}/.iaw-staging-XXXXXX")"
  local backup_all_dir=""

  rollback_on_failure() {
    rm -rf "$staging_dir"
    if [[ -n "$backup_all_dir" && -d "$backup_all_dir" ]]; then
      echo "== Ocurrió un error. Revirtiendo cambios a la instalación anterior completa..." >&2
      # Restaurar LIB_DIR
      if [[ -d "$backup_all_dir/lib" ]]; then
        rm -rf "$LIB_DIR"
        mv "$backup_all_dir/lib" "$LIB_DIR"
      fi
      # Restaurar binarios
      if [[ -d "$backup_all_dir/bin" ]]; then
        cp -a "$backup_all_dir/bin/." "$BIN_DIR/"
      fi
      # Restaurar lanzadores desktop
      if [[ -d "$backup_all_dir/apps" ]]; then
        cp -a "$backup_all_dir/apps/." "$APPS_DIR/"
      fi
      # Restaurar iconos
      if [[ -d "$backup_all_dir/icons" ]]; then
        cp -a "$backup_all_dir/icons/." "$ICONS_DIR/"
      fi
      rm -rf "$backup_all_dir"
    fi
  }
  trap 'rollback_on_failure' ERR RETURN

  # Si existe una instalación previa con manifiesto, respaldar todos sus componentes para rollback integral
  if [[ -f "$MANIFEST_FILE" ]]; then
    backup_all_dir="$(mktemp -d "${DATA_HOME}/.iaw-full-backup-XXXXXX")"
    if [[ -d "$LIB_DIR" ]]; then
      mkdir -p "$backup_all_dir/lib"
      cp -a "$LIB_DIR/." "$backup_all_dir/lib/"
    fi
    mkdir -p "$backup_all_dir/bin" "$backup_all_dir/apps" "$backup_all_dir/icons"
    while IFS= read -r f; do
      [[ -z "$f" ]] && continue
      if [[ -f "$f" ]]; then
        if [[ "$f" == "$BIN_DIR/"* ]]; then
          cp -a "$f" "$backup_all_dir/bin/"
        elif [[ "$f" == "$APPS_DIR/"* ]]; then
          cp -a "$f" "$backup_all_dir/apps/"
        elif [[ "$f" == "$ICONS_DIR/"* ]]; then
          local rel_icon="${f#$ICONS_DIR/}"
          mkdir -p "$backup_all_dir/icons/$(dirname "$rel_icon")"
          cp -a "$f" "$backup_all_dir/icons/$rel_icon"
        fi
      fi
    done < "$MANIFEST_FILE"
  fi

  mkdir -p "$staging_dir/ia_workspace"
  cp -a "$ROOT/src/ia_workspace/." "$staging_dir/ia_workspace/"
  # Limpiar cachés Python del staging
  find "$staging_dir" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

  # Crear manifest de archivos para desinstalación determinista
  local manifest_tmp="$staging_dir/.install-manifest"
  : > "$manifest_tmp"

  echo "$LIB_DIR" >> "$manifest_tmp"

  # 1. Copiar código a LIB_DIR
  rm -rf "$LIB_DIR"
  mkdir -p "$LIB_DIR"
  cp -a "$staging_dir/ia_workspace" "$LIB_DIR/"

  # 2. Instalar lanzadores ejecutables en BIN_DIR
  cat <<'EOF' > "$BIN_DIR/ia-workspace"
#!/usr/bin/env bash
set -euo pipefail

# Descubrimiento de herramientas de usuario en el PATH para sesiones gráficas GNOME
if [[ -n "${HOME:-}" ]]; then
  NEW_PATHS=""
  for candidate in \
    "$HOME/.local/bin" \
    "$HOME/.npm-global/bin" \
    "$HOME/.opencode/bin" \
    "$HOME/.cargo/bin" \
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

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::DeprecationWarning:gi.events}"
exec python3 -m ia_workspace "$@"
EOF
  chmod 755 "$BIN_DIR/ia-workspace"
  echo "$BIN_DIR/ia-workspace" >> "$manifest_tmp"

  cat <<'EOF' > "$BIN_DIR/iaw-restore-gui"
#!/usr/bin/env bash
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ia_workspace.ui.dialogs.restore_assistant "$@"
EOF
  chmod 755 "$BIN_DIR/iaw-restore-gui"
  echo "$BIN_DIR/iaw-restore-gui" >> "$manifest_tmp"

  cat <<'EOF' > "$BIN_DIR/iaw-restore"
#!/usr/bin/env bash
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ia_workspace.restore_cli "$@"
EOF
  chmod 755 "$BIN_DIR/iaw-restore"
  echo "$BIN_DIR/iaw-restore" >> "$manifest_tmp"

  cat <<'EOF' > "$BIN_DIR/iaw-brain"
#!/usr/bin/env bash
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ia_workspace.brain "$@"
EOF
  chmod 755 "$BIN_DIR/iaw-brain"
  echo "$BIN_DIR/iaw-brain" >> "$manifest_tmp"

  cat <<'EOF' > "$BIN_DIR/iaw-claude-statusline"
#!/usr/bin/env bash
set -euo pipefail
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB_DIR="$DATA_HOME/ia-workspace-app"
export PYTHONPATH="$LIB_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ia_workspace.quota.claude_receiver "$@"
EOF
  chmod 755 "$BIN_DIR/iaw-claude-statusline"
  echo "$BIN_DIR/iaw-claude-statusline" >> "$manifest_tmp"

  # 3. Instalar archivos .desktop
  local tmp_desktop
  tmp_desktop="$(mktemp --suffix=.desktop)"
  sed -e "s|^Exec=.*|Exec=\"$BIN_DIR/ia-workspace\"|" \
      -e "/^Exec=/a TryExec=$BIN_DIR/ia-workspace" \
      "$DESKTOP_SRC" > "$tmp_desktop"
  if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$tmp_desktop"
  fi
  install -m 0644 "$tmp_desktop" "$DESKTOP_TARGET"
  rm -f "$tmp_desktop"
  echo "$DESKTOP_TARGET" >> "$manifest_tmp"

  local tmp_restore_desktop
  tmp_restore_desktop="$(mktemp --suffix=.desktop)"
  sed -e "s|^Exec=.*|Exec=\"$BIN_DIR/iaw-restore-gui\" %f|" \
      -e "/^Exec=/a TryExec=$BIN_DIR/iaw-restore-gui" \
      "$RESTORE_DESKTOP_SRC" > "$tmp_restore_desktop"
  if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$tmp_restore_desktop"
  fi
  install -m 0644 "$tmp_restore_desktop" "$RESTORE_DESKTOP_TARGET"
  rm -f "$tmp_restore_desktop"
  echo "$RESTORE_DESKTOP_TARGET" >> "$manifest_tmp"

  # 4. Instalar iconos oficiales
  for size in "${SIZES[@]}"; do
    local target
    target="$(icon_target "$size")"
    mkdir -p "$(dirname "$target")"
    install -m 0644 "$SOURCE_ICONS/${size}x${size}/apps/$APP_ID.png" "$target"
    echo "$target" >> "$manifest_tmp"
  done
  refresh_icon_cache

  # 5. Guardar manifiesto de instalación
  mv "$manifest_tmp" "$MANIFEST_FILE"

  if [[ -n "$backup_all_dir" && -d "$backup_all_dir" ]]; then
    rm -rf "$backup_all_dir"
  fi

  echo "== Instalación completada exitosamente."
  echo "Lanzador principal: $DESKTOP_TARGET"
  echo "Lanzador recuperación: $RESTORE_DESKTOP_TARGET"
  echo "Binarios: $BIN_DIR/ia-workspace, $BIN_DIR/iaw-restore-gui, $BIN_DIR/iaw-restore"
}

cmd_uninstall() {
  echo "== Verificando instancias activas antes de desinstalar..."
  check_running_instances

  if [[ ! -f "$MANIFEST_FILE" && ! -f "$DESKTOP_TARGET" ]]; then
    echo "IA Workspace no parece estar instalado en $DATA_HOME."
    return 0
  fi

  echo "== Desinstalando componentes instalados de IA Workspace..."

  if [[ -f "$MANIFEST_FILE" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      if [[ "$line" == "$LIB_DIR" ]]; then
        rm -rf "$LIB_DIR"
      elif [[ -f "$line" ]]; then
        rm -f "$line"
      fi
    done < "$MANIFEST_FILE"
    rm -f "$MANIFEST_FILE"
  else
    # Fallback si no había manifiesto pero sí el lanzador oficial
    rm -f "$DESKTOP_TARGET" "$RESTORE_DESKTOP_TARGET"
    rm -f "$BIN_DIR/ia-workspace" "$BIN_DIR/iaw-restore-gui" "$BIN_DIR/iaw-restore" "$BIN_DIR/iaw-brain" "$BIN_DIR/iaw-claude-statusline"
    rm -rf "$LIB_DIR"
    for size in "${SIZES[@]}"; do
      rm -f "$(icon_target "$size")"
    done
  fi

  for size in "${SIZES[@]}"; do
    rmdir --ignore-fail-on-non-empty "$ICONS_DIR/${size}x${size}/apps" "$ICONS_DIR/${size}x${size}" 2>/dev/null || true
  done
  refresh_icon_cache

  echo "== Desinstalación completada."
  echo "Nota de seguridad: Los datos de tus proyectos, base de datos SQLite y respaldos en:"
  echo "  $USER_DATA_DIR"
  echo "se han conservado intactos y no fueron eliminados."
}

cmd_status() {
  echo "Estado de la instalación de IA Workspace:"
  echo "  Directorio de código: $LIB_DIR ($( [[ -d "$LIB_DIR" ]] && echo 'instalado' || echo 'no instalado' ))"
  echo "  Lanzador principal:   $DESKTOP_TARGET ($( [[ -f "$DESKTOP_TARGET" ]] && echo 'instalado' || echo 'no instalado' ))"
  echo "  Lanzador recuperación: $RESTORE_DESKTOP_TARGET ($( [[ -f "$RESTORE_DESKTOP_TARGET" ]] && echo 'instalado' || echo 'no instalado' ))"
  echo "  Binario CLI:          $BIN_DIR/ia-workspace ($( [[ -x "$BIN_DIR/ia-workspace" ]] && echo 'instalado' || echo 'no instalado' ))"
  echo "  Binario Asistente:    $BIN_DIR/iaw-restore-gui ($( [[ -x "$BIN_DIR/iaw-restore-gui" ]] && echo 'instalado' || echo 'no instalado' ))"

  local icon_count=0
  for size in "${SIZES[@]}"; do
    [[ -f "$(icon_target "$size")" ]] && icon_count=$((icon_count + 1))
  done
  echo "  Iconos hicolor:       $icon_count de ${#SIZES[@]} instalados"
  echo "  Datos de usuario XDG: $USER_DATA_DIR ($( [[ -d "$USER_DATA_DIR" ]] && echo 'presente' || echo 'no creado aún' ))"
}

case "${1:-}" in
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  *)
    echo "Uso: $0 {install|uninstall|status}" >&2
    exit 2
    ;;
esac
