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

from ia_workspace import APP_ID  # noqa: E402

SOURCE = ROOT / "data" / "icons" / "app-icon.png"
THEME_DIR = ROOT / "data" / "icons" / "hicolor"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def target(size: int) -> Path:
    return THEME_DIR / f"{size}x{size}" / "apps" / f"{APP_ID}.png"


def render(size: int) -> bytes:
    source = GdkPixbuf.Pixbuf.new_from_file(str(SOURCE))
    scaled = source.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
    ok, data = scaled.save_to_bufferv("png", [], [])
    if not ok:
        raise RuntimeError(f"no se pudo codificar el icono de {size}px")
    return bytes(data)


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    before = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    stale = []
    for size in SIZES:
        data = render(size)
        path = target(size)
        if path.is_file() and path.read_bytes() == data:
            continue
        stale.append(path)
        if not check_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != before:
        raise RuntimeError("el icono original cambió durante la generación")
    if check_only and stale:
        print("Iconos desactualizados (ejecuta scripts/build_icons.py):", *map(str, stale), sep="\n  ")
        return 1
    print("Iconos al día." if not stale else f"Generados {len(stale)} tamaños en {THEME_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
