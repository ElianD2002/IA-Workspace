#!/usr/bin/env bash
# Lanzador de IA Workspace para el escritorio del usuario (GNOME), reversible.
#
#   scripts/desktop-launcher.sh install     # instala el .desktop y el icono
#   scripts/desktop-launcher.sh uninstall   # quita exactamente lo instalado
#   scripts/desktop-launcher.sh status      # muestra qué está instalado
#
# Solo escribe en $XDG_DATA_HOME (por defecto ~/.local/share), nunca en rutas del
# sistema ni con sudo. Todos los archivos llevan el nombre del identificador de la
# aplicación, así que no puede pisar archivos de otras aplicaciones. El lanzador
# instalado ejecuta scripts/run.sh de esta copia del repositorio.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_ID="io.github.elianmartinez.IAWorkspace"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_HOME/applications"
ICONS_DIR="$DATA_HOME/icons/hicolor"
SOURCE_DESKTOP="$ROOT/data/$APP_ID.desktop"
SOURCE_ICONS="$ROOT/data/icons/hicolor"
DESKTOP_TARGET="$APPS_DIR/$APP_ID.desktop"
SIZES=(16 24 32 48 64 128 256 512)

icon_target() { echo "$ICONS_DIR/${1}x${1}/apps/$APP_ID.png"; }

# Actualiza la caché de iconos solo si ya existía: no crea archivos nuevos en el tema.
refresh_icon_cache() {
  if [[ -f "$ICONS_DIR/icon-theme.cache" ]]; then
    for tool in gtk4-update-icon-cache gtk-update-icon-cache; do
      if command -v "$tool" >/dev/null 2>&1; then
        "$tool" -q -t -f "$ICONS_DIR" || true
        break
      fi
    done
  fi
}

install_launcher() {
  # Exec exige comillas y no admite ciertos caracteres sin escapar: se rechazan.
  if [[ "$ROOT" == *[\"\`\$\\]* ]]; then
    echo "La ruta del repositorio contiene caracteres no admitidos en un lanzador: $ROOT" >&2
    exit 1
  fi
  for size in "${SIZES[@]}"; do
    [[ -f "$SOURCE_ICONS/${size}x${size}/apps/$APP_ID.png" ]] || {
      echo "Falta el icono de ${size}px: ejecuta python3 scripts/build_icons.py" >&2
      exit 1
    }
  done

  local tmp
  tmp="$(mktemp --suffix=.desktop)"
  trap 'rm -f "$tmp"' RETURN
  sed -e "s|^Exec=.*|Exec=\"$ROOT/scripts/run.sh\"|" \
      -e "/^Exec=/a TryExec=$ROOT/scripts/run.sh" \
      "$SOURCE_DESKTOP" > "$tmp"
  if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$tmp"
  fi

  mkdir -p "$APPS_DIR"
  install -m 0644 "$tmp" "$DESKTOP_TARGET"
  for size in "${SIZES[@]}"; do
    install -D -m 0644 "$SOURCE_ICONS/${size}x${size}/apps/$APP_ID.png" "$(icon_target "$size")"
  done
  refresh_icon_cache
  echo "Lanzador instalado en $DESKTOP_TARGET"
  echo "Icono instalado en $ICONS_DIR/<tamaño>/apps/$APP_ID.png"
}

uninstall_launcher() {
  rm -f "$DESKTOP_TARGET"
  for size in "${SIZES[@]}"; do
    rm -f "$(icon_target "$size")"
    # Solo se borran carpetas que hayan quedado vacías.
    rmdir --ignore-fail-on-non-empty "$ICONS_DIR/${size}x${size}/apps" "$ICONS_DIR/${size}x${size}" 2>/dev/null || true
  done
  refresh_icon_cache
  echo "Lanzador e icono de IA Workspace desinstalados de $DATA_HOME"
}

status_launcher() {
  [[ -f "$DESKTOP_TARGET" ]] && echo "instalado: $DESKTOP_TARGET" || echo "no instalado: $DESKTOP_TARGET"
  local count=0
  for size in "${SIZES[@]}"; do [[ -f "$(icon_target "$size")" ]] && count=$((count + 1)); done
  echo "iconos instalados: $count de ${#SIZES[@]}"
}

case "${1:-}" in
  install) install_launcher ;;
  uninstall) uninstall_launcher ;;
  status) status_launcher ;;
  *) echo "Uso: $0 install|uninstall|status" >&2; exit 2 ;;
esac
