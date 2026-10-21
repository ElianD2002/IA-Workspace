"""Resolución de rutas de datos de la aplicación.

Prioridad:
1. Variable de entorno ``IAW_DATA_DIR``.
2. Ejecución desde el código fuente (existe ``pyproject.toml`` junto a ``src/``):
   ``<raíz del repositorio>/var``. Así el desarrollo no escribe fuera del proyecto.
3. Instalación normal: ``$XDG_DATA_HOME/ia-workspace`` (por defecto ``~/.local/share``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DB_FILENAME = "ia-workspace.db"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / DB_FILENAME


def _source_root() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "ia_workspace").is_dir():
        return root
    return None


def resolve_paths(env: dict[str, str] | None = None) -> AppPaths:
    env = dict(os.environ) if env is None else env
    if override := env.get("IAW_DATA_DIR"):
        return AppPaths(Path(override).expanduser())
    if (root := _source_root()) is not None:
        return AppPaths(root / "var")
    xdg = env.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return AppPaths(Path(xdg) / "ia-workspace")
