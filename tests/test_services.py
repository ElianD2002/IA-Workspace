from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import ROOT, temp_dir

from ia_workspace.config import resolve_paths
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind, NewProject, initials_for
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import (
    ProjectRepository,
    SettingsRepository,
    WorkspaceStateRepository,
)
from ia_workspace.services.detection import inspect_folder
from ia_workspace.services.integrations import detect_tools, summary
from ia_workspace.services.projects import ProjectService, ProjectValidationError
from ia_workspace.services.workspace import WorkspaceController


class ConfigTests(unittest.TestCase):
    def test_override(self) -> None:
        self.assertEqual(resolve_paths({"IAW_DATA_DIR": "/x/y"}).data_dir, Path("/x/y"))

    def test_source_checkout_stays_inside_repo(self) -> None:
        paths = resolve_paths({})
        self.assertEqual(paths.data_dir, ROOT / "var")
        self.assertTrue(paths.database.is_relative_to(ROOT))


class ProjectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ProjectService(ProjectRepository(Database(":memory:")))
        self.events = 0
        self.service.subscribe(self._count)

    def _count(self) -> None:
        self.events += 1

    def test_register_validates(self) -> None:
        with temp_dir() as tmp:
            with self.assertRaises(ProjectValidationError):
                self.service.register("  ", tmp, SELECTABLE_AGENTS)
            with self.assertRaises(ProjectValidationError):
                self.service.register("X", "", SELECTABLE_AGENTS)
            with self.assertRaises(ProjectValidationError):
                self.service.register("X", str(Path(tmp) / "no-existe"), SELECTABLE_AGENTS)
            with self.assertRaises(ProjectValidationError):
                self.service.register("x" * 81, tmp, SELECTABLE_AGENTS)
            self.assertEqual(self.events, 0)

    def test_register_normalizes_and_rejects_duplicates(self) -> None:
        with temp_dir() as tmp:
            project = self.service.register("Uno", tmp + "/./", (AgentKind.CLAUDE_CODE,))
            self.assertEqual(project.path, str(Path(tmp).resolve()))
            with self.assertRaises(ProjectValidationError) as caught:
                self.service.register("Dos", tmp, SELECTABLE_AGENTS)
            self.assertIn("«Uno»", str(caught.exception))
            self.assertEqual(self.events, 1)

    def test_register_does_not_touch_folder(self) -> None:
        with temp_dir() as tmp:
            before = sorted(Path(tmp).rglob("*"))
            project = self.service.register("Uno", tmp, SELECTABLE_AGENTS)
            self.service.rename(project.id, "Otro")
            self.service.unregister(project.id)
            self.assertEqual(sorted(Path(tmp).rglob("*")), before)
            self.assertTrue(Path(tmp).is_dir())


class WorkspaceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        projects = ProjectRepository(self.db)
        self.a = projects.add(NewProject("A", "/a"))
        self.b = projects.add(NewProject("B", "/b"))
        self.settings = SettingsRepository(self.db)
        self.ctl = WorkspaceController(WorkspaceStateRepository(self.db), self.settings)

    def test_single_active_project(self) -> None:
        seen: list[int | None] = []
        self.ctl.subscribe(seen.append)
        self.ctl.activate(self.a.id)
        self.ctl.activate(self.a.id)  # sin cambio, sin evento
        self.ctl.activate(self.b.id)
        self.assertEqual(self.ctl.active_project_id, self.b.id)
        self.assertEqual(seen, [self.a.id, self.b.id])
        self.assertEqual(self.ctl.last_project_id, self.b.id)

    def test_states_are_isolated_and_persisted(self) -> None:
        sa = self.ctl.state_for(self.a.id)
        sb = self.ctl.state_for(self.b.id)
        self.assertIsNot(sa, sb)
        sa.prompt_draft = "solo A"
        sa.show_chat = False
        sa.extra["chat_width"] = 410
        self.ctl.mark_dirty(self.a.id)
        self.assertEqual(sb.prompt_draft, "")
        self.assertTrue(sb.show_chat)
        self.ctl.flush()

        fresh = WorkspaceController(WorkspaceStateRepository(self.db), self.settings)
        self.assertEqual(fresh.state_for(self.a.id).prompt_draft, "solo A")
        self.assertEqual(fresh.state_for(self.a.id).extra["chat_width"], 410)
        self.assertEqual(fresh.state_for(self.b.id).prompt_draft, "")

    def test_forget(self) -> None:
        self.ctl.activate(self.a.id)
        self.ctl.forget(self.a.id)
        self.assertIsNone(self.ctl.active_project_id)
        self.assertIsNone(self.ctl.last_project_id)


class DetectionTests(unittest.TestCase):
    def test_empty_and_missing(self) -> None:
        with temp_dir() as tmp:
            info = inspect_folder(tmp)
            self.assertFalse(info.is_git)
            self.assertEqual(info.stack, ())
        self.assertFalse(inspect_folder("/no/existe/seguro").is_git)

    def test_git_stack_and_context(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
            (root / "package.json").write_text(json.dumps({"dependencies": {"next": "1", "react": "1"}}))
            (root / "pyproject.toml").write_text("")
            (root / "AGENTS.md").write_text("")
            info = inspect_folder(root)
            self.assertEqual(info.branch, "main")
            self.assertEqual(info.stack, ("Next.js", "Python"))
            self.assertEqual(info.context_files, ("AGENTS.md",))

    def test_detached_head(self) -> None:
        with temp_dir() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("0123456789abcdef\n")
            self.assertEqual(inspect_folder(root).branch, "01234567")


class IntegrationsTests(unittest.TestCase):
    def test_detect_with_fake_which(self) -> None:
        found = {"claude": "/usr/bin/claude", "agy": "/opt/agy"}
        statuses = detect_tools(found.get)
        by_kind = {s.kind: s for s in statuses}
        self.assertEqual(by_kind[AgentKind.CLAUDE_CODE].executable, "/usr/bin/claude")
        self.assertFalse(by_kind[AgentKind.CODEX].installed)
        self.assertEqual(by_kind[AgentKind.ANTIGRAVITY].executable, "/opt/agy")
        self.assertEqual(summary(statuses), "2 de 3 detectadas")


class ModelTests(unittest.TestCase):
    def test_initials(self) -> None:
        self.assertEqual(initials_for("Sistema Pablo"), "SP")
        self.assertEqual(initials_for("fleet-control"), "FC")
        self.assertEqual(initials_for("ia"), "IA")
        self.assertEqual(initials_for(""), "?")


if __name__ == "__main__":
    unittest.main()
