"""Cerebro compartido (fase 3A): Markdown, índice, búsqueda selectiva y aislamiento (sin GTK)."""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, temp_dir

from ia_workspace.brain import cli
from ia_workspace.brain.model import (
    MAX_DOCUMENT_BYTES,
    BrainError,
    Scope,
    find_secret,
    parse_document,
    serialize_document,
    slugify,
    summarize,
)
from ia_workspace.brain.proposals import (
    BrainConflictError,
    ProposalOperation,
    ProposalStatus,
)
from ia_workspace.brain.service import BrainService
from ia_workspace.brain.store import BrainStore, UnsafePathError
from ia_workspace.config import AppPaths
from ia_workspace.context import build_brain
from ia_workspace.domain.models import NewProject
from ia_workspace.persistence.brain_index import BrainIndex
from ia_workspace.persistence.database import MIGRATIONS, SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.services.projects import ProjectService


class BrainCase(unittest.TestCase):
    """Base con una carpeta de datos temporal, dos proyectos y el servicio real."""

    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.data = Path(self.tmp.name)
        (self.data / "alfa").mkdir()
        (self.data / "beta").mkdir()
        self.db = Database(self.data / "ia-workspace.db")
        repo = ProjectRepository(self.db)
        self.a = repo.add(NewProject("Alfa", str(self.data / "alfa")))
        self.b = repo.add(NewProject("Beta", str(self.data / "beta")))
        self.brain = build_brain(self.db, AppPaths(self.data))
        self.ka = self.brain.project_key(self.a)
        self.kb = self.brain.project_key(self.b)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def add(self, key: str, category: str, title: str, body: str, tags: str = "", module: str = ""):
        return self.brain.create(Scope.PROJECT, key, category, title, body, tags, module)


class ModelTests(unittest.TestCase):
    def test_front_matter_round_trip(self) -> None:
        text = serialize_document("Capas: API y UI", ("api", "ui"), "Contenido\n\n## Detalle\nMás.")
        parsed = parse_document(text, "x")
        self.assertEqual((parsed.title, parsed.tags, parsed.body), ("Capas: API y UI", ("api", "ui"),
                                                                     "Contenido\n\n## Detalle\nMás.\n"))
        self.assertIsNotNone(parsed.updated)
        self.assertEqual(parsed.problems, ())

    def test_header_values_cannot_break_the_front_matter(self) -> None:
        text = serialize_document("Título\n---\ntitle: inyectado", (), "cuerpo")
        self.assertEqual(text.count("\n---\n"), 1)
        self.assertEqual(parse_document(text, "x").title, "Título --- title: inyectado")

    def test_files_without_header_are_still_readable(self) -> None:
        parsed = parse_document("# Notas sueltas\n\nTexto", "Fallback")
        self.assertEqual(parsed.title, "Notas sueltas")
        self.assertTrue(parsed.problems)
        self.assertEqual(parse_document("solo texto", "Fallback").title, "Fallback")

    def test_slugify(self) -> None:
        self.assertEqual(slugify("ADR-006 · Fechas en UTC"), "adr-006-fechas-en-utc")
        self.assertEqual(slugify("Diseño de módulos"), "diseno-de-modulos")
        self.assertEqual(slugify("../../etc/passwd"), "etc-passwd")
        self.assertEqual(slugify("!!!"), "documento")

    def test_summary_drops_markdown_but_keeps_identifiers(self) -> None:
        self.assertEqual(summarize("- Se guardan en **UTC** con `_fecha_`."), "Se guardan en UTC con fecha.")
        self.assertEqual(summarize("> Usa user_id y MAX_RETRIES si a > b"), "Usa user_id y MAX_RETRIES si a > b")

    def test_secret_detection(self) -> None:
        for secret in ("sk-proj-" + "a" * 40, "ghp_" + "A" * 36, "AKIAABCDEFGHIJKLMNOP",
                       "-----BEGIN OPENSSH PRIVATE KEY-----", "password: hunter2222",
                       "Authorization: Bearer " + "x" * 40, "AIza" + "B" * 35,
                       "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"):
            self.assertIsNotNone(find_secret(f"texto {secret} texto"), secret)
        for normal in ("Las contraseñas se guardan con bcrypt", "usa el token CSRF del formulario",
                       "la clave primaria es id", "Bearer de la noticia"):
            self.assertIsNone(find_secret(normal), normal)


