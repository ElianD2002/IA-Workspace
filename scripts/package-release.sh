#!/usr/bin/env bash
# ==============================================================================
# package-release.sh — Construye el archivo reproducible de release y su SHA-256
#
# Genera:
#   dist/ia-workspace-v0.1.0-beta.1.tar.gz
#   dist/ia-workspace-v0.1.0-beta.1.tar.gz.sha256
#
# Incluye exclusivamente los archivos necesarios para instalar y ejecutar
# la aplicación (código, iconos, lanzadores, scripts de usuario y licencias).
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERSION="${1:-v0.1.0-beta.1}"
DIST_DIR="$ROOT/dist"
ARCHIVE_NAME="ia-workspace-${VERSION}.tar.gz"
ARCHIVE_PATH="$DIST_DIR/$ARCHIVE_NAME"
SHA_PATH="$ARCHIVE_PATH.sha256"

echo "== Preparando empaquetado de IA Workspace ($VERSION)..."

# Asegurar que los iconos hicolor estén al día
python3 "$ROOT/scripts/build_icons.py" --check

mkdir -p "$DIST_DIR"
rm -f "$ARCHIVE_PATH" "$SHA_PATH"

STAGING_DIR="$(mktemp -d "/tmp/iaw-release-staging-XXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT

TARGET_DIR="$STAGING_DIR/ia-workspace-${VERSION}"
mkdir -p "$TARGET_DIR"

# 1. Copiar código fuente
mkdir -p "$TARGET_DIR/src"
cp -a "$ROOT/src/ia_workspace" "$TARGET_DIR/src/"
find "$TARGET_DIR/src" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# 2. Copiar recursos data (iconos y .desktop)
mkdir -p "$TARGET_DIR/data"
cp -a "$ROOT/data/icons" "$TARGET_DIR/data/"
cp -a "$ROOT/data/"*.desktop "$TARGET_DIR/data/"

# 3. Copiar scripts esenciales
mkdir -p "$TARGET_DIR/scripts"
cp -a "$ROOT/scripts/instalador-local.sh" "$TARGET_DIR/scripts/"
cp -a "$ROOT/scripts/desktop-launcher.sh" "$TARGET_DIR/scripts/"
cp -a "$ROOT/scripts/run.sh" "$TARGET_DIR/scripts/"
chmod +x "$TARGET_DIR/scripts/"*

# 4. Crear lanzador directo install.sh en la raíz del paquete
cat <<'EOF' > "$TARGET_DIR/install.sh"
#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$DIR/scripts/instalador-local.sh" install "$@"
EOF
chmod +x "$TARGET_DIR/install.sh"

# 5. Copiar documentación y metadatos
cp -a "$ROOT/README.md" "$TARGET_DIR/"
cp -a "$ROOT/CHANGELOG.md" "$TARGET_DIR/"
cp -a "$ROOT/LICENSE" "$TARGET_DIR/"
cp -a "$ROOT/SECURITY.md" "$TARGET_DIR/"
cp -a "$ROOT/pyproject.toml" "$TARGET_DIR/"

# Obtener timestamp de Git del commit actual para reproducibilidad (si está disponible)
SOURCE_DATE_EPOCH="$(git -C "$ROOT" log -1 --pretty=%ct 2>/dev/null || date +%s)"

echo "== Generando archivo $ARCHIVE_NAME..."
# Construir tarball con orden determinista y metadatos limpios
tar \
  --sort=name \
  --mtime="@$SOURCE_DATE_EPOCH" \
  --owner=0 --group=0 --numeric-owner \
  --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
  -I 'gzip -n -9' \
  -cf "$ARCHIVE_PATH" \
  -C "$STAGING_DIR" "ia-workspace-${VERSION}"

# Calcular checksum SHA-256
(
  cd "$DIST_DIR"
  sha256sum "$ARCHIVE_NAME" > "$ARCHIVE_NAME.sha256"
)

echo "== Paquete generado exitosamente:"
echo "  Archivo: $ARCHIVE_PATH ($(du -h "$ARCHIVE_PATH" | cut -f1))"
echo "  SHA-256: $(cat "$SHA_PATH")"
