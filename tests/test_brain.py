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
from ia_workspace.brain.service import BrainService
from ia_workspace.brain.store import BrainStore, UnsafePathError
from ia_workspace.config import AppPaths
from ia_workspace.context import build_brain
from ia_workspace.domain.models import NewProject
from ia_workspace.persistence.brain_index import BrainIndex
from ia_workspace.persistence.database import MIGRATIONS, SCHEMA_VERSION, Database
from ia_workspace.persistence.repositories import ProjectRepository


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
        self.assertIn("iaw-brain search", brief)
        self.assertIn("iaw-brain read", brief)
        self.assertNotIn("Contenido privado", brief)
        self.assertLess(len(brief), 900)

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

    def test_no_credential_or_content_columns(self) -> None:
        db = Database(":memory:")
        for table in ("brain_projects", "brain_documents"):
            columns = [c[1].lower() for c in db.conn.execute(f"PRAGMA table_info({table})")]
            self.assertFalse([c for c in columns if any(f in c for f in ("token", "secret", "password", "body"))])


if __name__ == "__main__":
    unittest.main()