class PersistenceTests(BrainCase):
    def test_document_is_a_markdown_file_with_front_matter(self) -> None:
        meta = self.add(self.ka, "decisiones", "ADR-006 · Fechas en UTC", "Se guardan en **UTC**.", "fechas")
        self.assertEqual(meta.relpath, "decisiones/adr-006-fechas-en-utc.md")
        path = self.brain.root / "projects" / self.ka / meta.relpath
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\ntitle: ADR-006 · Fechas en UTC\ntags: fechas\nupdated: "))
        self.assertTrue(text.endswith("Se guardan en **UTC**.\n"))

    def test_survives_reopening_with_a_new_index(self) -> None:
        self.add(self.ka, "estado", "Rama activa", "feature/filtros")
        self.db.close()
        self.db = Database(self.data / "ia-workspace.db")
        brain = build_brain(self.db, AppPaths(self.data))
        self.assertEqual([d.title for d in brain.list(Scope.PROJECT, self.ka)], ["Rama activa"])
        self.assertEqual(brain.read(Scope.PROJECT, self.ka, "estado/rama-activa.md").body, "feature/filtros\n")

    def test_index_can_be_rebuilt_from_markdown_alone(self) -> None:
        self.add(self.ka, "arquitectura", "Capas", "Next.js con API en src/app/api.")
        with self.db.conn:
            self.db.conn.execute("DELETE FROM brain_documents")
            self.db.conn.execute("DELETE FROM brain_fts")
        hits = self.brain.search(Scope.PROJECT, self.ka, "api")
        self.assertEqual([h.meta.title for h in hits], ["Capas"])

    def test_no_document_text_is_stored_in_sqlite(self) -> None:
        # La primera línea es el resumen (metadato, como en INDEX.md); el resto no se copia.
        marker = "Frase-Unica-De-Contenido-7Q2"
        self.add(self.ka, "reglas", "Esquema", f"No modificar schema.prisma.\n\nDetalle: {marker} al final.")
        self.db.conn.execute("PRAGMA wal_checkpoint(FULL)")
        raw = b"".join(p.read_bytes() for p in self.data.glob("ia-workspace.db*"))
        self.assertTrue(marker.encode() not in raw, "el contenido del documento se copió a SQLite")
        self.assertTrue(b"No modificar schema.prisma." in raw, "el resumen de una línea sí es metadato")

    def test_stable_keys_without_collisions(self) -> None:
        repo = ProjectRepository(self.db)
        (self.data / "otra").mkdir()
        twin = repo.add(NewProject("Alfa", str(self.data / "otra")))  # mismo nombre, otra carpeta
        twin_key = self.brain.project_key(twin)
        self.assertNotEqual(twin_key, self.ka)
        self.assertTrue(twin_key.startswith("alfa-") and self.ka.startswith("alfa-"))
        repo.rename(self.a.id, "Alfa renombrado")
        self.assertEqual(self.brain.project_key(repo.get(self.a.id)), self.ka)  # no cambia al renombrar

    def test_update_keeps_path_and_delete_moves_to_trash(self) -> None:
        meta = self.add(self.ka, "proximos", "Filtros", "Pendiente: filtro por estado.")
        updated = self.brain.update(Scope.PROJECT, self.ka, meta.relpath, "Filtros de la lista", "Hecho.", "ui")
        self.assertEqual(updated.relpath, meta.relpath)
        self.assertEqual(updated.title, "Filtros de la lista")
        trash = self.brain.delete(Scope.PROJECT, self.ka, meta.relpath)
        self.assertTrue(trash.is_file() and trash.is_relative_to(self.brain.root / ".trash"))
        self.assertEqual(self.brain.list(Scope.PROJECT, self.ka), [])

    def test_validation(self) -> None:
        with self.assertRaises(BrainError):
            self.add(self.ka, "estado", "  ", "sin título")
        with self.assertRaises(BrainError) as caught:
            self.add(self.ka, "estado", "Claves", "OPENAI_KEY=sk-proj-" + "z" * 40)
        self.assertIn("credenciales", str(caught.exception))
        self.assertNotIn("zzzz", str(caught.exception))  # nunca repite el secreto
        with self.assertRaises(BrainError) as caught:
            self.add(self.ka, "estado", "Enorme", "x" * (MAX_DOCUMENT_BYTES + 10))
        self.assertIn("divídelo", str(caught.exception))
        with self.assertRaises(BrainError):
            self.add(self.ka, "modulos", "Sin módulo", "texto")  # la documentación por módulo exige módulo
        with self.assertRaises(BrainError):
            self.brain.create(Scope.GLOBAL, "", "estado", "No es global", "x")  # categoría de otro ámbito
        self.assertEqual(self.brain.list(Scope.PROJECT, self.ka), [])

    def test_same_title_gets_a_new_file(self) -> None:
        first = self.add(self.ka, "sesiones", "Sesión", "uno")
        second = self.add(self.ka, "sesiones", "Sesión", "dos")
        self.assertEqual((first.relpath, second.relpath), ("sesiones/sesion.md", "sesiones/sesion-2.md"))


class IndexTests(BrainCase):
    def index_text(self, scope: Scope = Scope.PROJECT, key: str | None = None) -> str:
        return self.brain.index_path(scope, self.ka if key is None else key).read_text(encoding="utf-8")

    def test_index_lists_documents_without_their_content(self) -> None:
        body = "Primera línea del resumen.\n\n" + "Detalle largo que no debe aparecer en el índice. " * 30
        self.add(self.ka, "arquitectura", "Capas", body, "api")
        self.add(self.ka, "modulos", "Webhooks", "Recibe webhooks firmados.", module="Pagos")
        text = self.index_text()
        self.assertIn("# Memoria del proyecto «Alfa»", text)
        self.assertIn("- [Capas](arquitectura/capas.md) — Primera línea del resumen. · etiquetas: api", text)
        self.assertIn("### pagos\n- [Webhooks](modulos/pagos/webhooks.md)", text)
        self.assertNotIn("Detalle largo", text)
        self.assertLess(len(text), 1500)

    def test_index_follows_changes_and_is_not_rewritten_needlessly(self) -> None:
        meta = self.add(self.ka, "reglas", "Esquema", "No tocar el esquema.")
        path = self.brain.index_path(Scope.PROJECT, self.ka)
        before = path.stat().st_mtime_ns
        time.sleep(0.01)
        self.brain.sync(Scope.PROJECT, self.ka)
        self.assertEqual(path.stat().st_mtime_ns, before, "INDEX.md se reescribió sin cambios")
        self.brain.update(Scope.PROJECT, self.ka, meta.relpath, "Esquema de datos", "No tocar.", "")
        self.assertIn("[Esquema de datos]", self.index_text())
        self.brain.delete(Scope.PROJECT, self.ka, meta.relpath)
        self.assertNotIn("Esquema", self.index_text())
        self.assertIn("_Todavía no hay documentos._", self.index_text())

    def test_hand_edited_and_new_files_are_picked_up(self) -> None:
        meta = self.add(self.ka, "estado", "Rama", "main")
        root = self.brain.root / "projects" / self.ka
        (root / meta.relpath).write_text(serialize_document("Rama activa", ("git",), "feature/x"), encoding="utf-8")
        (root / "decisiones").mkdir()
        (root / "decisiones" / "a-mano.md").write_text("# Decisión escrita a mano\n\nTexto.", encoding="utf-8")
        report = self.brain.sync(Scope.PROJECT, self.ka)
        self.assertEqual((report.added, report.updated), (1, 1))
        self.assertIn("[Rama activa]", self.index_text())
        self.assertIn("[Decisión escrita a mano](decisiones/a-mano.md)", self.index_text())

    def test_integrity_problems_are_reported_not_hidden(self) -> None:
        root = self.brain.root / "projects" / self.ka
        (root / "inventada").mkdir(parents=True)
        (root / "inventada" / "x.md").write_text("texto", encoding="utf-8")
        (root / "estado").mkdir(exist_ok=True)
        (root / "estado" / "binario.md").write_bytes(b"\xff\xfe\x00 no es utf-8")
        (root / "modulos").mkdir()
        (root / "modulos" / "sin-modulo.md").write_text("texto", encoding="utf-8")
        report = self.brain.sync(Scope.PROJECT, self.ka)
        problems = dict(report.problems)
        self.assertIn("inventada/x.md", problems)
        self.assertIn("estado/binario.md", problems)
        self.assertIn("modulos/sin-modulo.md", problems)
        self.assertEqual(self.brain.list(Scope.PROJECT, self.ka), [])

    def test_global_index_is_separate(self) -> None:
        self.brain.create(Scope.GLOBAL, "", "preferencias", "Idioma", "Responde en español.")
        self.brain.sync(Scope.PROJECT, self.ka)  # INDEX.md se crea en la primera sincronización
        self.assertIn("# Memoria global", self.index_text(Scope.GLOBAL, ""))
        self.assertNotIn("Idioma", self.index_text())


