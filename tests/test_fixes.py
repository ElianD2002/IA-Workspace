"""Corrección unificada: enlaces de ChatGPT, migración, códigos de salida y Codex (sin GTK)."""

from __future__ import annotations

import signal
import sqlite3
import unittest
from pathlib import Path

from helpers import temp_dir

from ia_workspace.agents import tools
from ia_workspace.domain.models import AgentKind, NewProject
from ia_workspace.persistence.database import MIGRATIONS, SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import ChatLinkRepository, ProjectRepository, WorkspaceStateRepository
from ia_workspace.services.chat_links import ChatLinkError, ChatLinkService
from ia_workspace.services.chatgpt_links import InvalidChatLinkError, LinkKind, parse_chat_link
from ia_workspace.sessions.models import SessionState
from ia_workspace.terminal.process import classify_exit

CID = "6718a2b4-1c2d-8000-9f3a-0123456789ab"


class ChatLinkParsingTests(unittest.TestCase):
    def test_private_links(self) -> None:
        for raw in (
            f"https://chatgpt.com/c/{CID}",
            f"https://www.chatgpt.com/c/{CID}/",
            f"https://CHATGPT.com/c/{CID}?model=gpt-5#fin",
            f"  https://chat.openai.com/c/{CID}  ",
        ):
            parsed = parse_chat_link(raw)
            self.assertIs(parsed.kind, LinkKind.PRIVATE, raw)
            self.assertEqual(parsed.url, f"https://chatgpt.com/c/{CID}", raw)
            self.assertEqual(parsed.conversation_id, CID)

    def test_private_link_inside_gpt_or_project(self) -> None:
        parsed = parse_chat_link(f"https://chatgpt.com/g/g-p-67a1b2c3d4-mi-proyecto/c/{CID}")
        self.assertIs(parsed.kind, LinkKind.PRIVATE)
        self.assertEqual(parsed.url, f"https://chatgpt.com/g/g-p-67a1b2c3d4-mi-proyecto/c/{CID}")

    def test_shared_links_are_not_private(self) -> None:
        for raw in (f"https://chatgpt.com/share/{CID}", f"https://chat.openai.com/share/{CID}"):
            parsed = parse_chat_link(raw)
            self.assertIs(parsed.kind, LinkKind.SHARED)
            self.assertEqual(parsed.url, f"https://chatgpt.com/share/{CID}")
        self.assertEqual(LinkKind.SHARED.label, "Enlace compartido")
        self.assertNotEqual(LinkKind.SHARED.default_title, LinkKind.PRIVATE.default_title)

    def test_rejects_external_and_malformed(self) -> None:
        for raw in (
            "",
            "chatgpt.com/c/" + CID,  # sin esquema
            f"http://chatgpt.com/c/{CID}",  # sin TLS
            f"https://chatgpt.com.evil.example/c/{CID}",
            f"https://evilchatgpt.com/c/{CID}",
            f"https://openai.com/c/{CID}",
            f"https://user:pw@chatgpt.com/c/{CID}",
            f"https://chatgpt.com:8443/c/{CID}",
            "https://chatgpt.com/",
            "https://chatgpt.com/gpts",
            "https://chatgpt.com/c/",
            "https://chatgpt.com/c/corto",
            f"https://chatgpt.com/c/{CID}/extra",
            f"https://chatgpt.com/c/{CID} y más",
            "javascript:alert(1)",
            "https://chatgpt.com/c/" + "a" * 3000,
            "https://[::1",  # URL mal formada
        ):
            with self.assertRaises(InvalidChatLinkError, msg=raw):
                parse_chat_link(raw)

    def test_short_form(self) -> None:
        self.assertEqual(parse_chat_link(f"https://chatgpt.com/c/{CID}").short, "chatgpt.com/c/…89ab")


class ChatLinkServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        projects = ProjectRepository(self.db)
        self.a = projects.add(NewProject("A", "/a"))
        self.b = projects.add(NewProject("B", "/b"))
        self.service = ChatLinkService(ChatLinkRepository(self.db))
        self.events: list[int] = []
        self.service.subscribe(self.events.append)

    def test_link_with_default_and_custom_titles(self) -> None:
        private = self.service.link(self.a.id, f"https://chatgpt.com/c/{CID}")
        shared = self.service.link(self.a.id, f"https://chatgpt.com/share/{CID}", "  Plan   de pruebas ")
        self.assertEqual(private.title, "Conversación de ChatGPT")
        self.assertEqual(private.kind, "private")
        self.assertEqual(shared.title, "Plan de pruebas")
        self.assertEqual(shared.kind, "shared")  # mismo id, distinto tipo: no es duplicado
        self.assertEqual([link.id for link in self.service.list(self.a.id)], [shared.id, private.id])
        self.assertEqual(self.events, [self.a.id, self.a.id])

    def test_no_duplicates_within_a_project(self) -> None:
        self.service.link(self.a.id, f"https://chatgpt.com/c/{CID}", "Original")
        for variant in (f"https://chat.openai.com/c/{CID}", f"https://chatgpt.com/c/{CID}?x=1"):
            with self.assertRaises(ChatLinkError) as caught:
                self.service.link(self.a.id, variant)
            self.assertIn("«Original»", str(caught.exception))
        self.assertEqual(len(self.service.list(self.a.id)), 1)

    def test_projects_are_isolated(self) -> None:
        in_a = self.service.link(self.a.id, f"https://chatgpt.com/c/{CID}")
        in_b = self.service.link(self.b.id, f"https://chatgpt.com/c/{CID}")  # permitido en otro proyecto
        self.assertEqual([link.id for link in self.service.list(self.a.id)], [in_a.id])
        self.assertEqual([link.id for link in self.service.list(self.b.id)], [in_b.id])
        self.events.clear()
        self.service.unlink(in_a.id)
        self.assertEqual(self.events, [self.a.id])  # solo se avisa al proyecto afectado
        self.assertEqual([link.id for link in self.service.list(self.b.id)], [in_b.id])

    def test_rename_and_unlink(self) -> None:
        link = self.service.link(self.a.id, f"https://chatgpt.com/c/{CID}")
        self.service.rename(link.id, "Diseño de la API")
        self.assertEqual(self.service.get(link.id).title, "Diseño de la API")
        for bad in ("   ", "x" * 121):
            with self.assertRaises(ChatLinkError):
                self.service.rename(link.id, bad)
        self.service.unlink(link.id)
        self.assertEqual(self.service.list(self.a.id), [])

    def test_invalid_links_are_not_saved(self) -> None:
        with self.assertRaises(ChatLinkError):
            self.service.link(self.a.id, "https://ejemplo.com/c/" + CID)
        self.assertEqual(self.service.list(self.a.id), [])
        self.assertEqual(self.events, [])

    def test_unregistering_a_project_removes_only_its_links(self) -> None:
        self.service.link(self.a.id, f"https://chatgpt.com/c/{CID}")
        kept = self.service.link(self.b.id, f"https://chatgpt.com/c/{CID}")
        ProjectRepository(self.db).remove(self.a.id)
        rows = self.db.conn.execute("SELECT id FROM chat_links").fetchall()
        self.assertEqual([r[0] for r in rows], [kept.id])


