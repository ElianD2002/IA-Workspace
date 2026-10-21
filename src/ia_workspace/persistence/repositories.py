"""Repositorios: única capa que conoce el SQL."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ia_workspace.domain.models import AgentKind, NewProject, Project, WorkspaceState
from ia_workspace.persistence.database import Database


class DuplicateProjectError(Exception):
    """Ya existe un proyecto registrado con la misma carpeta."""


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

    def remove(self, project_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


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
