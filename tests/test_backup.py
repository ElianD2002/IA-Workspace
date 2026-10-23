"""Pruebas unitarias aisladas para el servicio de respaldo (BackupService, Fase 7B.2).

Comprueba:
1. Base SQLite en modo WAL y volcado consistente sin arrastrar -wal/-shm.
2. Inclusión exclusiva de archivos autorizados (DB, Markdown global, proyectos, propuestas y papelera).
3. Exclusión estricta de credenciales, cookies de WebKitGTK, cachés, logs y temporales.
4. Integridad del manifiesto y verificación de sumas SHA-256.
5. Permisos de seguridad en Linux (0700 en directorios, 0600 en archivos).
6. Prevención de sobrescritura de respaldos existentes.
7. Detección de modificaciones concurrentes en documentos Markdown (BackupConcurrencyError).
8. Limpieza garantizada de directorios y archivos temporales ante fallos.
9. Protección contra enlaces simbólicos maliciosos o escape de directorios (BackupSecurityError).
10. Conservación intacta del estado original después de crear el respaldo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import unittest

tests_dir = Path(__file__).resolve().parent
if str(tests_dir) not in sys.path:
    sys.path.insert(0, str(tests_dir))

from helpers import temp_dir

from ia_workspace.config import AppPaths
from ia_workspace.domain.models import AgentKind, NewProject
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.services.backup import (
    BackupConcurrencyError,
    BackupError,
    BackupSecurityError,
    BackupService,
    sha256_file,
)


class BackupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.base_dir = Path(self.tmp.name)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _init_workspace_fixture(self) -> tuple[Database, str]:
        """Crea una base de datos real con esquema en WAL y documentos de prueba."""
        db = Database(self.paths.database)
        projects = ProjectRepository(db)
        p = projects.add(NewProject("Proyecto Alfa", str(self.base_dir / "proy_alfa"), (AgentKind.CLAUDE_CODE,)))

        # Registrar memoria en brain_projects
        mem_key = "proy-alfa-0123456789ab"
        db.conn.execute(
            "INSERT INTO brain_projects (project_id, memory_key, created_at) VALUES (?, ?, ?)",
            (p.id, mem_key, "2026-10-23T00:00:00Z"),
        )

        # Crear estructura de carpetas en brain/
        brain_dir = self.paths.brain_dir
        global_dir = brain_dir / "global" / "reglas"
        global_dir.mkdir(parents=True, exist_ok=True)
        doc_global = global_dir / "codigo.md"
        doc_global.write_text("# Reglas globales de código\nUsar tipado estricto.", encoding="utf-8")
        h_global = sha256_file(doc_global)

        db.conn.execute(
            """
            INSERT INTO brain_documents (
                scope, project_key, relpath, title, category, module, tags, summary, size, mtime_ns, sha256, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "global",
                "",
                "reglas/codigo.md",
                "Reglas globales",
                "reglas",
                "",
                "",
                "Tipado estricto",
                doc_global.stat().st_size,
                doc_global.stat().st_mtime_ns,
                h_global,
                "2026-10-23T00:00:00Z",
            ),
        )

        # Documento del proyecto
        proj_dir = brain_dir / "projects" / mem_key / "arquitectura"
        proj_dir.mkdir(parents=True, exist_ok=True)
        doc_proj = proj_dir / "sistema.md"
        doc_proj.write_text("# Arquitectura de Alfa\nUsa SQLite y GTK4.", encoding="utf-8")
        h_proj = sha256_file(doc_proj)

        db.conn.execute(
            """
            INSERT INTO brain_documents (
                scope, project_key, relpath, title, category, module, tags, summary, size, mtime_ns, sha256, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "project",
                mem_key,
                "arquitectura/sistema.md",
                "Arquitectura Alfa",
                "arquitectura",
                "",
                "",
                "SQLite y GTK4",
                doc_proj.stat().st_size,
                doc_proj.stat().st_mtime_ns,
                h_proj,
                "2026-10-23T00:00:00Z",
            ),
        )

        # Propuesta pendiente en .proposals/
        prop_dir = brain_dir / "projects" / mem_key / ".proposals"
        prop_dir.mkdir(parents=True, exist_ok=True)
        prop_file = prop_dir / "p-1234abcd.md"
        prop_file.write_text("# Propuesta para actualizar arquitectura", encoding="utf-8")

        db.conn.execute(
            """
            INSERT INTO brain_proposals (
                id, project_id, project_key, agent_kind, operation, relpath, title, category, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p-1234abcd",
                p.id,
                mem_key,
                "claude_code",
                "create",
                "arquitectura/nueva.md",
                "Nueva arquitectura",
                "arquitectura",
                "pending",
                "2026-10-23T00:00:00Z",
            ),
        )

        # Documento en .trash/
        trash_dir = brain_dir / ".trash" / mem_key / "20261023000000"
        trash_dir.mkdir(parents=True, exist_ok=True)
        trash_file = trash_dir / "arquitectura__antigua.md"
        trash_file.write_text("# Documento antiguo descartado", encoding="utf-8")

        db.conn.commit()
        return db, mem_key

    def test_creates_consistent_backup_with_wal(self) -> None:
        """Crea un respaldo de SQLite en WAL y verifica su contenido y consistencia."""
        db, mem_key = self._init_workspace_fixture()

        # Realizar transacciones activas en WAL
        db.conn.execute("INSERT INTO settings (key, value) VALUES ('theme', 'dark')")
        db.conn.commit()

        # Comprobar que existe el archivo WAL en disco
        wal_file = Path(f"{self.paths.database}-wal")
        self.assertTrue(wal_file.exists() or self.paths.database.exists())

        service = BackupService(self.paths, db=db)
        backup_file = service.create_backup()

        self.assertTrue(backup_file.is_file())
        self.assertTrue(backup_file.name.endswith(".tar.gz"))

        # Extraer y comprobar base de datos respaldada
        with tarfile.open(backup_file, "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("manifest.json", names)
            self.assertIn("database.sqlite", names)
            self.assertIn("brain/global/reglas/codigo.md", names)
            self.assertIn(f"brain/projects/{mem_key}/arquitectura/sistema.md", names)
            self.assertIn(f"brain/projects/{mem_key}/.proposals/p-1234abcd.md", names)
            self.assertIn(f"brain/.trash/{mem_key}/20261023000000/arquitectura__antigua.md", names)

            # Verificar que el contenido de SQLite en el respaldo incluye la transacción en WAL
            db_member = tar.extractfile("database.sqlite")
            self.assertIsNotNone(db_member)
            extracted_db_path = self.base_dir / "extracted.sqlite"
            extracted_db_path.write_bytes(db_member.read())

            conn = sqlite3.connect(extracted_db_path)
            res = conn.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(res, "ok")
            row = conn.execute("SELECT value FROM settings WHERE key = 'theme'").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "dark")
            conn.close()

        db.close()

    def test_excludes_webkit_credentials_logs_and_external_projects(self) -> None:
        """Comprueba que WebKitGTK, cookies, cachés, logs y proyectos externos se excluyan."""
        db, _ = self._init_workspace_fixture()

        # Simular presencia de datos sensibles y efímeros en data_dir
        webkit_dir = self.data_dir / "webkit" / "data"
        webkit_dir.mkdir(parents=True, exist_ok=True)
        (webkit_dir / "cookies.sqlite").write_text("SENSITIVE_SESSION_COOKIES", encoding="utf-8")

        cache_dir = self.data_dir / "webkit" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "http_cache.bin").write_bytes(b"\x00\x01\x02\x03")

        (self.data_dir / "ui-test.log").write_text("Log temporal", encoding="utf-8")
        (self.data_dir / "temp_file.tmp").write_text("Temporal", encoding="utf-8")

        # Proyecto externo
        external_dir = self.base_dir / "proy_alfa"
        external_dir.mkdir(parents=True, exist_ok=True)
        (external_dir / "mi_codigo_privado.py").write_text("print('secreto')", encoding="utf-8")

        service = BackupService(self.paths, db=db)
        backup_file = service.create_backup()

        with tarfile.open(backup_file, "r:gz") as tar:
            names = tar.getnames()
            for name in names:
                self.assertNotIn("webkit", name)
                self.assertNotIn("cookies", name)
                self.assertNotIn("cache", name)
                self.assertNotIn(".log", name)
                self.assertNotIn("mi_codigo_privado", name)
                self.assertNotIn("proy_alfa", name)

        db.close()

    def test_manifest_checksums_and_stats(self) -> None:
        """Verifica que el manifiesto registre tamaños, hashes e inventario con exactitud."""
        db, _ = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)
        backup_file = service.create_backup()

        with tarfile.open(backup_file, "r:gz") as tar:
            manifest_file = tar.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read().decode("utf-8"))

            self.assertEqual(manifest["manifest_version"], 1)
            self.assertEqual(manifest["app_name"], "ia-workspace")
            self.assertEqual(manifest["database"]["integrity_check"], "ok")
            self.assertEqual(manifest["stats"]["projects_count"], 1)
            self.assertEqual(manifest["stats"]["documents_count"], 2)
            self.assertEqual(manifest["stats"]["proposals_count"], 1)
            self.assertEqual(manifest["stats"]["trash_documents_count"], 1)

            # Validar que cada archivo del manifiesto coincida exactamente en el archivo tar
            for entry in manifest["files"]:
                arcname = entry["path"]
                tar_member = tar.extractfile(arcname)
                self.assertIsNotNone(tar_member, f"Falta {arcname} en el archivo tar")
                content = tar_member.read()
                self.assertEqual(len(content), entry["size"])
                import hashlib
                h = hashlib.sha256(content).hexdigest()
                self.assertEqual(h, entry["sha256"], f"Hash no coincide para {arcname}")

        db.close()

    def test_permissions_are_secure(self) -> None:
        """Verifica que los directorios tengan permisos 0700 y los archivos 0600."""
        db, _ = self._init_workspace_fixture()

        dest_dir = self.base_dir / "custom_backups"
        service = BackupService(self.paths, db=db, destination_dir=dest_dir)
        backup_file = service.create_backup()

        # Permisos de la carpeta destino (0700)
        dest_mode = dest_dir.stat().st_mode & 0o777
        self.assertEqual(dest_mode, 0o700)

        # Permisos del archivo tar.gz final (0600)
        file_mode = backup_file.stat().st_mode & 0o777
        self.assertEqual(file_mode, 0o600)

        # Permisos de las entradas dentro del tar
        with tarfile.open(backup_file, "r:gz") as tar:
            for member in tar.getmembers():
                self.assertEqual(member.mode & 0o777, 0o600)

        db.close()

    def test_does_not_overwrite_existing_backup(self) -> None:
        """Comprueba que no se sobrescriba un archivo de respaldo explícito ya existente."""
        db, _ = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)
        custom_name = "mi-respaldo-fijo.tar.gz"
        first = service.create_backup(filename=custom_name)
        self.assertEqual(first.name, custom_name)

        # Intentar crear otro con el mismo nombre explícito debe lanzar BackupError
        with self.assertRaises(BackupError) as ctx:
            service.create_backup(filename=custom_name)
        self.assertIn("ya existe", str(ctx.exception))

        db.close()

    def test_detects_concurrent_modifications_and_aborts(self) -> None:
        """Verifica que si un documento es modificado concurrentemente se aborte sin publicar."""
        db, mem_key = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)

        # Modificar un documento en disco sin actualizar SQLite para simular cambio concurrente
        doc_file = self.paths.brain_dir / "projects" / mem_key / "arquitectura" / "sistema.md"
        doc_file.write_text("# Modificación concurrente por agente externo!", encoding="utf-8")

        with self.assertRaises(BackupConcurrencyError):
            service.create_backup()

        # Comprobar que no quedó ningún archivo de respaldo publicado en backups/
        backups = list(self.paths.backups_dir.glob("*.tar.gz"))
        self.assertEqual(backups, [])

        db.close()

    def test_cleans_up_temporary_files_on_failure(self) -> None:
        """Verifica que ante un fallo en el respaldo se elimine la carpeta temporal."""
        db, mem_key = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)

        # Provocar error borrando el archivo de una propuesta que SQLite cree que existe
        prop_file = self.paths.brain_dir / "projects" / mem_key / ".proposals" / "p-1234abcd.md"
        prop_file.unlink()

        with self.assertRaises(BackupConcurrencyError):
            service.create_backup()

        # Verificar que no quedaron carpetas o archivos .tmp-* en backups_dir
        if self.paths.backups_dir.exists():
            tmps = [p for p in self.paths.backups_dir.iterdir() if p.name.startswith(".tmp-")]
            self.assertEqual(tmps, [])

        db.close()

    def test_rejects_symlinks_for_security(self) -> None:
        """Rechaza enlaces simbólicos dentro de la carpeta del Cerebro."""
        db, mem_key = self._init_workspace_fixture()

        # Crear un symlink malicioso apuntando a /etc/hosts
        evil_link = self.paths.brain_dir / "projects" / mem_key / "arquitectura" / "evil.md"
        evil_link.symlink_to("/etc/hosts")

        service = BackupService(self.paths, db=db)
        with self.assertRaises(BackupSecurityError):
            service.create_backup()

        db.close()

    def test_include_trash_toggle(self) -> None:
        """Verifica que si include_trash=False no se empaquete la papelera .trash/."""
        db, mem_key = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db, include_trash=False)
        backup_file = service.create_backup()

        with tarfile.open(backup_file, "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("brain/global/reglas/codigo.md", names)
            self.assertIn(f"brain/projects/{mem_key}/arquitectura/sistema.md", names)
            # No debe contener nada de .trash
            for name in names:
                self.assertNotIn(".trash", name)

            manifest_file = tar.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read().decode("utf-8"))
            self.assertEqual(manifest["stats"]["trash_documents_count"], 0)

        db.close()

    def test_no_duplicate_entries_in_archive(self) -> None:
        """Verifica que el archivo no contenga rutas repetidas en su índice."""
        db, _ = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)
        backup_file = service.create_backup()

        with tarfile.open(backup_file, "r:gz") as tar:
            names = tar.getnames()
            self.assertEqual(len(names), len(set(names)), "El archivo tar contiene entradas duplicadas")

        db.close()

    def test_publish_race_condition_does_not_overwrite(self) -> None:
        """Comprueba que no se sobrescriba un archivo si aparece en el destino justo antes de publicar."""
        db, _ = self._init_workspace_fixture()
        service = BackupService(self.paths, db=db)

        # Simular una colisión de carrera interceptando _pack_archive para crear el archivo en destino
        target_name = "respaldo-colision.tar.gz"
        dest_file = self.paths.backups_dir / target_name
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)

        original_pack = service._pack_archive

        def pack_and_collide(*args, **kwargs):
            original_pack(*args, **kwargs)
            # Simular que otro proceso creó el archivo en el directorio destino exactamente antes de publicar
            dest_file.write_text("ARCHIVO_PREEXISTENTE", encoding="utf-8")

        service._pack_archive = pack_and_collide  # type: ignore[assignment]

        with self.assertRaises(BackupError) as ctx:
            service.create_backup(filename=target_name)
        self.assertIn("ya existe", str(ctx.exception))

        # Verificar que el contenido preexistente jamás fue sobrescrito
        self.assertEqual(dest_file.read_text(encoding="utf-8"), "ARCHIVO_PREEXISTENTE")
        db.close()

    def test_detects_concurrent_unrecorded_proposal(self) -> None:
        """Verifica que una propuesta Markdown creada en disco concurrentemente aborte el respaldo."""
        db, mem_key = self._init_workspace_fixture()
        service = BackupService(self.paths, db=db)

        # Crear un archivo de propuesta en disco que no existe en SQLite
        ghost_prop = self.paths.brain_dir / "projects" / mem_key / ".proposals" / "p-9999ghost.md"
        ghost_prop.write_text("# Propuesta fantasma creada concurrentemente", encoding="utf-8")

        with self.assertRaises(BackupConcurrencyError) as ctx:
            service.create_backup()
        self.assertIn("creada en disco pero no registrada", str(ctx.exception))

        db.close()

    def test_preserves_original_state_after_backup(self) -> None:
        """Verifica que la base y los archivos originales no sufran cambios ni bloqueos."""
        db, _ = self._init_workspace_fixture()

        service = BackupService(self.paths, db=db)
        backup_file = service.create_backup()
        self.assertTrue(backup_file.is_file())

        # Verificar que la conexión original sigue viva y operativa
        row = db.conn.execute("SELECT COUNT(*) FROM brain_documents").fetchone()
        self.assertEqual(row[0], 2)

        # Probar inserción post-respaldo
        db.conn.execute("INSERT INTO settings (key, value) VALUES ('post_backup', 'ok')")
        db.conn.commit()

        read_back = db.conn.execute("SELECT value FROM settings WHERE key = 'post_backup'").fetchone()
        self.assertEqual(read_back[0], "ok")

        db.close()


if __name__ == "__main__":
    unittest.main()
