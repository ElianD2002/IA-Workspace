"""Conexión SQLite y migraciones versionadas.

Cada migración es una lista de sentencias que se aplica una sola vez, en orden,
y queda registrada en ``PRAGMA user_version``. Para evolucionar el esquema basta
con añadir una entrada al final de ``MIGRATIONS``; nunca se editan las existentes.

Regla de seguridad: el esquema no contiene, ni debe contener, columnas para
credenciales o tokens de proveedores de IA.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    # 1 — registro de proyectos, estado visual por proyecto y ajustes.
    (
        """
        CREATE TABLE projects (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL CHECK (length(trim(name)) > 0),
            path           TEXT    NOT NULL UNIQUE,
            color_index    INTEGER NOT NULL DEFAULT 0,
            agents         TEXT    NOT NULL DEFAULT '[]',
            created_at     TEXT    NOT NULL,
            last_opened_at TEXT
        )
        """,
        """
        CREATE TABLE project_ui_state (
            project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            state      TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    ),
    # 2 — conversaciones de ChatGPT vinculadas a cada proyecto (solo enlace y título;
    #     nunca el contenido de la conversación ni credenciales).
    (
        """
        CREATE TABLE chat_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            url             TEXT    NOT NULL,
            link_kind       TEXT    NOT NULL CHECK (link_kind IN ('private', 'shared')),
            conversation_id TEXT    NOT NULL,
            title           TEXT    NOT NULL CHECK (length(trim(title)) > 0),
            created_at      TEXT    NOT NULL,
            UNIQUE (project_id, link_kind, conversation_id)
        )
        """,
        "CREATE INDEX chat_links_project ON chat_links(project_id, id)",
    ),
)

SCHEMA_VERSION = len(MIGRATIONS)


class Database:
    """Envoltorio fino sobre una conexión SQLite usada desde el hilo principal."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    @property
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        current = self.version
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"La base de datos usa el esquema {current}, más nuevo que el soportado "
                f"({SCHEMA_VERSION}). Actualiza IA Workspace."
            )
        for index in range(current, SCHEMA_VERSION):
            with self.conn:
                for statement in MIGRATIONS[index]:
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version = {index + 1}")

    def close(self) -> None:
        self.conn.close()
