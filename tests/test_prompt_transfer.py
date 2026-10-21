"""Transferencia de prompts a agentes: argv oficial, persistencia y migración (sin GTK).

Ninguna prueba ejecuta Claude Code, Codex ni Antigravity reales.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from helpers import temp_dir

from ia_workspace.agents import tools
from ia_workspace.domain.models import AgentKind, NewProject
from ia_workspace.persistence.database import MIGRATIONS, SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import ProjectRepository, PromptTransferRepository
from ia_workspace.services.prompt_transfers import PromptTransferService

FAKE_PATHS = {"claude": "/fake/bin/claude", "codex": "/fake/bin/codex", "agy": "/fake/bin/agy"}


def which_with(*names: str):
    return lambda name: FAKE_PATHS.get(name) if name in names else None


WHICH_ALL = which_with("claude", "codex", "agy")


class DirectPromptArgvTests(unittest.TestCase):
    """Mecanismo oficial de cada CLI para un prompt inicial (ver agents/tools.py)."""

    def test_claude_and_codex_use_positional_with_double_dash(self) -> None:
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CLAUDE_CODE, prompt="hola agente", which=WHICH_ALL),
            ["/fake/bin/claude", "--", "hola agente"],
        )
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CODEX, prompt="hola agente", which=WHICH_ALL),
            ["/fake/bin/codex", "--no-daemon", "--", "hola agente"],
        )

    def test_antigravity_uses_prompt_interactive_flag(self) -> None:
        self.assertEqual(
            tools.build_launch_argv(AgentKind.ANTIGRAVITY, prompt="hola agente", which=WHICH_ALL),
            ["/fake/bin/agy", "--prompt-interactive", "hola agente"],
        )

    def test_without_prompt_behaves_as_before(self) -> None:
        self.assertEqual(tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=WHICH_ALL), ["/fake/bin/claude"])

    def test_prompt_is_a_single_argv_element_never_shell_concatenated(self) -> None:
        tricky = "línea 1\nlínea 2 con \"comillas\" y \\barras\\\n-- parece una opción\n🙂"
        argv = tools.build_launch_argv(AgentKind.CLAUDE_CODE, prompt=tricky, which=WHICH_ALL)
        self.assertEqual(argv[-1], tricky)  # intacto: ni dividido ni re-escapado
        self.assertEqual(len(argv), 3)  # [ejecutable, "--", prompt] — nunca más tokens

    def test_leading_dash_prompt_cannot_be_mistaken_for_a_flag(self) -> None:
        argv = tools.build_launch_argv(AgentKind.CODEX, prompt="--dangerously-looking but not a flag",
                                       which=WHICH_ALL)
        self.assertEqual(argv, ["/fake/bin/codex", "--no-daemon", "--", "--dangerously-looking but not a flag"])

    def test_prompt_mentioning_dangerously_is_not_rejected(self) -> None:
        # Antes, _check_safe miraba el texto del prompt como si fuera una opción.
        prompt = "Resume los cambios, pero no borres nada dangerously rápido."
        argv = tools.build_launch_argv(AgentKind.CLAUDE_CODE, prompt=prompt, which=WHICH_ALL)
        self.assertEqual(argv, ["/fake/bin/claude", "--", prompt])

    def test_still_rejects_genuine_unsafe_flags(self) -> None:
        with self.assertRaises(tools.UnsafeArgumentError):
            tools.build_launch_argv(AgentKind.CLAUDE_CODE, ("--dangerously-skip-permissions",),
                                    prompt="hola", which=WHICH_ALL)

    def test_tool_without_direct_prompt_raises_clear_error(self) -> None:
        bare = tools.AgentTool(kind=AgentKind.ANTIGRAVITY, executables=("agy",))
        with mock.patch.dict(tools.TOOLS, {AgentKind.ANTIGRAVITY: bare}):
            self.assertFalse(tools.TOOLS[AgentKind.ANTIGRAVITY].supports_direct_prompt)
            with self.assertRaises(ValueError) as caught:
                tools.build_launch_argv(AgentKind.ANTIGRAVITY, prompt="hola", which=WHICH_ALL)
            self.assertIn("no admite transferencia directa", str(caught.exception))
            # Sin prompt, sigue funcionando con normalidad.
            self.assertEqual(tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=WHICH_ALL), ["/fake/bin/agy"])

    def test_supports_direct_prompt_for_current_agents(self) -> None:
        for kind in (AgentKind.CLAUDE_CODE, AgentKind.CODEX, AgentKind.ANTIGRAVITY):
            self.assertTrue(tools.TOOLS[kind].supports_direct_prompt, kind)

    def test_missing_tool_still_reports_not_installed(self) -> None:
        with self.assertRaises(tools.ToolNotInstalledError):
            tools.build_launch_argv(AgentKind.CODEX, prompt="hola", which=which_with())


class PromptTransferPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        projects = ProjectRepository(self.db)
        self.a = projects.add(NewProject("A", "/a"))
        self.b = projects.add(NewProject("B", "/b"))
        self.service = PromptTransferService(PromptTransferRepository(self.db))
        self.events: list[int] = []
        self.service.subscribe(self.events.append)

    def test_record_round_trips_long_multiline_special_prompt(self) -> None:
        prompt = "Refactoriza `main.py`:\n- usa \"comillas\"\n- acepta rutas con espacios\n- 日本語 🚀\n" + ("x" * 5000)
        transfer = self.service.record(self.a.id, AgentKind.CODEX, prompt, "s1", "direct", "sent")
        self.assertEqual(transfer.prompt, prompt)
        self.assertEqual(self.service.list(self.a.id)[0].prompt, prompt)
        self.assertEqual(len(self.service.list(self.a.id)[0].prompt), len(prompt))

    def test_fields_round_trip_and_defaults(self) -> None:
        transfer = self.service.record(self.a.id, AgentKind.CLAUDE_CODE, "hola", "s1", "direct", "sent")
        self.assertEqual(transfer.agent, AgentKind.CLAUDE_CODE)
        self.assertEqual(transfer.session_id, "s1")
        self.assertEqual(transfer.method, "direct")
        self.assertEqual(transfer.status, "sent")
        self.assertEqual(transfer.detail, "")
        self.assertIsNotNone(transfer.created_at)
        manual = self.service.record(self.a.id, AgentKind.ANTIGRAVITY, "hola", None, "manual", "copied")
        self.assertIsNone(manual.session_id)

    def test_projects_are_isolated(self) -> None:
        in_a = self.service.record(self.a.id, AgentKind.CLAUDE_CODE, "para A", "s1", "direct", "sent")
        in_b = self.service.record(self.b.id, AgentKind.CODEX, "para B", "s2", "direct", "sent")
        self.assertEqual([t.id for t in self.service.list(self.a.id)], [in_a.id])
        self.assertEqual([t.id for t in self.service.list(self.b.id)], [in_b.id])
        self.assertEqual(self.events, [self.a.id, self.b.id])  # solo se avisa al proyecto afectado

    def test_most_recent_first(self) -> None:
        first = self.service.record(self.a.id, AgentKind.CLAUDE_CODE, "primero", None, "direct", "sent")
        second = self.service.record(self.a.id, AgentKind.CODEX, "segundo", None, "direct", "sent")
        self.assertEqual([t.id for t in self.service.list(self.a.id)], [second.id, first.id])

    def test_error_status_keeps_detail(self) -> None:
        transfer = self.service.record(self.a.id, AgentKind.CODEX, "hola", None, "direct", "error",
                                       "Codex no está instalado")
        self.assertEqual(transfer.status, "error")
        self.assertEqual(transfer.detail, "Codex no está instalado")

    def test_schema_rejects_invalid_values(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO prompt_transfers (project_id, agent, prompt, method, status, created_at) "
                "VALUES (?, 'codex', '', 'direct', 'sent', '2026-01-01T00:00:00+00:00')",
                (self.a.id,),
            )  # prompt vacío
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO prompt_transfers (project_id, agent, prompt, method, status, created_at) "
                "VALUES (?, 'codex', 'x', 'volando', 'sent', '2026-01-01T00:00:00+00:00')",
                (self.a.id,),
            )  # method inválido
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO prompt_transfers (project_id, agent, prompt, method, status, created_at) "
                "VALUES (?, 'codex', 'x', 'direct', 'exitoso', '2026-01-01T00:00:00+00:00')",
                (self.a.id,),
            )  # status inválido: nunca "exitoso"/"completado" sin verificación

    def test_cascade_delete_with_project(self) -> None:
        self.service.record(self.a.id, AgentKind.CLAUDE_CODE, "hola", None, "direct", "sent")
        ProjectRepository(self.db).remove(self.a.id)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM prompt_transfers").fetchone()[0], 0)


class MigrationV3Tests(unittest.TestCase):
    def test_v2_database_upgrades_to_v3_without_losing_data(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "v2.db"
            conn = sqlite3.connect(path)
            for migration in MIGRATIONS[:2]:
                for statement in migration:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 2")
            conn.execute(
                "INSERT INTO projects (name, path, color_index, agents, created_at) "
                "VALUES ('Existente', '/existente', 0, '[]', '2026-10-01T10:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO chat_links (project_id, url, link_kind, conversation_id, title, created_at) "
                "VALUES (1, 'https://chatgpt.com/c/abcdefgh12345678', 'private', 'abcdefgh12345678', "
                "'Conversación previa', '2026-10-02T10:00:00+00:00')"
            )
            conn.commit()
            conn.close()

            db = Database(path)
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 3)
            self.assertEqual([p[0] for p in db.conn.execute("SELECT name FROM projects")], ["Existente"])
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM chat_links").fetchone()[0], 1)
            columns = [c[1] for c in db.conn.execute("PRAGMA table_info(prompt_transfers)")]
            self.assertEqual(
                columns, ["id", "project_id", "agent", "prompt", "session_id", "method", "status", "detail",
                         "created_at"],
            )
            # La base migrada admite transferencias de inmediato, sin tocar los datos previos.
            PromptTransferService(PromptTransferRepository(db)).record(1, AgentKind.CLAUDE_CODE, "hola", None,
                                                                        "direct", "sent")
            self.assertEqual(db.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.close()

    def test_no_credential_columns(self) -> None:
        db = Database(":memory:")
        forbidden = ("token", "secret", "password", "api_key", "apikey", "credential", "cookie")
        columns = [c[1].lower() for c in db.conn.execute("PRAGMA table_info(prompt_transfers)")]
        for column in columns:
            self.assertFalse(any(f in column for f in forbidden), column)


if __name__ == "__main__":
    unittest.main()
