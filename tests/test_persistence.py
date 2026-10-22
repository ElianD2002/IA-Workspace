from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from helpers import temp_dir

from ia_workspace.domain.models import AgentKind, AutonomyMode, NewProject, WorkspaceState
from ia_workspace.persistence.database import SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import (
    AgentAutonomyRepository,
    DuplicateProjectError,
    ProjectRepository,
    SettingsRepository,
    WorkspaceStateRepository,
)


class DatabaseTests(unittest.TestCase):
    def test_migrates_to_current_version(self) -> None:
        db = Database(":memory:")
        self.assertEqual(db.version, SCHEMA_VERSION)
        db.migrate()  # idempotente
        self.assertEqual(db.version, SCHEMA_VERSION)

    def test_rejects_newer_schema(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "db.sqlite"
            Database(path).close()
            conn = sqlite3.connect(path)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            conn.close()
            with self.assertRaises(RuntimeError):
                Database(path)

    def test_schema_has_no_credential_columns(self) -> None:
        db = Database(":memory:")
        tables = [r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        forbidden = ("token", "secret", "password", "api_key", "apikey", "credential", "cookie")
        for table in tables:
            for column in db.conn.execute(f"PRAGMA table_info({table})"):
                name = column[1].lower()
                self.assertFalse(any(f in name for f in forbidden), f"{table}.{name}")


class ProjectRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = ProjectRepository(Database(":memory:"))

    def test_add_and_list_persist_fields(self) -> None:
        project = self.repo.add(NewProject("Fleet", "/tmp/fleet", (AgentKind.CODEX,)))
        listed = self.repo.list()
        self.assertEqual([p.id for p in listed], [project.id])
        self.assertEqual(listed[0].agents, (AgentKind.CODEX,))
        self.assertIsNotNone(listed[0].created_at)
        self.assertIsNone(listed[0].last_opened_at)

    def test_colors_rotate(self) -> None:
        a = self.repo.add(NewProject("A", "/a"))
        b = self.repo.add(NewProject("B", "/b"))
        self.assertNotEqual(a.color_index, b.color_index)

    def test_duplicate_path(self) -> None:
        self.repo.add(NewProject("A", "/a"))
        with self.assertRaises(DuplicateProjectError):
            self.repo.add(NewProject("Otro", "/a"))

    def test_rename_touch_remove(self) -> None:
        p = self.repo.add(NewProject("A", "/a"))
        self.repo.rename(p.id, "  Nuevo  ")
        self.repo.touch_opened(p.id)
        got = self.repo.get(p.id)
        assert got is not None
        self.assertEqual(got.name, "Nuevo")
        self.assertIsNotNone(got.last_opened_at)
        self.repo.remove(p.id)
        self.assertIsNone(self.repo.get(p.id))

    def test_survives_reopen(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "db.sqlite"
            db = Database(path)
            ProjectRepository(db).add(NewProject("Persistente", "/p"))
            db.close()
            db2 = Database(path)
            self.assertEqual([p.name for p in ProjectRepository(db2).list()], ["Persistente"])
            db2.close()


class StateRepositoryTests(unittest.TestCase):
    def test_state_roundtrip_and_cascade(self) -> None:
        db = Database(":memory:")
        project = ProjectRepository(db).add(NewProject("A", "/a"))
        states = WorkspaceStateRepository(db)
        self.assertEqual(states.load(project.id), WorkspaceState())
        state = WorkspaceState(show_chat=False, context_tab="memory", prompt_draft="hola", extra={"w": 1})
        states.save(project.id, state)
        self.assertEqual(states.load(project.id), state)
        ProjectRepository(db).remove(project.id)
        row = db.conn.execute("SELECT COUNT(*) FROM project_ui_state").fetchone()[0]
        self.assertEqual(row, 0)

    def test_corrupt_state_falls_back(self) -> None:
        db = Database(":memory:")
        project = ProjectRepository(db).add(NewProject("A", "/a"))
        db.conn.execute(
            "INSERT INTO project_ui_state VALUES (?, ?, ?)", (project.id, "{no json", "2026-01-01")
        )
        self.assertEqual(WorkspaceStateRepository(db).load(project.id), WorkspaceState())

    def test_from_dict_ignores_bad_values(self) -> None:
        state = WorkspaceState.from_dict({"show_chat": "sí", "context_tab": "otra", "prompt_draft": 3})
        self.assertEqual(state, WorkspaceState())

    def test_settings(self) -> None:
        settings = SettingsRepository(Database(":memory:"))
        self.assertEqual(settings.get("k", "d"), "d")
        settings.set("k", "v")
        settings.set("k", "w")
        self.assertEqual(settings.get("k"), "w")
        settings.set("k", None)
        self.assertIsNone(settings.get("k"))


class AgentAutonomyRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.projects = ProjectRepository(self.db)
        self.repo = AgentAutonomyRepository(self.db)
        self.project_a = self.projects.add(NewProject("A", "/path/a", (AgentKind.CLAUDE_CODE, AgentKind.CODEX)))
        self.project_b = self.projects.add(NewProject("B", "/path/b", (AgentKind.CLAUDE_CODE, AgentKind.ANTIGRAVITY)))

    def test_default_mode_is_normal(self) -> None:
        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)
        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.CODEX), AutonomyMode.NORMAL)
        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.ANTIGRAVITY), AutonomyMode.NORMAL)

    def test_set_and_get_mode(self) -> None:
        self.repo.set_mode(self.project_a.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.repo.set_mode(self.project_a.id, AgentKind.CODEX, AutonomyMode.DANGER)

        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.CLAUDE_CODE), AutonomyMode.AUTO)
        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.CODEX), AutonomyMode.DANGER)

    def test_isolation_between_projects(self) -> None:
        self.repo.set_mode(self.project_a.id, AgentKind.CLAUDE_CODE, AutonomyMode.DANGER)
        # Project B remains in default NORMAL mode
        self.assertEqual(self.repo.get_mode(self.project_b.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)

    def test_get_modes_for_project(self) -> None:
        self.repo.set_mode(self.project_a.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.repo.set_mode(self.project_a.id, AgentKind.CODEX, AutonomyMode.DANGER)

        modes = self.repo.get_modes_for_project(self.project_a.id)
        self.assertEqual(modes, {
            AgentKind.CLAUDE_CODE: AutonomyMode.AUTO,
            AgentKind.CODEX: AutonomyMode.DANGER,
        })
        self.assertEqual(self.repo.get_modes_for_project(self.project_b.id), {})

    def test_cascade_delete_with_project(self) -> None:
        self.repo.set_mode(self.project_a.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.projects.remove(self.project_a.id)
        self.assertEqual(self.repo.get_mode(self.project_a.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)
        count = self.db.conn.execute("SELECT COUNT(*) FROM project_agent_modes").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