class MigrationTests(unittest.TestCase):
    def test_v1_database_upgrades_without_losing_data(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "v1.db"
            conn = sqlite3.connect(path)
            for statement in MIGRATIONS[0]:
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
            conn.execute(
                "INSERT INTO projects (name, path, color_index, agents, created_at) "
                "VALUES ('Existente', '/existente', 3, '[\"codex\"]', '2026-10-01T10:00:00+00:00')"
            )
            conn.execute(
                "INSERT INTO project_ui_state VALUES (1, '{\"show_context\": false, \"prompt_draft\": \"hola\"}', 'x')"
            )
            conn.execute("INSERT INTO settings VALUES ('window.size', '1200x700')")
            conn.commit()
            conn.close()

            db = Database(path)
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 2)
            projects = ProjectRepository(db).list()
            self.assertEqual([(p.name, p.color_index, p.agents) for p in projects],
                             [("Existente", 3, (AgentKind.CODEX,))])
            state = WorkspaceStateRepository(db).load(1)
            self.assertFalse(state.show_context)
            self.assertEqual(state.prompt_draft, "hola")
            self.assertEqual(db.conn.execute("SELECT value FROM settings").fetchone()[0], "1200x700")
            ChatLinkService(ChatLinkRepository(db)).link(1, f"https://chatgpt.com/c/{CID}")
            db.close()

    def test_chat_links_store_no_content_or_credentials(self) -> None:
        db = Database(":memory:")
        columns = [c[1] for c in db.conn.execute("PRAGMA table_info(chat_links)")]
        self.assertEqual(columns, ["id", "project_id", "url", "link_kind", "conversation_id", "title", "created_at"])


class ExitStateTests(unittest.TestCase):
    def test_classification(self) -> None:
        self.assertEqual(classify_exit(0, False)[0], SessionState.DONE)
        self.assertEqual(classify_exit(1, False), (SessionState.FAILED, "Finalizado con error · código 1"))
        state, detail = classify_exit(-signal.SIGKILL, False)
        self.assertEqual(state, SessionState.FAILED)
        self.assertIn("SIGKILL", detail)
        for code in (0, 1, -signal.SIGHUP, -signal.SIGKILL):  # pedido por el usuario
            self.assertEqual(classify_exit(code, True), (SessionState.STOPPED, "Detenido por el usuario"))

    def test_states_and_labels(self) -> None:
        self.assertTrue(SessionState.FAILED.is_terminal)
        self.assertFalse(SessionState.FAILED.is_active)
        labels = {s: s.label for s in (SessionState.DONE, SessionState.FAILED, SessionState.STOPPED)}
        self.assertEqual(labels, {
            SessionState.DONE: "Finalizado correctamente",
            SessionState.FAILED: "Finalizado con error",
            SessionState.STOPPED: "Detenido por el usuario",
        })


class CodexDiagnosisTests(unittest.TestCase):
    def test_codex_runs_without_shared_daemon(self) -> None:
        which = {"codex": "/fake/codex"}.get
        self.assertEqual(tools.build_launch_argv(AgentKind.CODEX, which=which), ["/fake/codex", "--no-daemon"])
        self.assertEqual(tools.build_login_argv(AgentKind.CODEX, which=which), ["/fake/codex", "login"])
        # Claude Code y Antigravity no cambian.
        self.assertEqual(tools.TOOLS[AgentKind.CLAUDE_CODE].interactive_args, ())
        self.assertEqual(tools.TOOLS[AgentKind.ANTIGRAVITY].interactive_args, ())

    def test_install_description(self) -> None:
        note = tools.describe_install(AgentKind.CODEX, "/home/u/.codex/plugins/.plugin-appserver/codex")
        self.assertIn("--no-daemon", note or "")
        self.assertIsNone(tools.describe_install(AgentKind.CODEX, "/usr/local/bin/codex-standalone"))
        self.assertIsNone(tools.describe_install(AgentKind.CLAUDE_CODE, "/x/.plugin-appserver/claude"))
        self.assertIsNone(tools.describe_install(AgentKind.CODEX, None))


if __name__ == "__main__":
    unittest.main()
