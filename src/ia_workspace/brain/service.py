"""Servicio del Cerebro compartido: localizar → seleccionar → leer.

* **Localizar**: ``INDEX.md`` y ``list()`` dan título, ruta, categoría, etiquetas y una
  línea de resumen por documento; nunca su contenido.
* **Seleccionar**: ``search()`` consulta el índice (FTS5) y solo abre los documentos
  encontrados para extraer un fragmento junto a la coincidencia.
* **Leer**: ``read()`` devuelve un documento completo, solo cuando se pide.

Los archivos Markdown son la fuente canónica. ``sync()`` compara fecha y tamaño de cada
archivo (y su hash si cambiaron) con el índice, así que también recoge ediciones hechas
a mano, y regenera ``INDEX.md`` cuando algo cambia.

Aislamiento: cada operación recibe un ámbito y, si es de proyecto, el identificador de
la memoria de ese proyecto. La raíz se deriva de ese identificador y ninguna ruta puede
salir de ella (``store.BrainStore``), de modo que un proyecto nunca lee la memoria de otro.
La memoria global es común y solo contiene preferencias generales del usuario.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from ia_workspace.brain.model import (
    INDEX_FILENAME,
    MAX_DOCUMENT_BYTES,
    BrainError,
    DocumentMeta,
    Scope,
    categories_for,
    category,
    clean_tags,
    parse_document,
    serialize_document,
    slugify,
    summarize,
    validate_document,
)
from ia_workspace.brain.store import BrainStore
from ia_workspace.domain.models import FolderIdentity, Project, RetiredMemory
from ia_workspace.persistence.brain_index import BrainIndex, normalize, query_terms
from ia_workspace.persistence.repositories import BrainProjectRepository
from ia_workspace.services.detection import folder_identity

DEFAULT_SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 25
EXCERPT_RADIUS = 90
# Un archivo editado a mano puede superar el límite de escritura; por encima de esto no
# se indexa (y se avisa) para que nunca entre algo desproporcionado en el contexto.
MAX_INDEXED_BYTES = 4 * MAX_DOCUMENT_BYTES

Listener = Callable[[Scope, str], None]


@dataclass(frozen=True)
class SearchHit:
    meta: DocumentMeta
    excerpt: str


@dataclass(frozen=True)
class DocumentContent:
    meta: DocumentMeta
    body: str
    problems: tuple[str, ...] = ()


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)  # (ruta, aviso)
    index_written: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def excerpt(body: str, terms: list[str], radius: int = EXCERPT_RADIUS) -> str:
    """Fragmento del texto alrededor de la primera palabra que coincide con algún término."""
    text = re.sub(r"\s+", " ", body).strip()
    position = None
    for match in re.finditer(r"\w+", text):
        word = normalize(match.group(0))
        if any(word.startswith(term) for term in terms):
            position = match.start()
            break
    if position is None:
        return summarize(body)
    start, end = max(0, position - radius), min(len(text), position + radius)
    snippet = text[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


@dataclass(frozen=True)
class RecoverableMemory:
    retired: RetiredMemory
    documents: int
    warnings: tuple[str, ...] = ()

    @property
    def id(self) -> int:
        return self.retired.id


def _identity_warnings(saved: FolderIdentity, current: FolderIdentity) -> tuple[str, ...]:
    if not saved.known:
        return ("No se pudo comprobar la identidad de la carpeta cuando se quitó el proyecto.",)
    if not current.known:
        return ("No se puede comprobar la identidad de la carpeta actual.",)
    warnings = []
    if (saved.device, saved.inode) != (current.device, current.inode):
        warnings.append("La carpeta tiene la misma ruta pero no es la misma que se quitó "
                        "(se reemplazó, se restauró de una copia o está en otro disco).")
    if saved.is_git is not None and saved.is_git != current.is_git:
        warnings.append("Antes era un repositorio Git y ahora no." if saved.is_git
                        else "Antes no era un repositorio Git y ahora sí.")
    return tuple(warnings)


def _escape_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


class BrainService:
    def __init__(
        self,
        root: Path,
        index: BrainIndex,
        projects: BrainProjectRepository,
        project_name: Callable[[int], str | None] = lambda _id: None,
    ) -> None:
        self.store = BrainStore(root)
        self._index = index
        self._projects = projects
        self._project_name = project_name
        self._listeners: list[Listener] = []

    @property
    def root(self) -> Path:
        return self.store.root

    @property
    def fts_enabled(self) -> bool:
        return self._index.fts_enabled

    # ---- observadores -----------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, scope: Scope, project_key: str) -> None:
        for listener in list(self._listeners):
            listener(scope, project_key)

    # ---- identidad de la memoria de cada proyecto -----------------------------------------

    def project_key(self, project: Project) -> str:
        """Identificador estable: nombre legible + 12 hex aleatorios. No cambia al renombrar
        el proyecto, y dos proyectos con el mismo nombre nunca comparten carpeta."""
        existing = self._projects.key_for(project.id)
        if existing:
            return existing
        while True:  # nunca reutiliza la carpeta de otra memoria (activa, retirada o huérfana)
            candidate = f"{slugify(project.name, 40)}-{secrets.token_hex(6)}"
            if not os.path.lexists(self.store.scope_root(Scope.PROJECT, candidate)):
                break
        return self._projects.assign(project.id, candidate)

    def existing_project_key(self, project_id: int) -> str | None:
        return self._projects.key_for(project_id)

    # ---- recuperación de la memoria de un proyecto quitado ----------------------------------

    def recoverable_memories(self, path: str) -> list[RecoverableMemory]:
        """Memorias de proyectos quitados cuya carpeta canónica era exactamente ``path``.

        Solo cuenta la ruta (nunca el nombre) y solo memorias cuya carpeta sigue en disco.
        Las diferencias con las referencias guardadas o varias candidatas para la misma
        ruta se devuelven como avisos: exigen confirmación antes de recuperar."""
        current = folder_identity(path)
        found: list[RecoverableMemory] = []
        for retired in self._projects.retired_for_path(path):
            try:
                root = self.store.scope_root(Scope.PROJECT, retired.memory_key)
            except BrainError:
                continue
            if root.is_symlink() or not root.is_dir():
                continue
            documents = sum(1 for _ in self.store.iter_documents(Scope.PROJECT, retired.memory_key))
            found.append(RecoverableMemory(retired, documents, _identity_warnings(retired.identity, current)))
        if len(found) > 1:
            note = f"Hay {len(found)} memorias anteriores para esta carpeta: comprueba cuál es la correcta."
            found = [replace(m, warnings=(*m.warnings, note)) for m in found]
        return found

    def recover_memory(self, project: Project, retired_id: int, confirmed: bool = False) -> str:
        """Asocia al proyecto la memoria anterior elegida y reutiliza su identificador y sus
        documentos tal cual (no copia, mueve ni reescribe ningún Markdown)."""
        candidate = next((m for m in self.recoverable_memories(project.path) if m.id == retired_id), None)
        if candidate is None:
            raise BrainError("Esa memoria no pertenece a esta carpeta o ya no se puede recuperar.")
        if candidate.warnings and not confirmed:
            raise BrainError("Confirma la recuperación: " + " ".join(candidate.warnings))
        if self._projects.key_for(project.id):
            raise BrainError("Este proyecto ya tiene su propia memoria.")
        try:
            key = self._projects.recover(retired_id, project.id)
        except (LookupError, sqlite3.IntegrityError) as exc:
            raise BrainError("Esa memoria ya no se puede recuperar.") from exc
        self.sync(Scope.PROJECT, key)  # solo regenera INDEX.md (p. ej. con el nombre nuevo)
        return key

    def index_path(self, scope: Scope, project_key: str = "") -> Path:
        return self.store.scope_root(scope, project_key) / INDEX_FILENAME

    # ---- sincronización archivos → índice ------------------------------------------------

    def sync(self, scope: Scope, project_key: str = "") -> SyncReport:
        report = SyncReport()
        self.store.ensure_root(scope, project_key)
        rows = self._index.rows(scope, project_key)
        seen: set[str] = set()
        for relpath in self.store.iter_documents(scope, project_key):
            seen.add(relpath)
            self._sync_one(scope, project_key, relpath, rows.get(relpath), report)
        for relpath, row in rows.items():
            if relpath not in seen:
                self._index.remove(row.id)
                report.removed += 1
        report.index_written = self.store.write_index(scope, project_key, self.render_index(scope, project_key))
        return report

    def _sync_one(self, scope: Scope, project_key: str, relpath: str, row, report: SyncReport) -> None:
        def problem(message: str) -> None:
            report.problems.append((relpath, message))
            if row is not None:
                self._index.remove(row.id)
                report.removed += 1

        parts = relpath.split("/")
        cat = category(scope, parts[0])
        if cat is None:
            return problem("carpeta que no es una categoría de esta memoria: no se indexa")
        if len(parts) != (3 if cat.modular else 2):
            expected = "modulos/<módulo>/<documento>.md" if cat.modular else f"{cat.key}/<documento>.md"
            return problem(f"ubicación no válida (se esperaba {expected}): no se indexa")
        path = self.store.resolve(scope, project_key, relpath)
        stat = path.stat()
        if row is not None and row.mtime_ns == stat.st_mtime_ns and row.meta.size == stat.st_size:
            return
        if stat.st_size > MAX_INDEXED_BYTES:
            return problem(f"demasiado grande ({stat.st_size // 1024} KiB): divídelo en varios documentos")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if row is not None and row.sha256 == digest:
            self._index.touch(row.id, stat.st_mtime_ns)
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return problem("no es texto UTF-8: no se indexa")
        parsed = parse_document(text, Path(relpath).stem.replace("-", " ").capitalize())
        report.problems.extend((relpath, p) for p in parsed.problems)
        meta = DocumentMeta(
            scope=scope,
            project_key=project_key,
            relpath=relpath,
            title=parsed.title,
            category=cat.key,
            module=parts[1] if cat.modular else "",
            tags=parsed.tags,
            summary=summarize(parsed.body),
            updated=parsed.updated or datetime.fromtimestamp(stat.st_mtime, UTC).replace(microsecond=0),
            size=stat.st_size,
        )
        self._index.upsert(meta, parsed.body, stat.st_mtime_ns, digest)
        if row is None:
            report.added += 1
        else:
            report.updated += 1

    # ---- índice ligero ------------------------------------------------------------------

    def render_index(self, scope: Scope, project_key: str = "") -> str:
        if scope is Scope.GLOBAL:
            heading = "# Memoria global"
            lead = "Preferencias generales del usuario, comunes a todos sus proyectos."
        else:
            project_id = self._projects.project_for_key(project_key)
            name = self._project_name(project_id) if project_id is not None else None
            heading = f"# Memoria del proyecto «{name}»" if name else "# Memoria del proyecto"
            lead = "Conocimiento de este proyecto únicamente."
        lines = [
            heading,
            "",
            "<!-- Generado por IA Workspace a partir de los documentos de esta carpeta. "
            "No lo edites: se regenera cuando cambia la memoria. -->",
            "",
            f"{lead} Índice ligero: cada línea es un documento con un resumen de una línea; "
            "lee solo los que necesites. Las rutas son relativas a esta carpeta.",
        ]
        documents = self._index.list(scope, project_key)
        if not documents:
            lines += ["", "_Todavía no hay documentos._"]
        for cat in categories_for(scope):
            docs = [d for d in documents if d.category == cat.key]
            if not docs:
                continue
            lines += ["", f"## {cat.label}"]
            module = None
            for doc in docs:
                if cat.modular and doc.module != module:
                    module = doc.module
                    lines += ["", f"### {module}"]
                entry = f"- [{_escape_link_text(doc.title)}]({doc.relpath})"
                if doc.summary:
                    entry += f" — {doc.summary}"
                if doc.tags:
                    entry += f" · etiquetas: {', '.join(doc.tags)}"
                lines.append(entry)
        return "\n".join(lines) + "\n"

    # ---- consultas (lo que usarán los agentes) --------------------------------------------

    def list(self, scope: Scope, project_key: str = "", category_key: str | None = None) -> list[DocumentMeta]:
        self.sync(scope, project_key)
        return self._index.list(scope, project_key, category_key)

    def counts(self, scope: Scope, project_key: str = "") -> dict[str, int]:
        return self._index.counts(scope, project_key)

    def search(self, scope: Scope, project_key: str, query: str, category_key: str | None = None,
               limit: int = DEFAULT_SEARCH_LIMIT) -> list[SearchHit]:
        self.sync(scope, project_key)
        terms = query_terms(query)
        if not terms:
            return []
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        if self._index.fts_enabled:
            metas = self._index.search(scope, project_key, terms, category_key, limit)
        else:
            metas = self._scan_search(scope, project_key, terms, category_key, limit)
        hits = []
        for meta in metas:  # solo se abren los documentos encontrados
            body = parse_document(self.store.read_text(scope, project_key, meta.relpath), meta.title).body
            hits.append(SearchHit(meta, excerpt(f"{meta.title}. {body}" if not body else body, terms)))
        return hits

    def _scan_search(self, scope: Scope, project_key: str, terms: list[str], category_key: str | None,
                     limit: int) -> list[DocumentMeta]:
        """Sin FTS5: lee los documentos del ámbito (locales y pequeños) y exige todos los términos."""
        found = []
        for meta in self._index.list(scope, project_key, category_key):
            text = f"{meta.title} {' '.join(meta.tags)} {self.store.read_text(scope, project_key, meta.relpath)}"
            words = re.findall(r"\w+", normalize(text))
            if all(any(word.startswith(term) for word in words) for term in terms):
                found.append(meta)
            if len(found) >= limit:
                break
        return found

    def read(self, scope: Scope, project_key: str, relpath: str) -> DocumentContent:
        """Lectura bajo demanda de un documento indexado de este ámbito."""
        self.store.check_relpath(relpath)
        self.sync(scope, project_key)
        meta = self._index.get(scope, project_key, relpath)
        if meta is None:
            raise BrainError("Ese documento no está en esta memoria.")
        parsed = parse_document(self.store.read_text(scope, project_key, relpath), meta.title)
        return DocumentContent(meta, parsed.body, parsed.problems)

    # ---- escritura (acciones controladas desde la interfaz) ---------------------------------

    def create(self, scope: Scope, project_key: str, category_key: str, title: str, body: str,
               tags: str | tuple[str, ...] = (), module: str = "") -> DocumentMeta:
        cat = category(scope, category_key)
        if cat is None:
            raise BrainError("Categoría no válida para esta memoria.")
        clean = clean_tags(tags)
        validate_document(title, body, clean)
        folder = cat.key
        if cat.modular:
            module_slug = slugify(module, 40) if module.strip() else ""
            if not module_slug:
                raise BrainError("Indica a qué módulo pertenece el documento.")
            folder = f"{cat.key}/{module_slug}"
        base = slugify(title)
        relpath, n = f"{folder}/{base}.md", 2
        while self.store.exists(scope, project_key, relpath):
            relpath, n = f"{folder}/{base}-{n}.md", n + 1
        self.store.write_text(scope, project_key, relpath, serialize_document(title.strip(), clean, body))
        self.sync(scope, project_key)
        self._emit(scope, project_key)
        meta = self._index.get(scope, project_key, relpath)
        assert meta is not None
        return meta

    def update(self, scope: Scope, project_key: str, relpath: str, title: str, body: str,
               tags: str | tuple[str, ...] = ()) -> DocumentMeta:
        """Reescribe un documento. La ruta no cambia: los agentes pueden seguir citándola."""
        if not self.store.exists(scope, project_key, relpath):
            raise BrainError("El documento ya no existe.")
        clean = clean_tags(tags)
        validate_document(title, body, clean)
        self.store.write_text(scope, project_key, relpath, serialize_document(title.strip(), clean, body))
        self.sync(scope, project_key)
        self._emit(scope, project_key)
        meta = self._index.get(scope, project_key, relpath)
        assert meta is not None
        return meta

    def delete(self, scope: Scope, project_key: str, relpath: str) -> Path:
        """Elimina un documento moviéndolo a la papelera de la memoria (recuperable a mano)."""
        target = self.store.move_to_trash(scope, project_key, relpath)
        self.sync(scope, project_key)
        self._emit(scope, project_key)
        return target

    # ---- referencia para agentes ------------------------------------------------------------

    def agent_brief(self, project: Project, command: str) -> str:
        """Lo único que recibirá un agente al empezar: dónde está el índice y cómo consultar.

        Nunca incluye el contenido de la memoria.
        """
        key = self.project_key(project)
        self.sync(Scope.PROJECT, key)
        return (
            f"Memoria del proyecto «{project.name}» (IA Workspace). No la cargues entera: consulta solo "
            "lo que necesite la tarea.\n"
            f"- Índice (una línea por documento): {self.index_path(Scope.PROJECT, key)}\n"
            f"- Buscar: {command} search \"<términos>\" [--category <categoría>] [--global]\n"
            f"- Leer un documento: {command} read <ruta-del-índice> [--global]\n"
            f"- Listar: {command} list [--category <categoría>] [--global]\n"
            "Ejecuta estas órdenes desde la carpeta del proyecto: solo dan acceso a su memoria y a "
            "la memoria global del usuario."
        )