class SearchTests(BrainCase):
    def setUp(self) -> None:
        super().setUp()
        self.add(self.ka, "decisiones", "ADR-006 · Fechas en UTC",
                 "Las fechas se guardan en UTC; la conversión a la zona local se hace solo al presentar.", "fechas")
        self.add(self.ka, "arquitectura", "Capas", "API en src/app/api y dominio en src/server.", "api")
        self.add(self.ka, "modulos", "Reintentos de webhooks", "Tres reintentos con espera exponencial.",
                 "pagos", module="Pagos")
        self.add(self.kb, "decisiones", "Secreto de Beta", "La conversión de Beta es distinta.")

    def test_keywords_accents_prefixes_and_ranking(self) -> None:
        hits = self.brain.search(Scope.PROJECT, self.ka, "conversion")
        self.assertEqual([h.meta.title for h in hits], ["ADR-006 · Fechas en UTC"])
        self.assertIn("conversión", hits[0].excerpt)
        self.assertEqual([h.meta.title for h in self.brain.search(Scope.PROJECT, self.ka, "reintent")],
                         ["Reintentos de webhooks"])
        self.assertEqual(self.brain.search(Scope.PROJECT, self.ka, "fechas utc presentar")[0].meta.relpath,
                         "decisiones/adr-006-fechas-en-utc.md")
        self.assertEqual(self.brain.search(Scope.PROJECT, self.ka, "fechas inexistente"), [])

    def test_category_filter(self) -> None:
        self.assertEqual(self.brain.search(Scope.PROJECT, self.ka, "api", "decisiones"), [])
        self.assertEqual([h.meta.title for h in self.brain.search(Scope.PROJECT, self.ka, "api", "arquitectura")],
                         ["Capas"])

    def test_query_syntax_is_never_interpreted(self) -> None:
        for query in ('"', "AND OR NOT", "fechas*)", "NEAR(a b)", "title:x", "-'; DROP TABLE x;--"):
            self.brain.search(Scope.PROJECT, self.ka, query)  # no debe lanzar sqlite3.OperationalError

    def test_projects_never_see_each_other(self) -> None:
        self.assertEqual([h.meta.title for h in self.brain.search(Scope.PROJECT, self.kb, "conversion")],
                         ["Secreto de Beta"])
        self.assertNotIn("Secreto de Beta", [h.meta.title for h in self.brain.search(Scope.PROJECT, self.ka,
                                                                                         "conversion")])
        with self.assertRaises(BrainError):
            self.brain.read(Scope.PROJECT, self.ka, "decisiones/secreto-de-beta.md")
        self.assertEqual(self.brain.search(Scope.GLOBAL, "", "conversion"), [])

    def test_search_only_opens_the_matching_documents(self) -> None:
        opened: list[str] = []
        original = BrainStore.read_text

        def spy(store, scope, key, relpath):
            opened.append(relpath)
            return original(store, scope, key, relpath)

        with mock.patch.object(BrainStore, "read_text", spy):
            self.brain.list(Scope.PROJECT, self.ka)
            self.assertEqual(opened, [], "listar no debe abrir documentos")
            self.brain.search(Scope.PROJECT, self.ka, "conversion")
        self.assertEqual(opened, ["decisiones/adr-006-fechas-en-utc.md"])

    def test_limit(self) -> None:
        for i in range(12):
            self.add(self.ka, "sesiones", f"Sesión {i}", "Trabajo sobre la migración de facturas.")
        self.assertEqual(len(self.brain.search(Scope.PROJECT, self.ka, "migracion")), 8)
        self.assertEqual(len(self.brain.search(Scope.PROJECT, self.ka, "migracion", limit=3)), 3)

    def test_works_without_fts5(self) -> None:
        brain = BrainService(self.brain.root, BrainIndex(self.db.conn), self.brain._projects)
        brain._index.fts_enabled = False
        self.assertEqual([h.meta.title for h in brain.search(Scope.PROJECT, self.ka, "conversion presentar")],
                         ["ADR-006 · Fechas en UTC"])
        self.assertEqual(brain.search(Scope.PROJECT, self.kb, "reintentos"), [])


