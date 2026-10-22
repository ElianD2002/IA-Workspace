"""Pruebas de la Fase 4: Autonomía y modos de autorización de agentes."""

from __future__ import annotations

import unittest
from pathlib import Path

from helpers import temp_dir
from ia_workspace.agents import tools
from ia_workspace.context import AppContext
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind, AutonomyMode, NewProject
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import AgentAutonomyRepository, ProjectRepository
from ia_workspace.sessions.models import SessionSpec

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None

FAKE_PATHS = {
    "claude": "/fake/bin/claude",
    "codex": "/fake/bin/codex",
    "agy": "/fake/bin/agy",
}


def which_mock(*names: str):
    return lambda name: FAKE_PATHS.get(name) if name in names else None


class AutonomyArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.which = which_mock("claude", "codex", "agy")

    def test_9_cases_exact_argv(self) -> None:
        """Verifica los 9 casos requeridos: 3 agentes × 3 modos."""
        # 1. Claude Code - Normal
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=self.which, autonomy_mode=AutonomyMode.NORMAL),
            ["/fake/bin/claude"],
        )
        # 2. Claude Code - Automático
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=self.which, autonomy_mode=AutonomyMode.AUTO),
            ["/fake/bin/claude", "--permission-mode", "auto"],
        )
        # 3. Claude Code - Sin confirmaciones (avanzado)
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=self.which, autonomy_mode=AutonomyMode.DANGER),
            ["/fake/bin/claude", "--dangerously-skip-permissions"],
        )

        # 4. Codex - Normal
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CODEX, which=self.which, autonomy_mode=AutonomyMode.NORMAL),
            ["/fake/bin/codex", "--no-daemon"],
        )
        # 5. Codex - Automático
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CODEX, which=self.which, autonomy_mode=AutonomyMode.AUTO),
            ["/fake/bin/codex", "--no-daemon", "--sandbox", "workspace-write", "--ask-for-approval", "never"],
        )
        # 6. Codex - Sin confirmaciones (avanzado)
        self.assertEqual(
            tools.build_launch_argv(AgentKind.CODEX, which=self.which, autonomy_mode=AutonomyMode.DANGER),
            ["/fake/bin/codex", "--no-daemon", "--dangerously-bypass-approvals-and-sandbox"],
        )

        # 7. Antigravity - Normal
        self.assertEqual(
            tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=self.which, autonomy_mode=AutonomyMode.NORMAL),
            ["/fake/bin/agy"],
        )
        # 8. Antigravity - Automático
        self.assertEqual(
            tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=self.which, autonomy_mode=AutonomyMode.AUTO),
            ["/fake/bin/agy", "--mode=accept-edits"],
        )
        # 9. Antigravity - Sin confirmaciones (avanzado)
        self.assertEqual(
            tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=self.which, autonomy_mode=AutonomyMode.DANGER),
            ["/fake/bin/agy", "--mode=accept-edits", "--dangerously-skip-permissions"],
        )

    def test_antigravity_danger_includes_both_required_arguments(self) -> None:
        """Actividad 1: Confirmar que el lanzamiento avanzado de Antigravity incluya ambos argumentos."""
        argv = tools.build_launch_argv(AgentKind.ANTIGRAVITY, which=self.which, autonomy_mode=AutonomyMode.DANGER)
        self.assertIn("--mode=accept-edits", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv, ["/fake/bin/agy", "--mode=accept-edits", "--dangerously-skip-permissions"])

    def test_unsafe_extra_flags_are_always_rejected(self) -> None:
        """Actividad 7: Comprobar que ningún modo peligroso pueda activarse mediante argumentos arbitrarios."""
        forbidden = [
            "--dangerously-skip-permissions",
            "--dangerously-bypass-approvals-and-sandbox",
            "--sandbox",
            "--permission-mode",
            "--ask-for-approval",
            "--mode",
            "--mode=accept-edits",
        ]
        for kind in SELECTABLE_AGENTS:
            for flag in forbidden:
                with self.assertRaises(tools.UnsafeArgumentError, msg=f"{kind}: {flag}"):
                    tools.build_launch_argv(kind, extra=(flag,), which=self.which, autonomy_mode=AutonomyMode.NORMAL)

    def test_autonomy_combined_with_context_and_prompt(self) -> None:
        """Actividad 6: Preserva compatibilidad con Cerebro Compartido y transferencia de prompts."""
        ref = "Referencia del Cerebro"
        prompt = "Instrucción de prueba"

        # Claude: Auto + Context + Prompt
        argv = tools.build_launch_argv(
            AgentKind.CLAUDE_CODE,
            which=self.which,
            context=ref,
            prompt=prompt,
            autonomy_mode=AutonomyMode.AUTO,
        )
        self.assertEqual(
            argv,
            ["/fake/bin/claude", "--permission-mode", "auto", "--append-system-prompt", ref, "--", prompt],
        )

        # Codex: Auto + Context + Prompt
        argv = tools.build_launch_argv(
            AgentKind.CODEX,
            which=self.which,
            context=ref,
            prompt=prompt,
            autonomy_mode=AutonomyMode.AUTO,
        )
        self.assertEqual(
            argv,
            [
                "/fake/bin/codex",
                "--no-daemon",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "-c",
                'developer_instructions="' + ref + '"',
                "--",
                prompt,
            ],
        )

        # Antigravity: Danger + Prompt
        argv = tools.build_launch_argv(
            AgentKind.ANTIGRAVITY,
            which=self.which,
            prompt=prompt,
            autonomy_mode=AutonomyMode.DANGER,
        )
        self.assertEqual(
            argv,
            ["/fake/bin/agy", "--mode=accept-edits", "--dangerously-skip-permissions", "--prompt-interactive", prompt],
        )


