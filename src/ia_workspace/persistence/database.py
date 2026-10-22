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
    # 3 — registro de transferencias de prompts a agentes: proyecto, agente, prompt
    #     completo, sesión y resultado inmediato de la transferencia (nunca si la
    #     tarea se completó; eso lo decide el estado real de la sesión).
    (
        """
        CREATE TABLE prompt_transfers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            agent       TEXT    NOT NULL,
            prompt      TEXT    NOT NULL CHECK (length(trim(prompt)) > 0),
            session_id  TEXT,
            method      TEXT    NOT NULL CHECK (method IN ('direct', 'manual')),
            status      TEXT    NOT NULL CHECK (status IN ('sent', 'copied', 'error')),
            detail      TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL
        )
        """,
        "CREATE INDEX prompt_transfers_project ON prompt_transfers(project_id, id DESC)",
    ),
    # 4 — Cerebro compartido. Los documentos son archivos Markdown (fuente canónica); aquí
    #     solo hay el identificador estable de la memoria de cada proyecto y metadatos para
    #     el índice y la búsqueda. El índice de texto (FTS5) se crea aparte: es derivado y
    #     puede reconstruirse en cualquier momento a partir de los archivos.
    (
        """
        CREATE TABLE brain_projects (
            project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            memory_key TEXT    NOT NULL UNIQUE,
            created_at TEXT    NOT NULL
        )
        """,
        """
        CREATE TABLE brain_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scope       TEXT    NOT NULL CHECK (scope IN ('global', 'project')),
            project_key TEXT    NOT NULL,
            relpath     TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            category    TEXT    NOT NULL,
            module      TEXT    NOT NULL DEFAULT '',
            tags        TEXT    NOT NULL DEFAULT '',
            summary     TEXT    NOT NULL DEFAULT '',
            size        INTEGER NOT NULL,
            mtime_ns    INTEGER NOT NULL,
            sha256      TEXT    NOT NULL,
            updated     TEXT,
            UNIQUE (scope, project_key, relpath)
        )
        """,
        "CREATE INDEX brain_documents_scope ON brain_documents(scope, project_key, category)",
    ),
    # 5 — Memorias de proyectos quitados del registro. Al quitar un proyecto se guarda aquí
    #     qué memoria tenía, su carpeta canónica y referencias de identidad de la carpeta
    #     (dispositivo, inodo, si era un repositorio Git). Solo se ofrece recuperarla al volver
    #     a registrar esa misma ruta y siempre con confirmación. Las filas recuperadas se
    #     conservan como historial; como mucho una fila pendiente por memoria.
    (
        """
        CREATE TABLE retired_memories (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_key           TEXT    NOT NULL,
            project_name         TEXT    NOT NULL,
            path                 TEXT    NOT NULL,
            device               INTEGER,
            inode                INTEGER,
            is_git               INTEGER,
            registered_at        TEXT,
            retired_at           TEXT    NOT NULL,
            recovered_at         TEXT,
            recovered_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL
        )
        """,
        "CREATE UNIQUE INDEX retired_memories_pending ON retired_memories(memory_key) WHERE recovered_at IS NULL",
        "CREATE INDEX retired_memories_path ON retired_memories(path)",
    ),
    # 6 — Propuestas de actualización del Cerebro Compartido (fase 3C.1).
    #     Permite a los agentes (Claude Code, OpenAI Codex, Google Antigravity) proponer
    #     nuevos conocimientos o modificaciones de documentos Markdown existentes sin
    #     escribir directamente en la memoria oficial. El usuario revisa y aprueba o
    #     rechaza cada propuesta desde la interfaz de IA Workspace.
    (
        """
        CREATE TABLE brain_proposals (
            id            TEXT PRIMARY KEY,
            project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            project_key   TEXT    NOT NULL,
            agent_kind    TEXT    NOT NULL DEFAULT '',
            operation     TEXT    NOT NULL,
            relpath       TEXT    NOT NULL,
            title         TEXT    NOT NULL,
            category      TEXT    NOT NULL,
            module        TEXT    NOT NULL DEFAULT '',
            tags          TEXT    NOT NULL DEFAULT '',
            summary       TEXT    NOT NULL DEFAULT '',
            base_hash     TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'pending',
            created_at    TEXT    NOT NULL,
            reviewed_at   TEXT,
            review_note   TEXT    NOT NULL DEFAULT ''
        )
        """,
        "CREATE INDEX brain_proposals_project_status ON brain_proposals(project_id, status)",
        "CREATE INDEX brain_proposals_created ON brain_proposals(project_id, created_at DESC)",
    ),
    # 7 — Autonomía y modos de autorización de agentes por proyecto (fase 4).
    #     Configuración individual de cada agente para el proyecto:
    #     'normal' (predeterminado), 'auto' (aprobaciones automáticas/reducidas) o
    #     'danger' (sin confirmaciones ni sandbox, requiere confirmación expresa).
    (
        """
        CREATE TABLE project_agent_modes (
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            agent_kind  TEXT    NOT NULL,
            mode        TEXT    NOT NULL DEFAULT 'normal',
            updated_at  TEXT    NOT NULL,
            PRIMARY KEY (project_id, agent_kind)
        )
        """,
        "CREATE INDEX project_agent_modes_project ON project_agent_modes(project_id)",
    ),
    # 8 — Límites de uso y cuotas de suscripciones (fase 5B.1).
    #     Caché global de las mediciones de cuotas de OpenAI Codex, Google Antigravity
    #     y Claude Code. No almacena tokens, cookies, credenciales ni información privada.
    (
        """
        CREATE TABLE subscription_quotas (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id    TEXT    NOT NULL,
            plan_type      TEXT    NOT NULL DEFAULT '',
            group_id       TEXT    NOT NULL,
            group_name     TEXT    NOT NULL,
            window_id      TEXT    NOT NULL,
            window_name    TEXT    NOT NULL,
            window_minutes INTEGER,
            metric_type    TEXT    NOT NULL CHECK (metric_type IN ('used_percent', 'remaining_percent')),
            percentage     REAL,
            resets_at      TEXT,
            updated_at     TEXT    NOT NULL,
            status         TEXT    NOT NULL CHECK (status IN ('available', 'pending', 'no_data', 'stale', 'error')),
            detail         TEXT    NOT NULL DEFAULT '',
            UNIQUE (provider_id, group_id, window_id)
        )
        """,
        "CREATE INDEX subscription_quotas_provider ON subscription_quotas(provider_id)",
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

    @classmethod
    def open_readonly(cls, path: Path | str, min_version: int = SCHEMA_VERSION) -> Database:
        """Conexión de solo lectura que nunca escribe ni crea nada junto a la base.

        Sirve dentro de sandboxes que prohíben escribir fuera del proyecto (p. ej. el de
        Codex). Con la aplicación abierta existen ``-wal``/``-shm`` y basta ``mode=ro``. Con
        la aplicación cerrada no hay WAL pendiente (todo está en el archivo principal) y se
        abre como ``immutable``, que no necesita crear el ``-shm``. No migra: si el esquema
        es anterior a ``min_version`` o más nuevo que el código, se rechaza.
        """
        db = cls.__new__(cls)
        db.path = str(path)
        uri = Path(path).resolve().as_uri()
        db.conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        try:
            db.conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.OperationalError:
            db.conn.close()
            if Path(f"{path}-wal").exists():
                raise
            db.conn = sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True)
        db.conn.row_factory = sqlite3.Row
        if not min_version <= db.version <= SCHEMA_VERSION:
            db.conn.close()
            raise RuntimeError("Los datos de IA Workspace usan otra versión: abre la aplicación para actualizarlos.")
        return db

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
