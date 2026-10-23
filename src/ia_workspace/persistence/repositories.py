"""Repositorios: única capa que conoce el SQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ia_workspace.domain.models import (
    AgentKind,
    AutonomyMode,
    ChatLink,
    FolderIdentity,
    HistoryEvent,
    HistoryEventKind,
    NewProject,
    Project,
    PromptTemplate,
    PromptTemplateScope,
    PromptTransfer,
    RetiredMemory,
    Task,
    TaskHistoryEntry,
    TaskState,
    WorkspaceState,
)
from ia_workspace.persistence.database import Database


class DuplicateProjectError(Exception):
    """Ya existe un proyecto registrado con la misma carpeta."""


class DuplicateChatLinkError(Exception):
    """La conversación ya está vinculada a ese proyecto."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _parse_agents(raw: str) -> tuple[AgentKind, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    known = {kind.value for kind in AgentKind}
    return tuple(AgentKind(v) for v in values if v in known)


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def _row(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            color_index=row["color_index"],
            agents=_parse_agents(row["agents"]),
            created_at=_parse_dt(row["created_at"]),
            last_opened_at=_parse_dt(row["last_opened_at"]),
        )

    def add(self, new: NewProject) -> Project:
        color_index = self._conn.execute(
            "SELECT COALESCE(MAX(color_index) + 1, 0) FROM projects"
        ).fetchone()[0]
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO projects (name, path, color_index, agents, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        new.name.strip(),
                        new.path,
                        color_index,
                        json.dumps([a.value for a in new.agents]),
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "projects.path" in str(exc):
                raise DuplicateProjectError(new.path) from exc
            raise
        project = self.get(int(cur.lastrowid))
        assert project is not None
        return project

    def get(self, project_id: int) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_path(self, path: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE path = ?", (path,)).fetchone()
        return self._row(row) if row else None

    def list(self) -> list[Project]:
        """Proyectos ordenados por fecha de registro (orden estable de la barra lateral)."""
        rows = self._conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def rename(self, project_id: int, name: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name.strip(), project_id))

    def touch_opened(self, project_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE projects SET last_opened_at = ? WHERE id = ?", (_now(), project_id)
            )

    def remove(self, project_id: int, identity: FolderIdentity | None = None) -> None:
        """Borra el proyecto. Si tenía memoria, la deja registrada como recuperable en la
        misma transacción (la asociación de ``brain_projects`` se borra en cascada)."""
        identity = identity or FolderIdentity()
        is_git = None if identity.is_git is None else int(identity.is_git)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO retired_memories (memory_key, project_name, path, device, inode, is_git, "
                "registered_at, retired_at) "
                "SELECT b.memory_key, p.name, p.path, ?, ?, ?, p.created_at, ? "
                "FROM brain_projects b JOIN projects p ON p.id = b.project_id WHERE p.id = ?",
                (identity.device, identity.inode, is_git, _now(), project_id),
            )
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


class ChatLinkRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    @staticmethod
    def _row(row: sqlite3.Row) -> ChatLink:
        return ChatLink(
            id=row["id"],
            project_id=row["project_id"],
            url=row["url"],
            kind=row["link_kind"],
            conversation_id=row["conversation_id"],
            title=row["title"],
            created_at=_parse_dt(row["created_at"]),
        )

    def add(self, project_id: int, url: str, kind: str, conversation_id: str, title: str) -> ChatLink:
        try:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO chat_links (project_id, url, link_kind, conversation_id, title, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project_id, url, kind, conversation_id, title.strip(), _now()),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateChatLinkError(url) from exc
            raise
        link = self.get(int(cur.lastrowid))
        assert link is not None
        return link

    def get(self, link_id: int) -> ChatLink | None:
        row = self._conn.execute("SELECT * FROM chat_links WHERE id = ?", (link_id,)).fetchone()
        return self._row(row) if row else None

    def find(self, project_id: int, kind: str, conversation_id: str) -> ChatLink | None:
        row = self._conn.execute(
            "SELECT * FROM chat_links WHERE project_id = ? AND link_kind = ? AND conversation_id = ?",
            (project_id, kind, conversation_id),
        ).fetchone()
        return self._row(row) if row else None

    def list(self, project_id: int) -> list[ChatLink]:
        """Conversaciones de un único proyecto, la más reciente primero."""
        rows = self._conn.execute(
            "SELECT * FROM chat_links WHERE project_id = ? ORDER BY id DESC", (project_id,)
        ).fetchall()
        return [self._row(r) for r in rows]

    def rename(self, link_id: int, title: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE chat_links SET title = ? WHERE id = ?", (title.strip(), link_id))

    def remove(self, link_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chat_links WHERE id = ?", (link_id,))


class PromptTransferRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    @staticmethod
    def _row(row: sqlite3.Row) -> PromptTransfer:
        return PromptTransfer(
            id=row["id"],
            project_id=row["project_id"],
            agent=AgentKind(row["agent"]),
            prompt=row["prompt"],
            session_id=row["session_id"],
            method=row["method"],
            status=row["status"],
            detail=row["detail"],
            created_at=_parse_dt(row["created_at"]),
        )

    def add(
        self,
        project_id: int,
        agent: AgentKind,
        prompt: str,
        session_id: str | None,
        method: str,
        status: str,
        detail: str = "",
    ) -> PromptTransfer:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO prompt_transfers "
                "(project_id, agent, prompt, session_id, method, status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, agent.value, prompt, session_id, method, status, detail, _now()),
            )
        transfer = self.get(int(cur.lastrowid))
        assert transfer is not None
        return transfer

    def get(self, transfer_id: int) -> PromptTransfer | None:
        row = self._conn.execute("SELECT * FROM prompt_transfers WHERE id = ?", (transfer_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, project_id: int) -> list[PromptTransfer]:
        """Transferencias de un único proyecto, la más reciente primero."""
        rows = self._conn.execute(
            "SELECT * FROM prompt_transfers WHERE project_id = ? ORDER BY id DESC", (project_id,)
        ).fetchall()
        return [self._row(r) for r in rows]


class BrainProjectRepository:
    """Identificador estable de la memoria de cada proyecto (nombre de su carpeta)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def key_for(self, project_id: int) -> str | None:
        row = self._conn.execute("SELECT memory_key FROM brain_projects WHERE project_id = ?", (project_id,)).fetchone()
        return row["memory_key"] if row else None

    def assign(self, project_id: int, memory_key: str) -> str:
        """Asigna el identificador si el proyecto aún no tiene uno; devuelve el definitivo."""
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO brain_projects (project_id, memory_key, created_at) VALUES (?, ?, ?)",
                (project_id, memory_key, _now()),
            )
        key = self.key_for(project_id)
        assert key is not None
        return key

    def project_for_key(self, memory_key: str) -> int | None:
        row = self._conn.execute("SELECT project_id FROM brain_projects WHERE memory_key = ?", (memory_key,)).fetchone()
        return row["project_id"] if row else None

    # ---- memorias de proyectos quitados ---------------------------------------------------

    @staticmethod
    def _retired(row: sqlite3.Row) -> RetiredMemory:
        is_git = row["is_git"]
        return RetiredMemory(
            id=row["id"],
            memory_key=row["memory_key"],
            project_name=row["project_name"],
            path=row["path"],
            identity=FolderIdentity(row["device"], row["inode"], None if is_git is None else bool(is_git)),
            retired_at=_parse_dt(row["retired_at"]),
        )

    def retired_for_path(self, path: str) -> list[RetiredMemory]:
        """Memorias pendientes cuya carpeta canónica era exactamente ``path`` (la más reciente
        primero). Nunca se busca por nombre."""
        rows = self._conn.execute(
            "SELECT * FROM retired_memories WHERE path = ? AND recovered_at IS NULL "
            "AND memory_key NOT IN (SELECT memory_key FROM brain_projects) ORDER BY id DESC",
            (path,),
        ).fetchall()
        return [self._retired(r) for r in rows]

    def recover(self, retired_id: int, project_id: int) -> str:
        """Asocia la memoria pendiente al proyecto y marca la fila como recuperada. Falla si la
        memoria ya no está pendiente, si ya está en uso o si el proyecto ya tiene memoria."""
        with self._conn:
            row = self._conn.execute(
                "SELECT memory_key FROM retired_memories WHERE id = ? AND recovered_at IS NULL", (retired_id,)
            ).fetchone()
            if row is None:
                raise LookupError(retired_id)
            self._conn.execute(
                "INSERT INTO brain_projects (project_id, memory_key, created_at) VALUES (?, ?, ?)",
                (project_id, row["memory_key"], _now()),
            )
            self._conn.execute(
                "UPDATE retired_memories SET recovered_at = ?, recovered_project_id = ? WHERE id = ?",
                (_now(), project_id, retired_id),
            )
        return row["memory_key"]


class WorkspaceStateRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def load(self, project_id: int) -> WorkspaceState:
        row = self._conn.execute(
            "SELECT state FROM project_ui_state WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return WorkspaceState()
        try:
            data = json.loads(row["state"])
        except json.JSONDecodeError:
            return WorkspaceState()
        return WorkspaceState.from_dict(data if isinstance(data, dict) else {})

    def save(self, project_id: int, state: WorkspaceState) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO project_ui_state (project_id, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET state = excluded.state, "
                "updated_at = excluded.updated_at",
                (project_id, json.dumps(state.to_dict(), ensure_ascii=False), _now()),
            )


class SettingsRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self._conn:
            if value is None:
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )


class AgentAutonomyRepository:
    """Persistencia de niveles de autonomía y autorización de agentes por proyecto (fase 4)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def get_mode(self, project_id: int, kind: AgentKind) -> AutonomyMode:
        row = self._conn.execute(
            "SELECT mode FROM project_agent_modes WHERE project_id = ? AND agent_kind = ?",
            (project_id, kind.value),
        ).fetchone()
        if row is None:
            return AutonomyMode.NORMAL
        try:
            return AutonomyMode(row["mode"])
        except ValueError:
            return AutonomyMode.NORMAL

    def set_mode(self, project_id: int, kind: AgentKind, mode: AutonomyMode) -> None:
        now = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO project_agent_modes (project_id, agent_kind, mode, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, agent_kind) DO UPDATE SET
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (project_id, kind.value, mode.value, now),
            )

    def get_modes_for_project(self, project_id: int) -> dict[AgentKind, AutonomyMode]:
        rows = self._conn.execute(
            "SELECT agent_kind, mode FROM project_agent_modes WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        result: dict[AgentKind, AutonomyMode] = {}
        for row in rows:
            try:
                kind = AgentKind(row["agent_kind"])
                result[kind] = AutonomyMode(row["mode"])
            except ValueError:
                continue
        return result


class TaskRepository:
    """Persistencia de tareas vinculadas a proyectos, prompts y agentes (fase 6B.1)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def _row(self, row: sqlite3.Row) -> Task:
        return Task(
            id=int(row["id"]),
            project_id=int(row["project_id"]),
            title=str(row["title"]),
            transfer_id=int(row["transfer_id"]),
            prompt=str(row["prompt"]) if "prompt" in row.keys() and row["prompt"] is not None else "",
            agent=AgentKind(row["agent"]),
            session_id=str(row["session_id"]) if row["session_id"] is not None else None,
            status=TaskState(row["status"]),
            result_notes=str(row["result_notes"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _history_row(self, row: sqlite3.Row) -> TaskHistoryEntry:
        return TaskHistoryEntry(
            id=int(row["id"]),
            task_id=int(row["task_id"]),
            from_status=str(row["from_status"]),
            to_status=str(row["to_status"]),
            note=str(row["note"]),
            created_at=_parse_dt(row["created_at"]),
        )

    def create(
        self,
        project_id: int,
        title: str,
        transfer_id: int,
        agent: AgentKind,
        session_id: str | None,
        status: TaskState = TaskState.IN_PROGRESS,
        result_notes: str = "",
        initial_note: str = "Tarea iniciada por transferencia",
    ) -> Task:
        now = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO tasks
                (project_id, title, transfer_id, agent, session_id, status, result_notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    title.strip() or "Tarea",
                    transfer_id,
                    agent.value,
                    session_id,
                    status.value,
                    result_notes,
                    now,
                    now,
                ),
            )
            task_id = int(cur.lastrowid)
            self._conn.execute(
                """
                INSERT INTO task_history (task_id, from_status, to_status, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, "", status.value, initial_note, now),
            )
        task = self.get(task_id)
        assert task is not None
        return task

    def get(self, task_id: int) -> Task | None:
        query = """
            SELECT t.*, p.prompt
            FROM tasks t
            LEFT JOIN prompt_transfers p ON t.transfer_id = p.id
            WHERE t.id = ?
        """
        row = self._conn.execute(query, (task_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_transfer_id(self, transfer_id: int) -> Task | None:
        query = """
            SELECT t.*, p.prompt
            FROM tasks t
            LEFT JOIN prompt_transfers p ON t.transfer_id = p.id
            WHERE t.transfer_id = ?
        """
        row = self._conn.execute(query, (transfer_id,)).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        project_id: int | None = None,
        status: TaskState | None = None,
    ) -> list[Task]:
        """Lista tareas filtradas por proyecto y/o estado, ordenadas por actualización reciente."""
        clauses = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("t.project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("t.status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT t.*, p.prompt
            FROM tasks t
            LEFT JOIN prompt_transfers p ON t.transfer_id = p.id
            {where}
            ORDER BY t.updated_at DESC, t.id DESC
        """
        rows = self._conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def update_status(self, task_id: int, new_status: TaskState, note: str = "") -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, task_id),
            )
            self._conn.execute(
                """
                INSERT INTO task_history (task_id, from_status, to_status, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, task.status.value, new_status.value, note, now),
            )
        return self.get(task_id)

    def update_notes(self, task_id: int, notes: str) -> Task | None:
        now = _now()
        with self._conn:
            self._conn.execute(
                "UPDATE tasks SET result_notes = ?, updated_at = ? WHERE id = ?",
                (notes, now, task_id),
            )
        return self.get(task_id)

    def list_history(self, task_id: int) -> list[TaskHistoryEntry]:
        rows = self._conn.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        return [self._history_row(r) for r in rows]

    def find_by_session_id(self, session_id: str) -> list[Task]:
        query = """
            SELECT t.*, p.prompt
            FROM tasks t
            LEFT JOIN prompt_transfers p ON t.transfer_id = p.id
            WHERE t.session_id = ?
        """
        rows = self._conn.execute(query, (session_id,)).fetchall()
        return [self._row(r) for r in rows]


class PromptTemplateRepository:
    """Persistencia de plantillas de prompts reutilizables (fase 6B.2)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def _row(self, row: sqlite3.Row) -> PromptTemplate:
        return PromptTemplate(
            id=int(row["id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            category=str(row["category"]),
            scope=PromptTemplateScope(row["scope"]),
            project_id=int(row["project_id"]) if row["project_id"] is not None else None,
            content=str(row["content"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def create(
        self,
        name: str,
        content: str,
        category: str = "General",
        description: str = "",
        scope: PromptTemplateScope = PromptTemplateScope.GLOBAL,
        project_id: int | None = None,
    ) -> PromptTemplate:
        if scope == PromptTemplateScope.GLOBAL:
            project_id = None
        elif scope == PromptTemplateScope.PROJECT and project_id is None:
            raise ValueError("Una plantilla de ámbito 'project' requiere especificar project_id")

        now = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO prompt_templates
                (name, description, category, scope, project_id, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    description.strip(),
                    category.strip() or "General",
                    scope.value,
                    project_id,
                    content.strip(),
                    now,
                    now,
                ),
            )
            template_id = int(cur.lastrowid)
        tmpl = self.get(template_id)
        assert tmpl is not None
        return tmpl

    def get(self, template_id: int) -> PromptTemplate | None:
        row = self._conn.execute(
            "SELECT * FROM prompt_templates WHERE id = ?",
            (template_id,),
        ).fetchone()
        return self._row(row) if row else None

    def update(
        self,
        template_id: int,
        name: str,
        content: str,
        category: str = "General",
        description: str = "",
        scope: PromptTemplateScope = PromptTemplateScope.GLOBAL,
        project_id: int | None = None,
    ) -> PromptTemplate | None:
        if scope == PromptTemplateScope.GLOBAL:
            project_id = None
        elif scope == PromptTemplateScope.PROJECT and project_id is None:
            raise ValueError("Una plantilla de ámbito 'project' requiere especificar project_id")

        now = _now()
        with self._conn:
            cur = self._conn.execute(
                """
                UPDATE prompt_templates
                SET name = ?, description = ?, category = ?, scope = ?, project_id = ?, content = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name.strip(),
                    description.strip(),
                    category.strip() or "General",
                    scope.value,
                    project_id,
                    content.strip(),
                    now,
                    template_id,
                ),
            )
            if cur.rowcount == 0:
                return None
        return self.get(template_id)

    def delete(self, template_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM prompt_templates WHERE id = ?",
                (template_id,),
            )
            return cur.rowcount > 0

    def list_for_project(
        self,
        project_id: int,
        category: str | None = None,
        search: str = "",
    ) -> list[PromptTemplate]:
        """Plantillas accesibles para un proyecto específico (globales + propias del proyecto)."""
        clauses = ["(scope = 'global' OR (scope = 'project' AND project_id = ?))"]
        params: list[object] = [project_id]
        if category and category != "all":
            clauses.append("category = ?")
            params.append(category)
        if search.strip():
            clauses.append("(name LIKE ? OR description LIKE ? OR content LIKE ?)")
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern, pattern])

        query = f"""
            SELECT * FROM prompt_templates
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, id DESC
        """
        rows = self._conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def list_all(
        self,
        project_id: int | None = None,
        category: str | None = None,
        search: str = "",
    ) -> list[PromptTemplate]:
        """Lista plantillas. Si project_id es None, lista todas; si tiene project_id filtra solo ese."""
        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("(scope = 'project' AND project_id = ?)")
            params.append(project_id)
        if category and category != "all":
            clauses.append("category = ?")
            params.append(category)
        if search.strip():
            clauses.append("(name LIKE ? OR description LIKE ? OR content LIKE ?)")
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern, pattern])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT * FROM prompt_templates
            {where}
            ORDER BY updated_at DESC, id DESC
        """
        rows = self._conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def categories(self, project_id: int | None = None) -> list[str]:
        """Devuelve categorías únicas de plantillas disponibles."""
        if project_id is not None:
            rows = self._conn.execute(
                """
                SELECT DISTINCT category FROM prompt_templates
                WHERE (scope = 'global' OR (scope = 'project' AND project_id = ?))
                ORDER BY category ASC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT DISTINCT category FROM prompt_templates ORDER BY category ASC"
            ).fetchall()
        return [str(r[0]) for r in rows if r[0]]