class AutonomyPersistenceTests(unittest.TestCase):
    """Actividad 4: Comprueba la persistencia por proyecto y agente."""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.projects = ProjectRepository(self.db)
        self.repo = AgentAutonomyRepository(self.db)
        self.p1 = self.projects.add(NewProject("P1", "/p1", (AgentKind.CLAUDE_CODE, AgentKind.CODEX)))
        self.p2 = self.projects.add(NewProject("P2", "/p2", (AgentKind.CLAUDE_CODE, AgentKind.ANTIGRAVITY)))

    def test_new_project_defaults_to_normal_for_all_agents(self) -> None:
        for kind in SELECTABLE_AGENTS:
            self.assertEqual(self.repo.get_mode(self.p1.id, kind), AutonomyMode.NORMAL)
            self.assertEqual(self.repo.get_mode(self.p2.id, kind), AutonomyMode.NORMAL)

    def test_set_and_persist_modes(self) -> None:
        self.repo.set_mode(self.p1.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.repo.set_mode(self.p1.id, AgentKind.CODEX, AutonomyMode.DANGER)

        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CLAUDE_CODE), AutonomyMode.AUTO)
        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CODEX), AutonomyMode.DANGER)

        # P2 remains isolated
        self.assertEqual(self.repo.get_mode(self.p2.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)
        self.assertEqual(self.repo.get_mode(self.p2.id, AgentKind.CODEX), AutonomyMode.NORMAL)

    def test_update_mode(self) -> None:
        self.repo.set_mode(self.p1.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CLAUDE_CODE), AutonomyMode.AUTO)

        self.repo.set_mode(self.p1.id, AgentKind.CLAUDE_CODE, AutonomyMode.NORMAL)
        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)

    def test_get_modes_for_project(self) -> None:
        self.repo.set_mode(self.p1.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.repo.set_mode(self.p1.id, AgentKind.CODEX, AutonomyMode.DANGER)

        modes = self.repo.get_modes_for_project(self.p1.id)
        self.assertEqual(modes, {
            AgentKind.CLAUDE_CODE: AutonomyMode.AUTO,
            AgentKind.CODEX: AutonomyMode.DANGER,
        })

    def test_cascade_delete_on_project_deletion(self) -> None:
        self.repo.set_mode(self.p1.id, AgentKind.CLAUDE_CODE, AutonomyMode.AUTO)
        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CLAUDE_CODE), AutonomyMode.AUTO)
        self.projects.remove(self.p1.id)
        # Tras borrado, get_mode devuelve NORMAL y no hay filas huérfanas
        self.assertEqual(self.repo.get_mode(self.p1.id, AgentKind.CLAUDE_CODE), AutonomyMode.NORMAL)
        self.assertEqual(self.repo.get_modes_for_project(self.p1.id), {})


class AutonomySessionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = temp_dir()
        self.db = Database(":memory:")
        self.ctx = AppContext.create(self.db, simulated_agents=True)
        self.p = self.ctx.projects.register("Proyecto Test", self.td.name, ())

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_session_spec_carries_autonomy_mode(self) -> None:
        spec = SessionSpec(
            project_id=self.p.id,
            kind=AgentKind.CLAUDE_CODE,
            cwd="/tmp",
            autonomy_mode=AutonomyMode.AUTO,
        )
        self.assertEqual(spec.autonomy_mode, AutonomyMode.AUTO)

        info = self.ctx.sessions.start(spec)
        self.assertEqual(info.spec.autonomy_mode, AutonomyMode.AUTO)

        spec_normal = SessionSpec(
            project_id=self.p.id,
            kind=AgentKind.CODEX,
            cwd="/tmp",
        )
        self.assertEqual(spec_normal.autonomy_mode, AutonomyMode.NORMAL)

    def test_changing_preferences_does_not_affect_existing_sessions(self) -> None:
        """Actividad 5: Cambiar preferencias no afecta sesiones existentes."""
        # Iniciar sesión con modo NORMAL
        spec1 = SessionSpec(
            project_id=self.p.id,
            kind=AgentKind.CLAUDE_CODE,
            cwd="/tmp",
            autonomy_mode=AutonomyMode.NORMAL,
        )
        info1 = self.ctx.sessions.start(spec1)
        self.assertEqual(info1.spec.autonomy_mode, AutonomyMode.NORMAL)
        self.assertTrue(info1.state.is_active)

        # Cambiar preferencia del proyecto a DANGER
        self.ctx.autonomy.set_mode(self.p.id, AgentKind.CLAUDE_CODE, AutonomyMode.DANGER)

        # La sesión existente permanece intacta con su modo original
        info1_after = self.ctx.sessions.get(info1.id)
        self.assertIsNotNone(info1_after)
        self.assertEqual(info1_after.spec.autonomy_mode, AutonomyMode.NORMAL)
        self.assertTrue(info1_after.state.is_active)

        # Una nueva sesión iniciada posteriormente utiliza el nuevo modo configurado
        new_mode = self.ctx.autonomy.get_mode(self.p.id, AgentKind.CLAUDE_CODE)
        self.assertEqual(new_mode, AutonomyMode.DANGER)
        spec2 = SessionSpec(
            project_id=self.p.id,
            kind=AgentKind.CLAUDE_CODE,
            cwd="/tmp",
            autonomy_mode=new_mode,
        )
        info2 = self.ctx.sessions.start(spec2)
        self.assertEqual(info2.spec.autonomy_mode, AutonomyMode.DANGER)


@unittest.skipUnless(HAS_DISPLAY, "sin display gráfico")
class AutonomyConfirmationDialogUITest(unittest.TestCase):
    """Actividad 3: Prueba específica del diálogo de confirmación y cancelación."""

    def test_advanced_mode_dialog_and_cancellation(self) -> None:
        from ia_workspace.ui.pages.agents import AgentsPage
        from ia_workspace.ui.pages.base import Navigator

        class DummyNav(Navigator):
            def __init__(self, ctx: AppContext) -> None:
                self.ctx = ctx
                self.toasts: list[str] = []

            def sidebar_toggle(self) -> Gtk.Widget:
                return Gtk.Button()

            def toast(self, message: str) -> None:
                self.toasts.append(message)

        with temp_dir() as folder:
            db = Database(":memory:")
            ctx = AppContext.create(db, simulated_agents=True)
            p = ctx.projects.register("Proyecto Diálogo", folder, ())

            nav = DummyNav(ctx)
            page = AgentsPage(nav)
            page.refresh()

            self.assertIsNotNone(page._project)
            self.assertEqual(page._project.id, p.id)

            combo = page._autonomy_rows[AgentKind.ANTIGRAVITY]
            self.assertEqual(combo.get_selected(), 0)  # NORMAL

            # 1. Cambiar a AUTO: sin diálogo
            combo.set_selected(1)
            self.assertIsNone(page._last_dialog)
            self.assertEqual(ctx.autonomy.get_mode(p.id, AgentKind.ANTIGRAVITY), AutonomyMode.AUTO)

            # 2. Cambiar a DANGER: abre diálogo de confirmación
            combo.set_selected(2)
            self.assertIsNotNone(page._last_dialog)
            dialog = page._last_dialog

            # 3. Cancelar: revierte a AUTO y NO guarda DANGER en base de datos
            dialog.emit("response", "cancel")
            self.assertEqual(combo.get_selected(), 1)
            self.assertEqual(ctx.autonomy.get_mode(p.id, AgentKind.ANTIGRAVITY), AutonomyMode.AUTO)

            # 4. Volver a seleccionar DANGER y confirmar: persiste DANGER
            combo.set_selected(2)
            self.assertIsNotNone(page._last_dialog)
            dialog2 = page._last_dialog
            dialog2.emit("response", "confirm")

            self.assertEqual(combo.get_selected(), 2)
            self.assertEqual(ctx.autonomy.get_mode(p.id, AgentKind.ANTIGRAVITY), AutonomyMode.DANGER)
            self.assertTrue(any("sin confirmaciones" in t.lower() for t in nav.toasts))


if __name__ == "__main__":
    unittest.main()
