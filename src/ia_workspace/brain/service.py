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
    MAX_TITLE_LENGTH,
    BrainError,
    DocumentMeta,
    Scope,
    _first_heading,
    categories_for,
    category,
    clean_tags,
    find_secret,
    parse_document,
    serialize_document,
    slugify,
    summarize,
    validate_document,
)
from ia_workspace.brain.proposals import (
    BrainConflictError,
    ProposalContent,
    ProposalMeta,
    ProposalOperation,
    ProposalRepository,
    ProposalStatus,
    parse_proposal,
    serialize_proposal,
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
        proposals: ProposalRepository | None = None,
        readonly: bool = False,
    ) -> None:
        self.store = BrainStore(root, readonly=readonly)
        self._index = index
        self._projects = projects
        self._project_name = project_name
        self._proposals = proposals
        self._readonly = readonly
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
        if self.store.readonly:
            raise BrainError("Este proyecto todavía no tiene memoria.")
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
        if scope is Scope.PROJECT and project_key and self._proposals is not None:
            project_id = self._projects.project_for_key(project_key)
            if project_id is not None:
                self._reconcile_proposals(project_id, project_key)
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

        Explica el uso de index, search y read recibiendo la ruta relativa de un documento específico.
        Nunca incluye el contenido de la memoria (< 600 caracteres).
        """
        key = self.project_key(project)
        self.sync(Scope.PROJECT, key)
        return (
            f"Memoria de «{project.name}» (IA Workspace). Prohibido cargarla entera por defecto.\n"
            f"- Índice: {self.index_path(Scope.PROJECT, key)}\n"
            f"- CLI: {command}\n"
            "  * index: ver índice | search \"<términos>\": buscar | read <ruta-relativa>: leer documento específico\n"
            "  * propose: sugerir cambios a la memoria (el usuario los revisa y aprueba)\n"
            "Ejecuta en la carpeta del proyecto: solo accede a su memoria y a la global."
        )

    # ---- propuestas de actualización (fase 3C.1) ---------------------------------------

    def propose(
        self,
        project: Project,
        operation: ProposalOperation | str,
        title: str,
        body: str,
        *,
        relpath: str | None = None,
        category_key: str | None = None,
        module: str = "",
        tags: str | tuple[str, ...] = (),
        summary: str = "",
        agent_kind: str = "",
    ) -> ProposalMeta:
        """Registra una propuesta de actualización de la memoria para que el usuario la revise."""
        if self._readonly:
            raise BrainError("La memoria está abierta en modo de solo lectura.")
        if self._proposals is None:
            raise BrainError("El repositorio de propuestas no está configurado.")

        secret = find_secret(body) or find_secret(title)
        if secret:
            raise BrainError(f"El texto parece contener {secret}: quítalo antes de proponerlo.")

        raw_bytes = body.encode("utf-8")
        if len(raw_bytes) > MAX_DOCUMENT_BYTES:
            raise BrainError(
                f"El contenido propuesto supera el límite permitido ({len(raw_bytes) // 1024} KiB > 64 KiB)."
            )

        op = ProposalOperation(operation)
        key = self.project_key(project)

        clean_t = clean_tags(tags)
        clean_title = title.strip()

        base_hash = ""
        cat_key = ""
        if op is ProposalOperation.UPDATE:
            if not relpath:
                raise BrainError("Para actualizar se debe indicar la ruta relativa del documento existente.")
            self.store.check_relpath(relpath)
            if not self.store.exists(Scope.PROJECT, key, relpath):
                raise BrainError(f"El documento «{relpath}» no existe en la memoria.")
            doc_raw = self.store.resolve(Scope.PROJECT, key, relpath).read_bytes()
            base_hash = hashlib.sha256(doc_raw).hexdigest()
            cat_key = relpath.partition("/")[0]
            if not clean_title:
                existing = self.read(Scope.PROJECT, key, relpath)
                clean_title = existing.meta.title
            target_relpath = relpath
        else:
            # CREATE
            cat_key = category_key or (relpath.partition("/")[0] if relpath else "")
            cat = category(Scope.PROJECT, cat_key)
            if cat is None:
                raise BrainError(
                    f"Categoría no válida. Debe ser una de: {', '.join(c.key for c in categories_for(Scope.PROJECT))}"
                )
            if not clean_title:
                clean_title = _first_heading(body) or (Path(relpath).stem if relpath else "") or "Nuevo documento"
            clean_title = clean_title[:MAX_TITLE_LENGTH]
            if relpath:
                self.store.check_relpath(relpath)
                target_relpath = relpath
            else:
                folder = f"{cat.key}/{slugify(module, 40)}" if (cat.modular and module.strip()) else cat.key
                base = slugify(clean_title)
                target_relpath = f"{folder}/{base}.md"
            if self.store.exists(Scope.PROJECT, key, target_relpath):
                raise BrainError(
                    f"El documento «{target_relpath}» ya existe. Usa la operación «update» para actualizarlo."
                )

        proposal_id = f"p-{secrets.token_hex(4)}"
        while self._proposals.get(proposal_id) is not None:
            proposal_id = f"p-{secrets.token_hex(4)}"

        cat = category(Scope.PROJECT, cat_key)
        mod = module if (cat and cat.modular) else ""

        meta = ProposalMeta(
            id=proposal_id,
            project_id=project.id,
            project_key=key,
            agent_kind=agent_kind or "cli",
            operation=op,
            relpath=target_relpath,
            title=clean_title,
            category=cat_key,
            module=mod,
            tags=clean_t,
            summary=summary.strip() or summarize(body),
            base_hash=base_hash,
            status=ProposalStatus.PENDING,
            created_at=datetime.now(UTC).replace(microsecond=0),
        )

        content_text = serialize_proposal(meta, body)
        self.store.write_proposal_text(key, proposal_id, content_text)
        try:
            self._proposals.add(meta)
        except Exception:
            self.store.delete_proposal_file(key, proposal_id)
            raise
        self._emit(Scope.PROJECT, key)
        return meta

    def _reconcile_proposals(self, project_id: int, project_key: str) -> None:
        if self._proposals is None:
            return
        p_root = self.store.proposals_root(project_key)
        on_disk_ids: set[str] = set()
        if p_root.is_dir():
            for p_file in p_root.glob("p-*.md"):
                if p_file.is_file() and not p_file.is_symlink():
                    proposal_id = p_file.stem
                    on_disk_ids.add(proposal_id)
                    existing = self._proposals.get(proposal_id)
                    if existing is None:
                        try:
                            text = p_file.read_text(encoding="utf-8")
                            fields, body = parse_proposal(text)
                            created_str = fields.get("created_at") or fields.get("created")
                            created_at = (
                                datetime.fromisoformat(created_str)
                                if created_str
                                else datetime.fromtimestamp(p_file.stat().st_mtime, UTC).replace(microsecond=0)
                            )
                            reviewed_str = fields.get("reviewed_at") or fields.get("reviewed")
                            reviewed_at = datetime.fromisoformat(reviewed_str) if reviewed_str else None
                            tags_raw = fields.get("tags", "")
                            tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
                            meta = ProposalMeta(
                                id=proposal_id,
                                project_id=project_id,
                                project_key=project_key,
                                agent_kind=fields.get("agent_kind") or fields.get("agent") or "cli",
                                operation=ProposalOperation(fields.get("operation", "create")),
                                relpath=fields.get("relpath", ""),
                                title=fields.get("title", proposal_id),
                                category=fields.get("category", ""),
                                module=fields.get("module", ""),
                                tags=tags,
                                summary=fields.get("summary") or summarize(body),
                                base_hash=fields.get("base_hash", ""),
                                status=ProposalStatus(fields.get("status", "pending")),
                                created_at=created_at,
                                reviewed_at=reviewed_at,
                                review_note=fields.get("review_note", ""),
                            )
                            self._proposals.add(meta)
                        except Exception:
                            continue
        for row in self._proposals.list_for_project(project_id):
            if row.id not in on_disk_ids:
                self._proposals.delete(row.id)

    def list_proposals(self, project_id: int, status: ProposalStatus | None = None) -> list[ProposalMeta]:
        if self._proposals is None:
            return []
        return self._proposals.list_for_project(project_id, status)

    def count_pending_proposals(self, project_id: int) -> int:
        if self._proposals is None:
            return 0
        return self._proposals.count_pending(project_id)

    def get_proposal(self, project: Project, proposal_id: str) -> ProposalContent:
        if self._proposals is None:
            raise BrainError("El repositorio de propuestas no está configurado.")
        meta = self._proposals.get(proposal_id)
        if meta is None or meta.project_id != project.id:
            raise BrainError("Propuesta no encontrada en este proyecto.")
        text = self.store.read_proposal_text(meta.project_key, proposal_id)
        _fields, body = parse_proposal(text)
        return ProposalContent(meta=meta, body=body)

    def approve_proposal(self, project: Project, proposal_id: str) -> DocumentMeta:
        """Aprueba una propuesta de actualización, verificando que no haya cambiado el original."""
        if self._readonly:
            raise BrainError("La memoria está abierta en modo de solo lectura.")
        if self._proposals is None:
            raise BrainError("El repositorio de propuestas no está configurado.")
        meta = self._proposals.get(proposal_id)
        if meta is None or meta.project_id != project.id:
            raise BrainError("Propuesta no encontrada en este proyecto.")
        if meta.status not in (ProposalStatus.PENDING, ProposalStatus.CONFLICT):
            raise BrainError(f"La propuesta ya fue revisada (estado: {meta.status.label}).")

        content = self.get_proposal(project, proposal_id)
        body = content.body
        key = meta.project_key

        if meta.operation is ProposalOperation.UPDATE:
            if not self.store.exists(Scope.PROJECT, key, meta.relpath):
                self._proposals.update_status(
                    proposal_id,
                    ProposalStatus.CONFLICT,
                    datetime.now(UTC),
                    "El documento original ya no existe en la memoria.",
                )
                self._emit(Scope.PROJECT, key)
                raise BrainConflictError("Conflicto: el documento de destino ya no existe en la memoria.")
            current_raw = self.store.resolve(Scope.PROJECT, key, meta.relpath).read_bytes()
            current_hash = hashlib.sha256(current_raw).hexdigest()
            if meta.base_hash and current_hash != meta.base_hash:
                self._proposals.update_status(
                    proposal_id,
                    ProposalStatus.CONFLICT,
                    datetime.now(UTC),
                    "El documento cambió después de crearse la propuesta.",
                )
                self._emit(Scope.PROJECT, key)
                raise BrainConflictError(
                    "Conflicto: el documento original cambió después de crearse la propuesta. "
                    "Revisa las diferencias antes de aprobar."
                )
            doc_meta = self.update(Scope.PROJECT, key, meta.relpath, meta.title, body, tags=meta.tags)
        else:
            # CREATE
            if self.store.exists(Scope.PROJECT, key, meta.relpath):
                self._proposals.update_status(
                    proposal_id,
                    ProposalStatus.CONFLICT,
                    datetime.now(UTC),
                    "El documento ya existe en la memoria.",
                )
                self._emit(Scope.PROJECT, key)
                raise BrainConflictError("Conflicto: el documento ya existe en la memoria.")
            doc_meta = self.create(
                Scope.PROJECT,
                key,
                meta.category,
                meta.title,
                body,
                tags=meta.tags,
                module=meta.module,
            )

        now = datetime.now(UTC).replace(microsecond=0)
        success = self._proposals.update_status(
            proposal_id,
            ProposalStatus.APPROVED,
            now,
            expected_status=(ProposalStatus.PENDING, ProposalStatus.CONFLICT),
        )
        if not success:
            raise BrainError(f"La propuesta ya fue revisada (estado: {meta.status.label}).")
        updated_meta = ProposalMeta(
            id=meta.id,
            project_id=meta.project_id,
            project_key=meta.project_key,
            agent_kind=meta.agent_kind,
            operation=meta.operation,
            relpath=meta.relpath,
            title=meta.title,
            category=meta.category,
            module=meta.module,
            tags=meta.tags,
            summary=meta.summary,
            base_hash=meta.base_hash,
            status=ProposalStatus.APPROVED,
            created_at=meta.created_at,
            reviewed_at=now,
            review_note=meta.review_note,
        )
        self.store.write_proposal_text(key, proposal_id, serialize_proposal(updated_meta, body))
        self._emit(Scope.PROJECT, key)
        return doc_meta

    def reject_proposal(self, project: Project, proposal_id: str, note: str = "") -> None:
        """Rechaza una propuesta sin tocar los documentos aprobados."""
        if self._readonly:
            raise BrainError("La memoria está abierta en modo de solo lectura.")
        if self._proposals is None:
            raise BrainError("El repositorio de propuestas no está configurado.")
        meta = self._proposals.get(proposal_id)
        if meta is None or meta.project_id != project.id:
            raise BrainError("Propuesta no encontrada en este proyecto.")
        if meta.status not in (ProposalStatus.PENDING, ProposalStatus.CONFLICT):
            raise BrainError(f"La propuesta ya fue revisada (estado: {meta.status.label}).")

        now = datetime.now(UTC).replace(microsecond=0)
        success = self._proposals.update_status(
            proposal_id,
            ProposalStatus.REJECTED,
            now,
            review_note=note,
            expected_status=(ProposalStatus.PENDING, ProposalStatus.CONFLICT),
        )
        if not success:
            raise BrainError(f"La propuesta ya fue revisada (estado: {meta.status.label}).")
        content = self.get_proposal(project, proposal_id)
        updated_meta = ProposalMeta(
            id=meta.id,
            project_id=meta.project_id,
            project_key=meta.project_key,
            agent_kind=meta.agent_kind,
            operation=meta.operation,
            relpath=meta.relpath,
            title=meta.title,
            category=meta.category,
            module=meta.module,
            tags=meta.tags,
            summary=meta.summary,
            base_hash=meta.base_hash,
            status=ProposalStatus.REJECTED,
            created_at=meta.created_at,
            reviewed_at=now,
            review_note=note,
        )
        self.store.write_proposal_text(
            meta.project_key, proposal_id, serialize_proposal(updated_meta, content.body)
        )
        self._emit(Scope.PROJECT, meta.project_key)
