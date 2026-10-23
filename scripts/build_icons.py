"""Genera los tamaños del tema de iconos «hicolor» a partir del icono oficial.

Uso:
    python3 scripts/build_icons.py          # regenera data/icons/hicolor/
    python3 scripts/build_icons.py --check  # solo comprueba que están al día

El original aprobado (``data/icons/app-icon.png``) nunca se modifica: cada tamaño se
obtiene reduciéndolo directamente con interpolación de alta calidad, sin recortar,
redibujar ni cambiar colores. El nombre del icono coincide con el identificador de la
aplicación, como exige GNOME para asociar la ventana, el lanzador y el dock.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import gi  # noqa: E402

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from ia_workspace import APP_ID, APP_RESTORE_ID  # noqa: E402

THEME_DIR = ROOT / "data" / "icons" / "hicolor"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

ICON_DEFINITIONS = (
    ("app-icon.png", APP_ID),
    ("app-icon-restore.png", APP_RESTORE_ID),
)


def target(size: int, icon_id: str = APP_ID) -> Path:
    return THEME_DIR / f"{size}x{size}" / "apps" / f"{icon_id}.png"


def render(size: int, source_path: Path) -> bytes:
    source = GdkPixbuf.Pixbuf.new_from_file(str(source_path))
    scaled = source.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
    ok, data = scaled.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError(f"no se pudo codificar el icono de {size}px desde {source_path.name}")
    return bytes(data)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    stale = []

    for filename, icon_id in ICON_DEFINITIONS:
        source_path = ROOT / "data" / "icons" / filename
        if not source_path.is_file():
            raise RuntimeError(f"falta el icono de origen: {source_path}")
        before = hashlib.sha256(source_path.read_bytes()).hexdigest()

        for size in SIZES:
            data = render(size, source_path)
            path = target(size, icon_id)
            if path.is_file() and path.read_bytes() == data:
                continue
            stale.append(path)
            if not check_only:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)

        if hashlib.sha256(source_path.read_bytes()).hexdigest() != before:
            raise RuntimeError(f"el icono original {filename} cambió durante la generación")

    if check_only and stale:
        print("Iconos desactualizados (ejecuta scripts/build_icons.py):", *map(str, stale), sep="\n  ")
        return 1
    print("Iconos al día." if not stale else f"Generados {len(stale)} iconos en {THEME_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