def _parse_dt_safe(val: str | None) -> datetime:
    if not val:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return datetime.now(UTC)


_PROPOSAL_STATUS_INFO = {
    "pending": ("Pendiente", "wait"),
    "approved": ("Aprobada", "success"),
    "rejected": ("Rechazada", "dim-label"),
    "conflict": ("En conflicto", "error"),
}

_PROPOSAL_OP_LABELS = {
    "create": "Nuevo documento",
    "update": "Actualización",
}

_TASK_STATUS_LABELS = {
    "pending": ("Pendiente", "idle"),
    "in_progress": ("En progreso", "working"),
    "pending_review": ("Pendiente de revisión", "wait"),
    "completed": ("Completada", "done"),
    "failed": ("Fallida", "error"),
    "canceled": ("Cancelada", "off"),
}

_TRANSFER_STATUS_LABELS = {
    "sent": ("Enviado", "done"),
    "copied": ("Copiado", "wait"),
    "error": ("Error", "error"),
}


class HistoryRepository:
    """Consultas del historial unificado de actividad (fase 6C.1)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.conn

    def _get_prompt_transfers(self, project_id: int | None) -> list[HistoryEvent]:
        query = """
            SELECT pt.id, pt.project_id, p.name AS project_name, pt.agent, pt.prompt, pt.status, pt.detail, pt.created_at
            FROM prompt_transfers pt
            JOIN projects p ON pt.project_id = p.id
        """
        params: list[object] = []
        if project_id is not None:
            query += " WHERE pt.project_id = ?"
            params.append(project_id)
        query += " ORDER BY pt.id DESC LIMIT 300"
        rows = self._conn.execute(query, params).fetchall()

        events: list[HistoryEvent] = []
        for r in rows:
            agent_raw = r["agent"]
            agent_lbl = AgentKind(agent_raw).label if agent_raw in AgentKind._value2member_map_ else agent_raw
            first_line = r["prompt"].strip().split("\n")[0][:120]
            st_raw = r["status"]
            st_lbl, st_stl = _TRANSFER_STATUS_LABELS.get(st_raw, (st_raw, "dim-label"))
            events.append(
                HistoryEvent(
                    id=f"pt:{r['id']}",
                    kind=HistoryEventKind.PROMPT_TRANSFER,
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                    title=f"Transferencia de prompt ({agent_lbl})",
                    description=first_line,
                    created_at=_parse_dt_safe(r["created_at"]),
                    agent_kind=agent_raw,
                    status=st_raw,
                    status_label=st_lbl,
                    status_style=st_stl,
                    target_type="workspace",
                    target_id=r["project_id"],
                )
            )
        return events

    def _get_tasks(self, project_id: int | None) -> list[HistoryEvent]:
        query = """
            SELECT t.id, t.project_id, p.name AS project_name, t.title, t.agent, t.created_at
            FROM tasks t
            JOIN projects p ON t.project_id = p.id
        """
        params: list[object] = []
        if project_id is not None:
            query += " WHERE t.project_id = ?"
            params.append(project_id)
        query += " ORDER BY t.id DESC LIMIT 300"
        rows = self._conn.execute(query, params).fetchall()

        events: list[HistoryEvent] = []
        for r in rows:
            agent_raw = r["agent"]
            agent_lbl = AgentKind(agent_raw).label if agent_raw in AgentKind._value2member_map_ else agent_raw
            desc = f"Tarea para {agent_lbl}"
            events.append(
                HistoryEvent(
                    id=f"task:{r['id']}",
                    kind=HistoryEventKind.TASK,
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                    title=f"Tarea creada: {r['title']}",
                    description=desc,
                    created_at=_parse_dt_safe(r["created_at"]),
                    agent_kind=agent_raw,
                    status="created",
                    status_label="Creada",
                    status_style="idle",
                    target_type="tasks",
                    target_id=r["id"],
                )
            )
        return events

    def _get_task_history(self, project_id: int | None) -> list[HistoryEvent]:
        query = """
            SELECT th.id, th.task_id, t.project_id, p.name AS project_name, t.title, t.agent, th.from_status, th.to_status, th.note, th.created_at
            FROM task_history th
            JOIN tasks t ON th.task_id = t.id
            JOIN projects p ON t.project_id = p.id
            WHERE th.from_status IS NOT NULL AND th.from_status != ''
        """
        params: list[object] = []
        if project_id is not None:
            query += " AND t.project_id = ?"
            params.append(project_id)
        query += " ORDER BY th.id DESC LIMIT 300"
        rows = self._conn.execute(query, params).fetchall()

        events: list[HistoryEvent] = []
        for r in rows:
            agent_raw = r["agent"]
            from_s = r["from_status"]
            to_s = r["to_status"]
            from_lbl = _TASK_STATUS_LABELS.get(from_s, (from_s, "dim-label"))[0]
            to_lbl, to_stl = _TASK_STATUS_LABELS.get(to_s, (to_s, "dim-label"))
            desc = f"{from_lbl} → {to_lbl}"
            if r["note"]:
                desc += f" · {r['note'].strip()[:100]}"
            events.append(
                HistoryEvent(
                    id=f"th:{r['id']}",
                    kind=HistoryEventKind.TASK,
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                    title=f"Actualización de tarea: {r['title']}",
                    description=desc,
                    created_at=_parse_dt_safe(r["created_at"]),
                    agent_kind=agent_raw,
                    status=to_s,
                    status_label=to_lbl,
                    status_style=to_stl,
                    target_type="tasks",
                    target_id=r["task_id"],
                )
            )
        return events

    def _get_brain_proposals(self, project_id: int | None) -> list[HistoryEvent]:
        query = """
            SELECT bp.id, bp.project_id, p.name AS project_name, bp.agent_kind, bp.operation, bp.relpath, bp.title, bp.category, bp.status, bp.created_at, bp.reviewed_at, bp.review_note
            FROM brain_proposals bp
            JOIN projects p ON bp.project_id = p.id
        """
        params: list[object] = []
        if project_id is not None:
            query += " WHERE bp.project_id = ?"
            params.append(project_id)
        query += " ORDER BY bp.created_at DESC LIMIT 300"
        rows = self._conn.execute(query, params).fetchall()

        events: list[HistoryEvent] = []
        for r in rows:
            st_raw = r["status"]
            st_lbl, st_stl = _PROPOSAL_STATUS_INFO.get(st_raw, (st_raw, "dim-label"))
            op_lbl = _PROPOSAL_OP_LABELS.get(r["operation"], r["operation"])
            agent_raw = r["agent_kind"] or "agent"

            # Evento de creación
            events.append(
                HistoryEvent(
                    id=f"bp_create:{r['id']}",
                    kind=HistoryEventKind.BRAIN_PROPOSAL,
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                    title=f"Propuesta de memoria: {r['title']}",
                    description=f"{op_lbl} en {r['relpath']} ({r['category']})",
                    created_at=_parse_dt_safe(r["created_at"]),
                    agent_kind=agent_raw,
                    status="pending",
                    status_label="Propuesta",
                    status_style="wait",
                    target_type="memory",
                    target_id=r["id"],
                )
            )

            # Evento de revisión si ya fue resuelta
            if r["reviewed_at"]:
                rev_desc = f"Revisión de {r['relpath']}"
                if r["review_note"]:
                    rev_desc += f" · Nota: {r['review_note'].strip()[:100]}"
                events.append(
                    HistoryEvent(
                        id=f"bp_rev:{r['id']}",
                        kind=HistoryEventKind.BRAIN_PROPOSAL,
                        project_id=r["project_id"],
                        project_name=r["project_name"],
                        title=f"Propuesta {st_lbl.lower()}: {r['title']}",
                        description=rev_desc,
                        created_at=_parse_dt_safe(r["reviewed_at"]),
                        agent_kind=agent_raw,
                        status=st_raw,
                        status_label=st_lbl,
                        status_style=st_stl,
                        target_type="memory",
                        target_id=r["id"],
                    )
                )
        return events

    def _get_chat_links(self, project_id: int | None) -> list[HistoryEvent]:
        query = """
            SELECT cl.id, cl.project_id, p.name AS project_name, cl.url, cl.link_kind, cl.title, cl.created_at
            FROM chat_links cl
            JOIN projects p ON cl.project_id = p.id
        """
        params: list[object] = []
        if project_id is not None:
            query += " WHERE cl.project_id = ?"
            params.append(project_id)
        query += " ORDER BY cl.id DESC LIMIT 300"
        rows = self._conn.execute(query, params).fetchall()

        events: list[HistoryEvent] = []
        for r in rows:
            kind_lbl = "compartido" if r["link_kind"] == "shared" else "privado"
            events.append(
                HistoryEvent(
                    id=f"chat:{r['id']}",
                    kind=HistoryEventKind.CHAT_LINK,
                    project_id=r["project_id"],
                    project_name=r["project_name"],
                    title=f"Conversación vinculada: {r['title']}",
                    description=f"Enlace {kind_lbl}: {r['url']}",
                    created_at=_parse_dt_safe(r["created_at"]),
                    agent_kind="gpt",
                    status="linked",
                    status_label="Vinculada",
                    status_style="dim-label",
                    target_type="workspace",
                    target_id=r["project_id"],
                )
            )
        return events

    def list_events(
        self,
        project_id: int | None = None,
        kind: HistoryEventKind | None = None,
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[HistoryEvent], bool]:
        """Devuelve una tupla (eventos, hay_mas) ordenados cronológicamente."""
        events: list[HistoryEvent] = []

        if kind is None or kind == HistoryEventKind.PROMPT_TRANSFER:
            events.extend(self._get_prompt_transfers(project_id))
        if kind is None or kind == HistoryEventKind.TASK:
            events.extend(self._get_tasks(project_id))
            events.extend(self._get_task_history(project_id))
        if kind is None or kind == HistoryEventKind.BRAIN_PROPOSAL:
            events.extend(self._get_brain_proposals(project_id))
        if kind is None or kind == HistoryEventKind.CHAT_LINK:
            events.extend(self._get_chat_links(project_id))

        if search.strip():
            term = search.strip().lower()
            events = [
                e
                for e in events
                if term in e.title.lower()
                or term in e.description.lower()
                or term in e.project_name.lower()
                or (e.agent_kind and term in e.agent_kind.lower())
                or (e.status_label and term in e.status_label.lower())
            ]

        # Ordenar cronológicamente (más reciente primero; actualizaciones desempatan antes que creaciones simultáneas)
        events.sort(
            key=lambda e: (e.created_at, 1 if (e.id.startswith("th:") or e.id.startswith("bp_rev:")) else 0),
            reverse=True,
        )

        sliced = events[offset : offset + limit + 1]
        has_more = len(sliced) > limit
        return sliced[:limit], has_more
