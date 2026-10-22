"""Archivos Markdown del Cerebro: rutas seguras, escritura atómica y papelera.

Toda ruta llega como relativa a la raíz de un ámbito (memoria global o de un proyecto)
y se valida antes de tocar el disco: nada de rutas absolutas, ``..``, componentes
ocultos, barras invertidas ni enlaces simbólicos. Tras resolverla, debe seguir dentro
de la raíz. Así ninguna operación puede leer ni escribir fuera de la memoria, ni pasar
de un proyecto a otro.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ia_workspace.brain.model import INDEX_FILENAME, BrainError, Scope

MAX_DEPTH = 3  # categoria/doc.md o modulos/<módulo>/doc.md
PROJECT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}$")


class UnsafePathError(BrainError):
    """Ruta rechazada por salir de la memoria o no ser un documento válido."""


class BrainStore:
    def __init__(self, root: Path, readonly: bool = False) -> None:
        self.root = Path(root)
        # Solo lectura (CLI de los agentes): nunca crea carpetas ni escribe archivos.
        self.readonly = readonly

    def _check_writable(self) -> None:
        if self.readonly:
            raise BrainError("La memoria está abierta en modo de solo lectura.")

    # ---- raíces ---------------------------------------------------------------------

    def scope_root(self, scope: Scope, project_key: str = "") -> Path:
        if scope is Scope.GLOBAL:
            if project_key:
                raise UnsafePathError("La memoria global no pertenece a ningún proyecto.")
            return self.root / "global"
        if not PROJECT_KEY.fullmatch(project_key or ""):
            raise UnsafePathError("Identificador de proyecto no válido.")
        return self.root / "projects" / project_key

    def ensure_root(self, scope: Scope, project_key: str = "") -> Path:
        root = self.scope_root(scope, project_key)
        for part in (self.root, root.parent, root):
            if part.is_symlink():
                raise UnsafePathError("La carpeta de memoria no puede ser un enlace simbólico.")
        if not self.readonly:
            root.mkdir(parents=True, exist_ok=True)
        return root

    # ---- validación de rutas ----------------------------------------------------------

    @staticmethod
    def check_relpath(relpath: str) -> PurePosixPath:
        if not isinstance(relpath, str) or not relpath or "\0" in relpath or "\\" in relpath:
            raise UnsafePathError("Ruta de documento no válida.")
        path = PurePosixPath(relpath)
        if path.is_absolute() or relpath.startswith("~"):
            raise UnsafePathError("Solo se admiten rutas relativas a la memoria.")
        parts = path.parts
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            raise UnsafePathError("La ruta no puede contener «..» ni carpetas ocultas.")
        if len(parts) < 2 or len(parts) > MAX_DEPTH:
            raise UnsafePathError("La ruta debe ser «categoría/documento.md» o «modulos/módulo/documento.md».")
        if path.suffix != ".md" or path.name == INDEX_FILENAME:
            raise UnsafePathError("Solo se admiten documentos Markdown (.md).")
        return path

    def resolve(self, scope: Scope, project_key: str, relpath: str) -> Path:
        rel = self.check_relpath(relpath)
        root = self.scope_root(scope, project_key)
        target = root.joinpath(*rel.parts)
        current = root
        for part in rel.parts:  # ningún componente puede ser un enlace simbólico
            current = current / part
            if current.is_symlink():
                raise UnsafePathError("La ruta pasa por un enlace simbólico.")
        if not target.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
            raise UnsafePathError("La ruta sale de la memoria.")
        return target

    # ---- lectura y escritura ----------------------------------------------------------

    def read_text(self, scope: Scope, project_key: str, relpath: str) -> str:
        path = self.resolve(scope, project_key, relpath)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise BrainError("El documento ya no existe.") from exc
        except UnicodeDecodeError as exc:
            raise BrainError("El documento no es texto UTF-8 válido.") from exc

    def exists(self, scope: Scope, project_key: str, relpath: str) -> bool:
        return self.resolve(scope, project_key, relpath).is_file()

    def write_text(self, scope: Scope, project_key: str, relpath: str, text: str) -> Path:
        """Escritura atómica: un archivo temporal en la misma carpeta y ``os.replace``."""
        self._check_writable()
        self.ensure_root(scope, project_key)
        path = self.resolve(scope, project_key, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise UnsafePathError("La ruta pasa por un enlace simbólico.")
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def write_index(self, scope: Scope, project_key: str, text: str) -> bool:
        """Reescribe INDEX.md solo si cambió (evita tocar la fecha sin motivo)."""
        if self.readonly:
            return False
        root = self.ensure_root(scope, project_key)
        path = root / INDEX_FILENAME
        if path.is_symlink():
            raise UnsafePathError("INDEX.md no puede ser un enlace simbólico.")
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".md", dir=root)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
        return True

    def move_to_trash(self, scope: Scope, project_key: str, relpath: str) -> Path:
        """Elimina un documento moviéndolo a la papelera de la memoria (recuperable)."""
        self._check_writable()
        path = self.resolve(scope, project_key, relpath)
        if not path.is_file():
            raise BrainError("El documento ya no existe.")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        owner = "global" if scope is Scope.GLOBAL else project_key
        trash = self.root / ".trash" / owner / stamp
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / relpath.replace("/", "__")
        os.replace(path, target)
        for parent in (path.parent, path.parent.parent):  # limpia carpetas de módulo vacías
            if parent != self.scope_root(scope, project_key) and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        return target

    def iter_documents(self, scope: Scope, project_key: str = ""):
        """Rutas relativas de los .md de un ámbito (sin INDEX.md, ocultos ni enlaces)."""
        root = self.scope_root(scope, project_key)
        if not root.is_dir() or root.is_symlink():
            return
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and not (Path(dirpath) / d).is_symlink())
            for name in sorted(filenames):
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if name.startswith(".") or not name.endswith(".md") or rel == INDEX_FILENAME or full.is_symlink():
                    continue
                yield rel
