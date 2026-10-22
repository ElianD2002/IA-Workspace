"""Índice SQLite del Cerebro: metadatos y búsqueda de texto (FTS5 sin contenido).

El texto de los documentos no se copia a SQLite: la tabla FTS5 es «contentless», guarda
solo el índice invertido. Los extractos de los resultados se sacan del propio archivo
Markdown, y solo de los documentos encontrados. Si el SQLite del sistema no tuviera FTS5,
``fts_enabled`` es falso y el servicio busca leyendo los archivos.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from ia_workspace.brain.model import DocumentMeta, Scope

MAX_QUERY_TERMS = 8


def normalize(text: str) -> str:
    """Minúsculas y sin acentos, como el tokenizador ``unicode61 remove_diacritics 2``."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def query_terms(query: str) -> list[str]:
    return re.findall(r"\w+", normalize(query))[:MAX_QUERY_TERMS]


def fts_query(terms: list[str]) -> str:
    """Cada término entre comillas (nunca sintaxis de FTS5) y con prefijo: «decis» → decisión."""
    return " ".join(f'"{term}"*' for term in terms)


@dataclass(frozen=True)
class IndexedRow:
    id: int
    meta: DocumentMeta
    mtime_ns: int
    sha256: str


class BrainIndex:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.fts_enabled = self._create_fts()

    def _create_fts(self) -> bool:
        try:
            with self._conn:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS brain_fts USING fts5("
                    "title, tags, body, content='', contentless_delete=1, "
                    "tokenize='unicode61 remove_diacritics 2')"
                )
            return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _meta(row: sqlite3.Row) -> DocumentMeta:
        return DocumentMeta(
            scope=Scope(row["scope"]),
            project_key=row["project_key"],
            relpath=row["relpath"],
            title=row["title"],
            category=row["category"],
            module=row["module"],
            tags=tuple(t for t in row["tags"].split(",") if t),
            summary=row["summary"],
            updated=datetime.fromisoformat(row["updated"]) if row["updated"] else None,
            size=row["size"],
        )

    def rows(self, scope: Scope, project_key: str) -> dict[str, IndexedRow]:
        result = {}
        for row in self._conn.execute(
            "SELECT * FROM brain_documents WHERE scope = ? AND project_key = ?", (scope.value, project_key)
        ):
            result[row["relpath"]] = IndexedRow(row["id"], self._meta(row), row["mtime_ns"], row["sha256"])
        return result

    def list(self, scope: Scope, project_key: str, category: str | None = None) -> list[DocumentMeta]:
        sql = "SELECT * FROM brain_documents WHERE scope = ? AND project_key = ?"
        params: list[object] = [scope.value, project_key]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY category, module, title COLLATE NOCASE, relpath"
        return [self._meta(r) for r in self._conn.execute(sql, params)]

    def counts(self, scope: Scope, project_key: str) -> dict[str, int]:
        return {
            row[0]: row[1]
            for row in self._conn.execute(
                "SELECT category, COUNT(*) FROM brain_documents WHERE scope = ? AND project_key = ? GROUP BY category",
                (scope.value, project_key),
            )
        }

    def get(self, scope: Scope, project_key: str, relpath: str) -> DocumentMeta | None:
        row = self._conn.execute(
            "SELECT * FROM brain_documents WHERE scope = ? AND project_key = ? AND relpath = ?",
            (scope.value, project_key, relpath),
        ).fetchone()
        return self._meta(row) if row else None

    def upsert(self, meta: DocumentMeta, body: str, mtime_ns: int, sha256: str) -> None:
        with self._conn:
            existing = self._conn.execute(
                "SELECT id FROM brain_documents WHERE scope = ? AND project_key = ? AND relpath = ?",
                (meta.scope.value, meta.project_key, meta.relpath),
            ).fetchone()
            values = (meta.title, meta.category, meta.module, ",".join(meta.tags), meta.summary, meta.size,
                      mtime_ns, sha256, meta.updated.isoformat() if meta.updated else None)
            if existing:
                doc_id = existing[0]
                self._conn.execute(
                    "UPDATE brain_documents SET title=?, category=?, module=?, tags=?, summary=?, size=?, "
                    "mtime_ns=?, sha256=?, updated=? WHERE id=?",
                    (*values, doc_id),
                )
                if self.fts_enabled:
                    self._conn.execute("DELETE FROM brain_fts WHERE rowid = ?", (doc_id,))
            else:
                doc_id = self._conn.execute(
                    "INSERT INTO brain_documents (scope, project_key, relpath, title, category, module, tags, "
                    "summary, size, mtime_ns, sha256, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (meta.scope.value, meta.project_key, meta.relpath, *values),
                ).lastrowid
            if self.fts_enabled:
                self._conn.execute(
                    "INSERT INTO brain_fts (rowid, title, tags, body) VALUES (?, ?, ?, ?)",
                    (doc_id, meta.title, " ".join(meta.tags), body),
                )

    def touch(self, doc_id: int, mtime_ns: int) -> None:
        with self._conn:
            self._conn.execute("UPDATE brain_documents SET mtime_ns = ? WHERE id = ?", (mtime_ns, doc_id))

    def remove(self, doc_id: int) -> None:
        with self._conn:
            if self.fts_enabled:
                self._conn.execute("DELETE FROM brain_fts WHERE rowid = ?", (doc_id,))
            self._conn.execute("DELETE FROM brain_documents WHERE id = ?", (doc_id,))

    def search(self, scope: Scope, project_key: str, terms: list[str], category: str | None,
               limit: int) -> list[DocumentMeta]:
        """Solo con FTS5. Siempre filtrado por ámbito y proyecto: nunca mezcla memorias."""
        sql = (
            "SELECT d.* FROM brain_fts f JOIN brain_documents d ON d.id = f.rowid "
            "WHERE brain_fts MATCH ? AND d.scope = ? AND d.project_key = ?"
        )
        params: list[object] = [fts_query(terms), scope.value, project_key]
        if category:
            sql += " AND d.category = ?"
            params.append(category)
        sql += " ORDER BY bm25(brain_fts, 5.0, 2.0, 1.0) LIMIT ?"
        params.append(limit)
        return [self._meta(r) for r in self._conn.execute(sql, params)]
