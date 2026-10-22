"""Pruebas unitarias para la biblioteca de plantillas de prompts (fase 6B.2).

Verifica persistencia en SQLite, migración 10, reglas de aislamiento por proyecto,
visibilidad global, búsqueda, categorización y manejo de variables parametrizables.
"""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from helpers import temp_dir

from ia_workspace.domain.models import (
    AgentKind,
    NewProject,
    PromptTemplate,
    PromptTemplateScope,
    extract_template_variables,
    render_template_content,
)
from ia_workspace.persistence.database import Database, SCHEMA_VERSION
from ia_workspace.persistence.repositories import (
    ProjectRepository,
    PromptTemplateRepository,
)
from ia_workspace.services.prompt_templates import PromptTemplateService


class PromptTemplateLogicTests(unittest.TestCase):
    def test_variable_extraction_and_render(self) -> None:
        content = "Hola {{nombre}}, por favor revisa el módulo {{modulo}} con las restricciones: {{restricciones}} y {{modulo}} de nuevo."
        vars_found = extract_template_variables(content)
        self.assertEqual(vars_found, ["nombre", "modulo", "restricciones"])

        # Sustitución segura
        rendered = render_template_content(
            content,
            {"nombre": "Elian", "modulo": "auth", "restricciones": "sin dependencias"},
        )
        self.assertEqual(
            rendered,
            "Hola Elian, por favor revisa el módulo auth con las restricciones: sin dependencias y auth de nuevo.",
        )

        # Sustitución parcial deja intactas las variables no suministradas
        partial = render_template_content(content, {"nombre": "Elian"})
        self.assertIn("{{modulo}}", partial)
        self.assertIn("{{restricciones}}", partial)
        self.assertTrue(partial.startswith("Hola Elian,"))

    def test_empty_content_variables(self) -> None:
        self.assertEqual(extract_template_variables(""), [])
        self.assertEqual(extract_template_variables("Texto sin variables"), [])
        self.assertEqual(render_template_content("Texto plano", {}), "Texto plano")


class PromptTemplatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.projects_repo = ProjectRepository(self.db)
        self.repo = PromptTemplateRepository(self.db)
        self.service = PromptTemplateService(self.repo)

        self.p1 = self.projects_repo.add(NewProject("Proyecto A", "/tmp/proy_a", (AgentKind.CLAUDE_CODE,)))
        self.p2 = self.projects_repo.add(NewProject("Proyecto B", "/tmp/proy_b", (AgentKind.CODEX,)))

    def tearDown(self) -> None:
        self.service.close()
        self.db.close()

    def test_schema_version_is_10(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 10)
        self.assertEqual(self.db.version, 10)

    def test_create_global_template(self) -> None:
        t = self.service.create(
            name="Refactorización Limpia",
            description="Aplica clean code",
            category="Refactorización",
            scope=PromptTemplateScope.GLOBAL,
            content="Refactoriza el módulo {{modulo}} manteniendo los tests verdes.",
        )
        self.assertIsNotNone(t.id)
        self.assertEqual(t.name, "Refactorización Limpia")
        self.assertEqual(t.scope, PromptTemplateScope.GLOBAL)
        self.assertIsNone(t.project_id)
        self.assertEqual(t.variables(), ["modulo"])

        # Visible en p1 y p2
        list_p1 = self.service.list_for_project(self.p1.id)
        self.assertEqual(len(list_p1), 1)
        self.assertEqual(list_p1[0].id, t.id)

        list_p2 = self.service.list_for_project(self.p2.id)
        self.assertEqual(len(list_p2), 1)
        self.assertEqual(list_p2[0].id, t.id)

    def test_create_project_template_isolation(self) -> None:
        # Plantilla exclusiva para Proyecto A
        t_a = self.service.create(
            name="Deploy Local A",
            description="Despliegue local de A",
            category="DevOps",
            scope=PromptTemplateScope.PROJECT,
            project_id=self.p1.id,
            content="Ejecuta ./scripts/deploy_a.sh con el tag {{tag}}.",
        )
        self.assertEqual(t_a.project_id, self.p1.id)
        self.assertEqual(t_a.scope, PromptTemplateScope.PROJECT)

        # Plantilla exclusiva para Proyecto B
        t_b = self.service.create(
            name="Migración DB B",
            category="Base de datos",
            scope=PromptTemplateScope.PROJECT,
            project_id=self.p2.id,
            content="Ejecuta migraciones para {{db_name}}.",
        )

        # Proyecto A solo ve la suya
        p1_templates = self.service.list_for_project(self.p1.id)
        self.assertEqual([t.id for t in p1_templates], [t_a.id])

        # Proyecto B solo ve la suya
        p2_templates = self.service.list_for_project(self.p2.id)
        self.assertEqual([t.id for t in p2_templates], [t_b.id])

        # Una plantilla global agregada se ve en ambos pero las privadas siguen aisladas
        t_global = self.service.create(
            name="Plantilla Común",
            content="Instrucción general",
            scope=PromptTemplateScope.GLOBAL,
        )

        p1_ids = {t.id for t in self.service.list_for_project(self.p1.id)}
        p2_ids = {t.id for t in self.service.list_for_project(self.p2.id)}

        self.assertEqual(p1_ids, {t_a.id, t_global.id})
        self.assertEqual(p2_ids, {t_b.id, t_global.id})
        self.assertNotIn(t_b.id, p1_ids)
        self.assertNotIn(t_a.id, p2_ids)

    def test_search_and_category_filtering(self) -> None:
        self.service.create(
            name="Test Unitario",
            category="Pruebas",
            content="Escribe tests para {{clase}}.",
            scope=PromptTemplateScope.GLOBAL,
        )
        self.service.create(
            name="Documentar API",
            category="Documentación",
            content="Genera docstrings OpenAPI para {{endpoint}}.",
            scope=PromptTemplateScope.GLOBAL,
        )

        # Filtro categoría
        tests_only = self.service.list_for_project(self.p1.id, category="Pruebas")
        self.assertEqual(len(tests_only), 1)
        self.assertEqual(tests_only[0].name, "Test Unitario")

        # Búsqueda por texto
        api_results = self.service.list_for_project(self.p1.id, search="OpenAPI")
        self.assertEqual(len(api_results), 1)
        self.assertEqual(api_results[0].name, "Documentar API")

        # Categorías disponibles
        categories = self.service.categories(self.p1.id)
        self.assertIn("Pruebas", categories)
        self.assertIn("Documentación", categories)

    def test_update_template_preserves_scope(self) -> None:
        t = self.service.create(
            name="Plantilla Inicial",
            content="Contenido v1",
            category="General",
            scope=PromptTemplateScope.PROJECT,
            project_id=self.p1.id,
        )

        updated = self.service.update(
            template_id=t.id,
            name="Plantilla Modificada",
            content="Contenido v2 con {{var}}",
            category="Arquitectura",
            description="Nueva descripción",
            scope=PromptTemplateScope.PROJECT,
            project_id=self.p1.id,
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.name, "Plantilla Modificada")
        self.assertEqual(updated.category, "Arquitectura")
        self.assertEqual(updated.scope, PromptTemplateScope.PROJECT)
        self.assertEqual(updated.project_id, self.p1.id)
        self.assertEqual(updated.variables(), ["var"])

        # Sigue sin ser visible en p2
        self.assertEqual(self.service.list_for_project(self.p2.id), [])

    def test_delete_template(self) -> None:
        t = self.service.create(name="Para borrar", content="Temporal", scope=PromptTemplateScope.GLOBAL)
        self.assertIsNotNone(self.service.get(t.id))

        deleted = self.service.delete(t.id)
        self.assertTrue(deleted)
        self.assertIsNone(self.service.get(t.id))

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            self.service.create(name="", content="Valido")

        with self.assertRaises(ValueError):
            self.service.create(name="Valido", content="   ")

        with self.assertRaises(ValueError):
            # Proyecto sin project_id
            self.service.create(
                name="Invalido",
                content="Valido",
                scope=PromptTemplateScope.PROJECT,
                project_id=None,
            )

    def test_notifications_on_changes(self) -> None:
        from gi.repository import GLib

        notified = []
        unsub = self.service.subscribe(lambda: notified.append(True))

        def pump() -> None:
            while GLib.MainContext.default().iteration(False):
                pass

        t = self.service.create(name="Prueba", content="Algo")
        pump()
        self.assertTrue(len(notified) >= 1)

        self.service.update(t.id, name="Prueba 2", content="Algo 2", category="G", description="", scope=PromptTemplateScope.GLOBAL, project_id=None)
        pump()
        self.assertTrue(len(notified) >= 2)

        self.service.delete(t.id)
        pump()
        self.assertTrue(len(notified) >= 3)

        unsub()