class PathSafetyTests(BrainCase):
    def test_rejects_traversal_and_foreign_paths(self) -> None:
        for bad in ("../alfa.md", "estado/../../x.md", "/etc/passwd", "~/x.md", "estado\\x.md", "estado/.oculto.md",
                    ".trash/x.md", "estado/x.txt", "x.md", "INDEX.md", "a/b/c/d.md", "estado/x.md\0",
                    f"../{self.kb}/decisiones/x.md"):
            with self.assertRaises(UnsafePathError, msg=bad):
                self.brain.store.resolve(Scope.PROJECT, self.ka, bad)
            with self.assertRaises(BrainError, msg=bad):
                self.brain.read(Scope.PROJECT, self.ka, bad)

    def test_rejects_bad_project_keys(self) -> None:
        for key in ("", "..", "../beta-0123456789ab", "alfa", "ALFA-0123456789AB", "a/b-0123456789ab"):
            with self.assertRaises(UnsafePathError, msg=key):
                self.brain.store.scope_root(Scope.PROJECT, key)
        with self.assertRaises(UnsafePathError):
            self.brain.store.scope_root(Scope.GLOBAL, self.ka)

    def test_symlinks_never_leak_outside(self) -> None:
        outside = self.data / "fuera.md"
        outside.write_text("---\ntitle: Fuera\n---\nsecreto externo", encoding="utf-8")
        root = self.brain.root / "projects" / self.ka
        (root / "estado").mkdir(parents=True, exist_ok=True)
        (root / "estado" / "enlace.md").symlink_to(outside)
        (root / "reglas").symlink_to(self.brain.root / "projects" / self.kb)
        self.brain.sync(Scope.PROJECT, self.ka)
        self.assertEqual(self.brain.list(Scope.PROJECT, self.ka), [])
        with self.assertRaises(BrainError) as caught:  # nunca se indexó: no está en la memoria
            self.brain.read(Scope.PROJECT, self.ka, "estado/enlace.md")
        self.assertNotIn("secreto externo", str(caught.exception))
        with self.assertRaises(UnsafePathError):
            self.brain.store.read_text(Scope.PROJECT, self.ka, "estado/enlace.md")
        with self.assertRaises(UnsafePathError):
            self.brain.store.write_text(Scope.PROJECT, self.ka, "estado/enlace.md", "x")
        self.assertIn("secreto externo", outside.read_text(encoding="utf-8"))  # intacto

    def test_writes_stay_inside_the_memory(self) -> None:
        self.add(self.ka, "estado", "../../../escape", "x")
        files = [p for p in self.data.rglob("*.md") if not p.is_relative_to(self.brain.root)]
        self.assertEqual(files, [])
        self.assertTrue((self.brain.root / "projects" / self.ka / "estado" / "escape.md").is_file())


