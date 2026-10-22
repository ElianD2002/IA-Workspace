"""Pruebas del puente de contexto bajo demanda (fase 3B).

Comprueba la resolución de enlaces de memoria para Claude Code, OpenAI Codex y
Google Antigravity, la brevedad de la referencia (< 600 caracteres), la no
modificación de configuraciones ni ejecuciones reales de IA, y el aislamiento de proyectos.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from helpers import temp_dir

from ia_workspace.agents import tools
from ia_workspace.brain.bridge import compose_prompt_with_reference, resolve_brain_link
from ia_workspace.brain.model import Scope
from ia_workspace.config import AppPaths
from ia_workspace.context import build_brain
from ia_workspace.domain.models import AgentKind, NewProject, Project
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.sessions.models import BrainLinkStatus


class BrainBridgeLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.data = Path(self.tmp.__enter__())
        self.paths = AppPaths(data_dir=self.data)
        self.db = Database(self.paths.database)
        self.brain = build_brain(self.db, self.paths)
        self.projects_repo = ProjectRepository(self.db)
        self.project_dir = self.data / "proyectos" / "alfa"
        self.project_dir.mkdir(parents=True)
        self.project = self.projects_repo.add(
            NewProject(name="Alfa", path=str(self.project_dir), agents=(AgentKind.CLAUDE_CODE, AgentKind.CODEX, AgentKind.ANTIGRAVITY))
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.__exit__(None, None, None)

    def test_project_without_memory_is_unavailable(self) -> None:
        link = resolve_brain_link(self.project, AgentKind.CLAUDE_CODE, self.brain)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.UNAVAILABLE)
        self.assertIn("todavía no tiene memoria", link.detail)
        self.assertIsNone(link.launch_reference)

    def test_none_brain_is_unavailable(self) -> None:
        link = resolve_brain_link(self.project, AgentKind.CLAUDE_CODE, None)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.UNAVAILABLE)
        self.assertIsNone(link.launch_reference)

    def test_shell_has_no_brain_link(self) -> None:
        link = resolve_brain_link(self.project, AgentKind.SHELL, self.brain)
        self.assertIsNone(link)

    def test_project_with_memory_resolves_claude(self) -> None:
        # Inicializamos la memoria del proyecto
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")
        link = resolve_brain_link(self.project, AgentKind.CLAUDE_CODE, self.brain)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.APPLIED)
        self.assertTrue(link.via_launch)
        self.assertIsNotNone(link.launch_reference)
        self.assertLess(len(link.launch_reference), 850)
        self.assertIn("--append-system-prompt", link.detail)

        # Verificamos argumentos generados
        argv = tools.build_launch_argv(AgentKind.CLAUDE_CODE, which=lambda _: "/fake/claude", context=link.launch_reference)
        self.assertEqual(argv[:2], ["/fake/claude", "--append-system-prompt"])
        self.assertEqual(argv[2], link.launch_reference)

    def test_project_with_memory_resolves_codex(self) -> None:
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")
        link = resolve_brain_link(self.project, AgentKind.CODEX, self.brain)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.APPLIED)
        self.assertTrue(link.via_launch)
        self.assertIsNotNone(link.launch_reference)
        self.assertIn("-c developer_instructions", link.detail)

        argv = tools.build_launch_argv(AgentKind.CODEX, which=lambda _: "/fake/codex", context=link.launch_reference)
        self.assertIn("-c", argv)
        self.assertTrue(any(a.startswith("developer_instructions=") for a in argv))

    def test_codex_with_custom_instructions_offers_copy(self) -> None:
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")
        codex_cfg = self.project_dir / ".codex" / "config.toml"
        codex_cfg.parent.mkdir()
        codex_cfg.write_text('developer_instructions = "mis reglas previas"\n')

        link = resolve_brain_link(self.project, AgentKind.CODEX, self.brain)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.COPY)
        self.assertFalse(link.via_launch)
        self.assertIsNone(link.launch_reference)
        self.assertIn("ya define developer_instructions", link.detail)
        self.assertIsNotNone(link.reference)

    def test_antigravity_resolution_and_composition(self) -> None:
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")

        # Sin transferencia de prompt: no admite argumentos de inicio -> estado COPY
        link = resolve_brain_link(self.project, AgentKind.ANTIGRAVITY, self.brain)
        self.assertIsNotNone(link)
        self.assertIs(link.status, BrainLinkStatus.COPY)
        self.assertFalse(link.via_launch)
        self.assertIsNone(link.launch_reference)
        self.assertIn("no admite instrucciones de sistema por CLI", link.detail)

        # Con incorporación al primer prompt transferido
        link_into = resolve_brain_link(self.project, AgentKind.ANTIGRAVITY, self.brain, into_prompt=True)
        self.assertIsNotNone(link_into)
        self.assertIs(link_into.status, BrainLinkStatus.APPLIED)
        self.assertFalse(link_into.via_launch)
        self.assertIsNone(link_into.launch_reference)

        prompt = "Crea un componente"
        composed = compose_prompt_with_reference(prompt, link_into.reference)
        self.assertTrue(composed.startswith("Memoria de «Alfa»"))
        self.assertIn("---", composed)
        self.assertTrue(composed.endswith(prompt))

    def test_codex_profiles_and_variants_offer_copy(self) -> None:
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")
        codex_home = self.data / "codex_home"
        codex_home.mkdir()

        # 1. Perfil específico via archivo *.config.toml
        prof = codex_home / "custom.config.toml"
        prof.write_text('developer_instructions = "regla en perfil"\\n')
        env = {"CODEX_HOME": str(codex_home)}

        self.assertTrue(tools.codex_defines_developer_instructions(str(self.project_dir), env=env))
        prof.unlink()

        # 2. Archivo codex.toml en raíz del proyecto
        proj_codex = self.project_dir / "codex.toml"
        proj_codex.write_text('developer_instructions = "regla local"\\n')
        self.assertTrue(tools.codex_defines_developer_instructions(str(self.project_dir), env=env))
        proj_codex.unlink()

        # 3. Archivo con sintaxis corrupta -> fallback seguro a True (COPY)
        proj_bad = self.project_dir / ".codex.toml"
        proj_bad.write_text("sintaxis = invalida {[\n")
        self.assertTrue(tools.codex_defines_developer_instructions(str(self.project_dir), env=env))
        proj_bad.unlink()

    def test_brain_failure_degrades_gracefully_to_unavailable(self) -> None:
        self.brain.create(Scope.PROJECT, self.brain.project_key(self.project), "reglas", "Base", "Contenido...")
        with mock.patch.object(self.brain, "agent_brief", side_effect=RuntimeError("Fallo inesperado del Cerebro")):
            link = resolve_brain_link(self.project, AgentKind.CLAUDE_CODE, self.brain)
            self.assertIsNotNone(link)
            self.assertIs(link.status, BrainLinkStatus.UNAVAILABLE)
            self.assertIn("No se pudo generar la referencia", link.detail)
            self.assertIsNone(link.launch_reference)