class AgentInterfaceTests(BrainCase):
    def test_brief_is_a_reference_not_the_memory(self) -> None:
        self.add(self.ka, "reglas", "Esquema", "Contenido privado del proyecto " * 50)
        brief = self.brain.agent_brief(self.a, "iaw-brain")
        self.assertIn(str(self.brain.index_path(Scope.PROJECT, self.ka)), brief)
        self.assertIn("iaw-brain", brief)
        self.assertIn("index", brief)
        self.assertIn("search", brief)
        self.assertIn("read", brief)
        self.assertIn("<ruta-relativa>", brief)
        self.assertIn("propose", brief)
        self.assertIn("hito relevante", brief)
        self.assertNotIn("<ruta-del-índice>", brief)
        self.assertNotIn("Contenido privado", brief)
        self.assertLess(len(brief), 850)

        # Invocación sin comando explícito: resuelve automáticamente a cli_command()
        brief_auto = self.brain.agent_brief(self.a)
        self.assertIn("iaw-brain", brief_auto)
        self.assertLess(len(brief_auto), 850)

    def run_cli(self, args: list[str], cwd: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"IAW_DATA_DIR": str(self.data)}):
            code = cli.main(args, cwd=cwd, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_cli_scopes_to_the_current_project(self) -> None:
        self.add(self.ka, "decisiones", "Fechas", "Todo en UTC.")
        self.add(self.kb, "decisiones", "Solo Beta", "Dato de Beta.")
        sub = self.data / "alfa" / "src" / "modulo"
        sub.mkdir(parents=True)
        code, out, _ = self.run_cli(["list"], sub)  # cualquier subcarpeta del proyecto
        self.assertEqual(code, 0)
        self.assertIn("decisiones/fechas.md · Fechas", out)
        self.assertNotIn("Beta", out)
        code, out, _ = self.run_cli(["search", "utc"], self.data / "alfa")
        self.assertIn("Fechas", out)
        code, out, _ = self.run_cli(["read", "decisiones/fechas.md"], self.data / "alfa")
        self.assertEqual((code, out), (0, "# Fechas\n\nTodo en UTC.\n"))
        code, _, err = self.run_cli(["read", "decisiones/solo-beta.md"], self.data / "alfa")
        self.assertEqual(code, cli.EXIT_ERROR)
        code, _, err = self.run_cli(["read", f"../{self.kb}/decisiones/solo-beta.md"], self.data / "alfa")
        self.assertEqual(code, cli.EXIT_ERROR)
        code, _, err = self.run_cli(["list"], self.data)  # fuera de cualquier proyecto
        self.assertEqual(code, cli.EXIT_NO_PROJECT)

    def test_cli_json_and_truncation(self) -> None:
        import json

        self.add(self.ka, "estado", "Largo", "palabra " * 500)
        code, out, _ = self.run_cli(["read", "estado/largo.md", "--max-chars", "40", "--json"], self.data / "alfa")
        data = json.loads(out)
        self.assertTrue(data["truncated"])
        self.assertLessEqual(len(data["body"]), 60)
        code, out, _ = self.run_cli(["search", "palabra", "--json"], self.data / "alfa")
        self.assertEqual(json.loads(out)[0]["path"], "estado/largo.md")

    def test_wrapper_script_runs_from_a_project_folder(self) -> None:
        self.brain.create(Scope.GLOBAL, "", "preferencias", "Idioma", "Español.")
        env = dict(os.environ, IAW_DATA_DIR=str(self.data))
        result = subprocess.run([str(ROOT / "scripts" / "iaw-brain"), "list", "--global"], cwd=self.data / "beta",
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preferencias/idioma.md · Idioma", result.stdout)

    def test_wrapper_script_runs_with_minimal_path_from_subdirectory(self) -> None:
        self.add(self.ka, "decisiones", "Microservicio", "Usar gRPC.")
        sub = self.data / "alfa" / "src" / "deep" / "nested"
        sub.mkdir(parents=True)
        minimal_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": os.environ.get("HOME", "/tmp"),
            "IAW_DATA_DIR": str(self.data),
        }
        wrapper = ROOT / "scripts" / "iaw-brain"
        result = subprocess.run(
            [str(wrapper), "search", "grpc"],
            cwd=sub,
            capture_output=True,
            text=True,
            env=minimal_env,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Microservicio", result.stdout)


class RecoveryTests(unittest.TestCase):
    """Fase 3A.1: memoria de proyectos quitados del registro."""

    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.data = Path(self.tmp.name)
        self.open()

    def open(self) -> None:
        self.db = Database(self.data / "ia-workspace.db")
        self.projects = ProjectService(ProjectRepository(self.db))
        self.brain = build_brain(self.db, AppPaths(self.data))

    def reopen(self) -> None:  # como reiniciar la aplicación
        self.db.close()
        self.open()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def folder(self, name: str) -> str:
        path = self.data / "carpetas" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def with_memory(self, name: str, folder: str, *titles: str):
        project = self.projects.register(name, folder, ())
        key = self.brain.project_key(project)
        for title in titles:
            self.brain.create(Scope.PROJECT, key, "decisiones", title, f"Contenido de {title}.")
        return project, key

    def markdown(self, key: str) -> dict[str, bytes]:
        root = self.brain.root / "projects" / key
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md") if p.name != "INDEX.md"}

    def recover(self, name: str, folder: str, retired_id: int, confirmed: bool = False):
        return self.projects.register(name, folder, (), prepare=lambda p: self.brain.recover_memory(
            p, retired_id, confirmed))

    def test_remove_and_recover_reuses_the_same_documents(self) -> None:
        folder = self.folder("alfa")
        project, key = self.with_memory("Alfa", folder, "Fechas en UTC", "Capas")
        before = self.markdown(key)
        self.projects.unregister(project.id)
        self.assertEqual(self.markdown(key), before, "quitar el proyecto alteró la memoria")
        self.assertIsNone(self.brain.existing_project_key(project.id))

        [memory] = self.brain.recoverable_memories(folder)
        self.assertEqual((memory.retired.memory_key, memory.retired.project_name, memory.documents, memory.warnings),
                         (key, "Alfa", 2, ()))
        again = self.recover("Alfa renombrado", folder, memory.id)
        self.assertNotEqual(again.id, project.id)
        self.assertEqual(self.brain.existing_project_key(again.id), key)
        self.assertEqual(self.markdown(key), before, "la recuperación reescribió o duplicó documentos")
        self.assertEqual(sorted(p.name for p in (self.brain.root / "projects").iterdir()), [key])
        self.assertEqual([d.title for d in self.brain.list(Scope.PROJECT, key)], ["Capas", "Fechas en UTC"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM brain_documents WHERE project_key = ?",
                                              (key,)).fetchone()[0], 2)
        self.assertIn("«Alfa renombrado»", self.brain.index_path(Scope.PROJECT, key).read_text(encoding="utf-8"))
        self.assertEqual(self.brain.recoverable_memories(folder), [], "una memoria recuperada sigue pendiente")

    def test_detection_and_recovery_survive_a_restart(self) -> None:
        folder = self.folder("alfa")
        project, key = self.with_memory("Alfa", folder, "Nota")
        self.projects.unregister(project.id)
        self.reopen()
        self.assertEqual(self.projects.list(), [], "un proyecto quitado aparece como activo")
        [memory] = self.brain.recoverable_memories(folder)
        again = self.recover("Alfa", folder, memory.id)
        self.reopen()
        self.assertEqual(self.brain.existing_project_key(again.id), key)
        self.assertEqual(self.brain.read(Scope.PROJECT, key, "decisiones/nota.md").body, "Contenido de Nota.\n")
        history = self.db.conn.execute("SELECT recovered_project_id, recovered_at FROM retired_memories").fetchall()
        self.assertEqual([(r[0], bool(r[1])) for r in history], [(again.id, True)], "no queda historial")

    def test_starting_fresh_keeps_the_old_memory_recoverable(self) -> None:
        folder = self.folder("alfa")
        project, old_key = self.with_memory("Alfa", folder, "Antigua")
        self.projects.unregister(project.id)
        fresh = self.projects.register("Alfa", folder, ())
        new_key = self.brain.project_key(fresh)
        self.assertNotEqual(new_key, old_key)
        self.assertEqual(self.brain.list(Scope.PROJECT, new_key), [])
        self.assertEqual([m.retired.memory_key for m in self.brain.recoverable_memories(folder)], [old_key])
        self.assertTrue(self.markdown(old_key))

    def test_never_matches_by_name(self) -> None:
        project, _ = self.with_memory("Alfa", self.folder("alfa"), "Nota")
        self.projects.unregister(project.id)
        other = self.folder("otra/alfa")  # mismo nombre de proyecto y de carpeta, otra ruta
        self.assertEqual(self.brain.recoverable_memories(other), [])

    def test_identical_names_stay_separate(self) -> None:
        one, two = self.folder("uno/web"), self.folder("dos/web")
        p1, k1 = self.with_memory("Web", one, "De uno")
        p2, k2 = self.with_memory("Web", two, "De dos")
        self.projects.unregister(p1.id)
        self.projects.unregister(p2.id)
        [m1] = self.brain.recoverable_memories(one)
        [m2] = self.brain.recoverable_memories(two)
        self.assertEqual((m1.retired.memory_key, m2.retired.memory_key), (k1, k2))
        with self.assertRaises(BrainError):
            self.recover("Web", one, m2.id)  # la memoria de la otra carpeta
        self.assertEqual([p.path for p in self.projects.list()], [], "el registro fallido no se deshizo")
        again = self.recover("Web", one, m1.id)
        self.assertEqual(self.brain.existing_project_key(again.id), k1)
        self.assertEqual([d.title for d in self.brain.list(Scope.PROJECT, k1)], ["De uno"])

    def test_cross_association_is_rejected(self) -> None:
        a_folder, b_folder = self.folder("alfa"), self.folder("beta")
        a, ka = self.with_memory("Alfa", a_folder, "Secreto de Alfa")
        self.projects.unregister(a.id)
        [memory] = self.brain.recoverable_memories(a_folder)
        b, kb = self.with_memory("Beta", b_folder, "De Beta")
        with self.assertRaises(BrainError):
            self.brain.recover_memory(b, memory.id)  # otra carpeta
        fresh = self.projects.register("Alfa", a_folder, ())
        self.brain.project_key(fresh)
        with self.assertRaises(BrainError):
            self.brain.recover_memory(fresh, memory.id)  # ya tiene memoria propia
        self.assertEqual(self.brain.existing_project_key(b.id), kb)
        self.assertNotEqual(self.brain.existing_project_key(fresh.id), ka)
        self.assertEqual(len(self.brain.recoverable_memories(a_folder)), 1, "la memoria dejó de estar pendiente")

    def test_replaced_folder_requires_confirmation(self) -> None:
        folder = self.folder("alfa")
        project, key = self.with_memory("Alfa", folder, "Nota")
        self.projects.unregister(project.id)
        os.rename(folder, folder + ".viejo")
        Path(folder).mkdir()
        (Path(folder) / ".git").mkdir()
        [memory] = self.brain.recoverable_memories(folder)
        self.assertEqual(len(memory.warnings), 2, memory.warnings)  # otra carpeta y ahora es Git
        with self.assertRaises(BrainError):
            self.recover("Alfa", folder, memory.id)
        self.assertEqual(self.projects.list(), [], "se registró sin confirmar")
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM brain_projects").fetchone())
        again = self.recover("Alfa", folder, memory.id, confirmed=True)
        self.assertEqual(self.brain.existing_project_key(again.id), key)

    def test_several_candidates_are_ambiguous(self) -> None:
        folder = self.folder("alfa")
        first, k1 = self.with_memory("Alfa", folder, "Primera")
        self.projects.unregister(first.id)
        second, k2 = self.with_memory("Alfa", folder, "Segunda")
        self.projects.unregister(second.id)
        memories = self.brain.recoverable_memories(folder)
        self.assertEqual([m.retired.memory_key for m in memories], [k2, k1])  # la más reciente primero
        self.assertTrue(all(m.warnings for m in memories))
        with self.assertRaises(BrainError):
            self.brain.recover_memory(self.projects.register("Alfa", folder, ()), memories[1].id)

    def test_missing_memory_folder_is_not_offered(self) -> None:
        folder = self.folder("alfa")
        project, key = self.with_memory("Alfa", folder, "Nota")
        self.projects.unregister(project.id)
        os.rename(self.brain.root / "projects" / key, self.data / "fuera")
        self.assertEqual(self.brain.recoverable_memories(folder), [])

    def test_projects_without_memory_leave_no_record(self) -> None:
        project = self.projects.register("Sin memoria", self.folder("vacio"), ())
        self.projects.unregister(project.id)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM retired_memories").fetchone()[0], 0)

    def test_new_keys_never_reuse_an_existing_memory_folder(self) -> None:
        project, key = self.with_memory("Alfa", self.folder("alfa"), "Nota")
        self.projects.unregister(project.id)
        fresh = self.projects.register("Alfa", self.folder("alfa"), ())
        suffix = key.rsplit("-", 1)[1]
        with mock.patch("ia_workspace.brain.service.secrets.token_hex", side_effect=[suffix, "0" * 12]):
            self.assertEqual(self.brain.project_key(fresh), "alfa-" + "0" * 12)


class MigrationV4Tests(unittest.TestCase):
    def test_v3_database_upgrades_without_losing_data(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "v3.db"
            conn = sqlite3.connect(path)
            for migration in MIGRATIONS[:3]:
                for statement in migration:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 3")
            conn.execute("INSERT INTO projects (name, path, color_index, agents, created_at) "
                         "VALUES ('Existente', '/existente', 0, '[]', '2026-10-01T10:00:00+00:00')")
            conn.execute("INSERT INTO prompt_transfers (project_id, agent, prompt, method, status, created_at) "
                         "VALUES (1, 'codex', 'hola', 'direct', 'sent', '2026-10-02T10:00:00+00:00')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 4)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM prompt_transfers").fetchone()[0], 1)
            self.assertEqual(db.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()

    def test_v4_database_keeps_its_memories(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "v4.db"
            conn = sqlite3.connect(path)
            for migration in MIGRATIONS[:4]:
                for statement in migration:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 4")
            conn.execute("INSERT INTO projects (name, path, color_index, agents, created_at) "
                         "VALUES ('Existente', '/existente', 0, '[]', '2026-10-01T10:00:00+00:00')")
            conn.execute("INSERT INTO brain_projects VALUES (1, 'existente-0123456789ab', '2026-10-01T10:00:00+00:00')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertEqual(db.conn.execute("SELECT memory_key FROM brain_projects").fetchone()[0],
                             "existente-0123456789ab")
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM retired_memories").fetchone()[0], 0)
            db.close()

    def test_v5_database_migrates_to_v6_keeping_data(self) -> None:
        with temp_dir() as tmp:
            path = Path(tmp) / "v5.db"
            conn = sqlite3.connect(path)
            for migration in MIGRATIONS[:5]:
                for statement in migration:
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 5")
            conn.execute("INSERT INTO projects (name, path, color_index, agents, created_at) "
                         "VALUES ('Migrado', '/migrado', 0, '[]', '2026-10-01T10:00:00+00:00')")
            conn.execute("INSERT INTO brain_projects VALUES (1, 'migrado-0123456789ab', '2026-10-01T10:00:00+00:00')")
            conn.commit()
            conn.close()
            db = Database(path)
            self.assertEqual(db.version, SCHEMA_VERSION)
            self.assertEqual(SCHEMA_VERSION, 10)
            self.assertEqual(db.conn.execute("SELECT memory_key FROM brain_projects").fetchone()[0],
                             "migrado-0123456789ab")
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM brain_proposals").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM project_agent_modes").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM subscription_quotas").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()

    def test_no_credential_or_content_columns(self) -> None:
        db = Database(":memory:")
        for table in ("brain_projects", "brain_documents", "retired_memories", "brain_proposals"):
            columns = [c[1].lower() for c in db.conn.execute(f"PRAGMA table_info({table})")]
            self.assertFalse([c for c in columns if any(f in c for f in ("token", "secret", "password", "body"))])


class ProposalTests(BrainCase):
    def test_propose_create_and_isolation(self) -> None:
        meta = self.brain.propose(
            self.a,
            "create",
            title="ADR-007 Nueva API",
            body="Detalle de la arquitectura de la API.",
            category_key="decisiones",
            tags=("api", "v2"),
            summary="Propuesta de API v2",
            agent_kind="claude-code",
        )
        self.assertTrue(meta.id.startswith("p-"))
        self.assertEqual(meta.status, ProposalStatus.PENDING)
        self.assertEqual(meta.operation, ProposalOperation.CREATE)
        self.assertEqual(meta.category, "decisiones")
        self.assertEqual(meta.relpath, "decisiones/adr-007-nueva-api.md")

        prop_file = self.brain.root / "projects" / self.ka / ".proposals" / f"{meta.id}.md"
        self.assertTrue(prop_file.is_file())
        content = prop_file.read_text(encoding="utf-8")
        self.assertIn("operation: create", content)
        self.assertIn("Detalle de la arquitectura de la API.", content)

        # Aislamiento: NO aparece en INDEX.md, ni en list, ni en search
        self.brain.sync(Scope.PROJECT, self.ka)
        index_text = self.brain.index_path(Scope.PROJECT, self.ka).read_text(encoding="utf-8")
        self.assertNotIn("ADR-007 Nueva API", index_text)
        self.assertNotIn("adr-007-nueva-api.md", index_text)
        self.assertEqual(self.brain.list(Scope.PROJECT, self.ka, "decisiones"), [])
        self.assertEqual(self.brain.search(Scope.PROJECT, self.ka, "arquitectura"), [])

        # Aislamiento entre proyectos
        self.assertEqual(self.brain.list_proposals(self.b.id), [])
        proposals_a = self.brain.list_proposals(self.a.id)
        self.assertEqual(len(proposals_a), 1)
        self.assertEqual(proposals_a[0].id, meta.id)
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 1)

    def test_approve_create_proposal(self) -> None:
        meta = self.brain.propose(
            self.a,
            "create",
            title="Convenciones de código",
            body="Usar PEP 8 y anotaciones de tipo estrictas.",
            category_key="reglas",
            tags="pep8, typing",
        )
        doc = self.brain.approve_proposal(self.a, meta.id)
        self.assertEqual(doc.title, "Convenciones de código")
        self.assertTrue((self.brain.root / "projects" / self.ka / "reglas" / "convenciones-de-codigo.md").is_file())
        index_text = self.brain.index_path(Scope.PROJECT, self.ka).read_text(encoding="utf-8")
        self.assertIn("Convenciones de código", index_text)
        self.assertEqual(len(self.brain.search(Scope.PROJECT, self.ka, "estrictas")), 1)
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 0)
        updated = self.brain.get_proposal(self.a, meta.id)
        self.assertEqual(updated.meta.status, ProposalStatus.APPROVED)

    def test_reject_proposal_keeps_memory_clean(self) -> None:
        meta = self.brain.propose(
            self.a,
            "create",
            title="Propuesta rechazada",
            body="No debería quedar en la memoria.",
            category_key="estado",
        )
        self.brain.reject_proposal(self.a, meta.id, note="No aplica actualmente")
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 0)
        self.assertFalse((self.brain.root / "projects" / self.ka / "estado" / "propuesta-rechazada.md").exists())
        updated = self.brain.get_proposal(self.a, meta.id)
        self.assertEqual(updated.meta.status, ProposalStatus.REJECTED)
        self.assertEqual(updated.meta.review_note, "No aplica actualmente")

    def test_propose_update_and_conflict_detection(self) -> None:
        orig_doc = self.add(self.ka, "decisiones", "Base de datos", "Usamos SQLite.")
        relpath = orig_doc.relpath

        meta = self.brain.propose(
            self.a,
            "update",
            title="Base de datos",
            body="Usamos SQLite con WAL y foreign keys activadas.",
            relpath=relpath,
            agent_kind="codex",
        )
        self.assertEqual(meta.operation, ProposalOperation.UPDATE)
        self.assertTrue(len(meta.base_hash) > 10)

        # Modificación concurrente del documento original
        self.brain.update(Scope.PROJECT, self.ka, relpath, "Base de datos", "Usamos PostgreSQL en su lugar.")

        # Al intentar aprobar detecta conflicto
        with self.assertRaises(BrainConflictError):
            self.brain.approve_proposal(self.a, meta.id)

        updated = self.brain.get_proposal(self.a, meta.id)
        self.assertEqual(updated.meta.status, ProposalStatus.CONFLICT)
        current_doc = self.brain.read(Scope.PROJECT, self.ka, relpath)
        self.assertIn("PostgreSQL", current_doc.body)

    def test_secret_protection_in_proposals(self) -> None:
        with self.assertRaises(BrainError) as ctx:
            self.brain.propose(
                self.a,
                "create",
                title="Claves secretas",
                body="token: ghp_" + "A" * 36,
                category_key="estado",
            )
        self.assertIn("token", str(ctx.exception).lower())
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 0)

    def test_unsafe_paths_and_limits_rejected(self) -> None:
        with self.assertRaises(UnsafePathError):
            self.brain.propose(
                self.a,
                "create",
                title="Escape",
                body="intento de escape",
                relpath="../fuera.md",
                category_key="estado",
            )
        with self.assertRaises(BrainError):
            self.brain.propose(
                self.a,
                "create",
                title="Gigante",
                body="A" * (MAX_DOCUMENT_BYTES + 1024),
                category_key="estado",
            )

    def test_cli_propose_stdin_and_file(self) -> None:
        sub = self.data / "alfa" / "sub"
        sub.mkdir(parents=True)

        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO("Lineamientos de arquitectura inicial.\n")
        with mock.patch.dict(os.environ, {"IAW_DATA_DIR": str(self.data)}):
            code = cli.main(
                ["propose", "--category", "arquitectura", "--title", "Arquitectura inicial", "--json"],
                cwd=sub,
                out=out,
                err=err,
                stdin=stdin,
            )
        self.assertEqual(code, 0)
        self.assertIn('"status": "pending"', out.getvalue())
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 1)

        file_path = sub / "propuesta.md"
        file_path.write_text("Nueva política de commits.\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"IAW_DATA_DIR": str(self.data)}):
            code = cli.main(
                ["propose", "--category", "reglas", "--title", "Commits", "--file", str(file_path)],
                cwd=sub,
                out=out,
                err=err,
            )
        self.assertEqual(code, 0)
        self.assertIn("Propuesta registrada", out.getvalue())
        self.assertEqual(self.brain.count_pending_proposals(self.a.id), 2)

    def test_cannot_approve_twice_or_reject_approved(self) -> None:
        meta1 = self.brain.propose(
            self.a, "create", title="Doc 1", body="Contenido 1", category_key="estado"
        )
        self.brain.approve_proposal(self.a, meta1.id)
        with self.assertRaises(BrainError) as ctx:
            self.brain.approve_proposal(self.a, meta1.id)
        self.assertIn("ya fue revisada", str(ctx.exception))
        with self.assertRaises(BrainError) as ctx:
            self.brain.reject_proposal(self.a, meta1.id)
        self.assertIn("ya fue revisada", str(ctx.exception))

        meta2 = self.brain.propose(
            self.a, "create", title="Doc 2", body="Contenido 2", category_key="estado"
        )
        self.brain.reject_proposal(self.a, meta2.id)
        with self.assertRaises(BrainError) as ctx:
            self.brain.approve_proposal(self.a, meta2.id)
        self.assertIn("ya fue revisada", str(ctx.exception))
        with self.assertRaises(BrainError) as ctx:
            self.brain.reject_proposal(self.a, meta2.id)
        self.assertIn("ya fue revisada", str(ctx.exception))

    def test_create_proposal_cannot_overwrite_existing_document(self) -> None:
        meta = self.brain.propose(
            self.a, "create", title="Arquitectura Base", body="Propuesta inicial.", category_key="arquitectura"
        )
        # Se crea concurrentemente el documento oficial antes de aprobar
        self.add(self.ka, "arquitectura", "Arquitectura Base", "Documento oficial ya existente.")

        with self.assertRaises(BrainConflictError):
            self.brain.approve_proposal(self.a, meta.id)

        # La propuesta pasa a conflicto y el documento oficial conserva su contenido
        updated_prop = self.brain.get_proposal(self.a, meta.id)
        self.assertEqual(updated_prop.meta.status, ProposalStatus.CONFLICT)
        doc = self.brain.read(Scope.PROJECT, self.ka, meta.relpath)
        self.assertEqual(doc.body.strip(), "Documento oficial ya existente.")

    def test_sync_reconciles_proposals_bidirectionally(self) -> None:
        prop_id = "p-reconcile01"
        p_root = self.brain.store.ensure_proposals_root(self.ka)
        p_file = p_root / f"{prop_id}.md"
        p_file.write_text(
            "---\n"
            f"id: {prop_id}\n"
            "operation: create\n"
            "status: pending\n"
            "category: estado\n"
            "title: Recuperado de disco\n"
            "relpath: estado/recuperado-de-disco.md\n"
            "---\n\n"
            "Cuerpo restaurado directamente en disco.\n",
            encoding="utf-8",
        )

        # Sincronización detecta el archivo en disco e indexa en SQLite
        self.brain.sync(Scope.PROJECT, self.ka)
        prop = self.brain.get_proposal(self.a, prop_id)
        self.assertEqual(prop.meta.title, "Recuperado de disco")
        self.assertEqual(prop.meta.status, ProposalStatus.PENDING)

        # Si el archivo se elimina de disco, sync limpia la fila huérfana de SQLite
        p_file.unlink()
        self.brain.sync(Scope.PROJECT, self.ka)
        with self.assertRaises(BrainError):
            self.brain.get_proposal(self.a, prop_id)

    def test_atomic_rollback_on_propose_failure(self) -> None:
        with mock.patch.object(self.brain._proposals, "add", side_effect=sqlite3.OperationalError("disk error")):
            with self.assertRaises(sqlite3.OperationalError):
                self.brain.propose(
                    self.a, "create", title="Fallo simulado", body="Texto", category_key="estado"
                )
        # Comprobar que no quedó ningún archivo huérfano en .proposals/
        p_root = self.brain.store.proposals_root(self.ka)
        files = list(p_root.glob("p-*.md")) if p_root.is_dir() else []
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
