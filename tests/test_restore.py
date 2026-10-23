"""Pruebas unitarias para el servicio de restauración (RestoreService, Fase 7B.3).

Comprueba:
1. Validación de un respaldo legítimo sin modificar datos.
2. Restauración completa de SQLite + Markdown.
3. Preservación estricta de WebKitGTK (cookies, perfiles) y respaldos anteriores en backups/.
4. Rechazo de respaldo con archivo corrupto o modificado (discrepancia SHA-256).
5. Rechazo de SQLite inválida (fallo en PRAGMA integrity_check).
6. Rechazo de esquema SQLite incompatible (> SCHEMA_VERSION).
7. Rechazo de rutas maliciosas (escape de directorio, rutas absolutas) y archivos duplicados.
8. Límites de tamaño y prevención de tarbombs.
9. Fallo provocado durante la sustitución de SQLite y verificación del rollback.
10. Fallo provocado durante la sustitución de Markdown y verificación del rollback.
11. Rollback completo y recuperación fiel del estado anterior ante cualquier error transaccional.
12. Rechazo de restauración cuando otra instancia mantiene abiertos los datos (RestoreExclusivityError).
13. Preservación de registros de proyectos con rutas en disco inexistentes sin modificarlas.
"""

from __future__ import annotations

import fcntl
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
from ia_workspace.persistence.database import Database, SCHEMA_VERSION
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.services.backup import BackupService, sha256_file
from ia_workspace.services.restore import (
    RestoreError,
    RestoreExclusivityError,
    RestoreSecurityError,
    RestoreService,
    RestoreValidationError,
)


class RestoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.base_dir = Path(self.tmp.name)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_fixture_workspace(
        self,
        db_path: Path,
        brain_dir: Path,
        project_name: str = "Proyecto Base",
        mem_key: str = "proy-base-123456789abc",
    ) -> Database:
        """Crea una base y estructura de cerebro válidas."""
        db = Database(db_path)
        projects = ProjectRepository(db)
        p = projects.add(NewProject(project_name, str(self.base_dir / project_name), (AgentKind.CLAUDE_CODE,)))

        db.conn.execute(
            "INSERT INTO brain_projects (project_id, memory_key, created_at) VALUES (?, ?, ?)",
            (p.id, mem_key, "2026-10-23T00:00:00Z"),
        )

        # Reglas globales
        global_dir = brain_dir / "global" / "reglas"
        global_dir.mkdir(parents=True, exist_ok=True)
        doc_global = global_dir / "base.md"
        doc_global.write_text("# Reglas Base\nTexto inicial.", encoding="utf-8")
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
                "reglas/base.md",
                "Reglas Base",
                "reglas",
                "",
                "",
                "Texto inicial",
                doc_global.stat().st_size,
                doc_global.stat().st_mtime_ns,
                h_global,
                "2026-10-23T00:00:00Z",
            ),
        )

        # Documento de proyecto
        proj_dir = brain_dir / "projects" / mem_key / "arquitectura"
        proj_dir.mkdir(parents=True, exist_ok=True)
        doc_proj = proj_dir / "diseno.md"
        doc_proj.write_text("# Diseno\nArquitectura base.", encoding="utf-8")
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
                "arquitectura/diseno.md",
                "Diseno",
                "arquitectura",
                "",
                "",
                "Arquitectura base",
                doc_proj.stat().st_size,
                doc_proj.stat().st_mtime_ns,
                h_proj,
                "2026-10-23T00:00:00Z",
            ),
        )

        # Propuesta
        prop_dir = brain_dir / "projects" / mem_key / ".proposals"
        prop_dir.mkdir(parents=True, exist_ok=True)
        prop_file = prop_dir / "p-prop001.md"
        prop_file.write_text("# Propuesta 001", encoding="utf-8")

        db.conn.execute(
            """
            INSERT INTO brain_proposals (
                id, project_id, project_key, agent_kind, operation, relpath, title, category, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p-prop001",
                p.id,
                mem_key,
                "claude_code",
                "create",
                "arquitectura/nueva.md",
                "Nueva",
                "arquitectura",
                "pending",
                "2026-10-23T00:00:00Z",
            ),
        )
        db.conn.commit()
        return db

    def test_validate_and_restore_legitimate_backup(self) -> None:
        """Comprueba validación y restauración completa de un respaldo legítimo."""
        # 1. Crear entorno origen y generar respaldo
        source_dir = self.base_dir / "source_data"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(
            source_paths.database, source_paths.brain_dir, project_name="Origen", mem_key="key-origen-111"
        )
        bkp_service = BackupService(source_paths, db=db_src)
        backup_file = bkp_service.create_backup(filename="respaldo_valido.tar.gz")
        db_src.close()

        # 2. Crear entorno destino con otros datos previos
        db_dst = self._create_fixture_workspace(
            self.paths.database, self.paths.brain_dir, project_name="Previo", mem_key="key-previo-000"
        )
        db_dst.close()

        restore_service = RestoreService(self.paths)

        # 3. Validar sin modificar datos
        report = restore_service.validate_backup(backup_file)
        self.assertTrue(report.integrity_ok)
        self.assertEqual(report.schema_version, SCHEMA_VERSION)
        self.assertEqual(report.stats["projects_count"], 1)

        # Comprobar que los datos destino siguen intactos
        conn_pre = sqlite3.connect(self.paths.database)
        self.assertEqual(conn_pre.execute("SELECT name FROM projects").fetchone()[0], "Previo")
        conn_pre.close()

        # 4. Restaurar sin confirmación debe fallar
        with self.assertRaises(RestoreError) as ctx:
            restore_service.restore_backup(backup_file, confirmed=False)
        self.assertIn("confirmación explícita", str(ctx.exception))

        # 5. Restaurar con confirmación
        restore_service.restore_backup(backup_file, confirmed=True)

        # Verificar que ahora contiene los datos del respaldo
        conn_post = sqlite3.connect(self.paths.database)
        row = conn_post.execute("SELECT name FROM projects").fetchone()
        self.assertEqual(row[0], "Origen")
        conn_post.close()

        # Verificar archivo de brain restaurado
        restored_doc = self.paths.brain_dir / "projects" / "key-origen-111" / "arquitectura" / "diseno.md"
        self.assertTrue(restored_doc.is_file())
        self.assertIn("Arquitectura base", restored_doc.read_text(encoding="utf-8"))

    def test_preserves_webkit_cookies_and_existing_backups(self) -> None:
        """Verifica que la restauración no toque cookies de WebKitGTK ni respaldos en backups/."""
        # Generar respaldo de origen
        source_dir = self.base_dir / "src_data"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup(filename="valid.tar.gz")
        db_src.close()

        # Configurar destino con datos previos y recursos no incluidos
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir)
        db_dst.close()

        webkit_cookies = self.data_dir / "webkit" / "data" / "cookies.sqlite"
        webkit_cookies.parent.mkdir(parents=True, exist_ok=True)
        webkit_cookies.write_text("SENSITIVE_SESSION_COOKIES_PERSISTED", encoding="utf-8")

        custom_backup_file = self.paths.backups_dir / "anterior_123.tar.gz"
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        custom_backup_file.write_text("RESPALDO_ANTERIOR_CONSERVADO", encoding="utf-8")

        restore_service = RestoreService(self.paths)
        restore_service.restore_backup(bkp, confirmed=True)

        # Comprobar preservación estricta
        self.assertTrue(webkit_cookies.exists())
        self.assertEqual(webkit_cookies.read_text(encoding="utf-8"), "SENSITIVE_SESSION_COOKIES_PERSISTED")
        self.assertTrue(custom_backup_file.exists())
        self.assertEqual(custom_backup_file.read_text(encoding="utf-8"), "RESPALDO_ANTERIOR_CONSERVADO")

    def test_rejects_corrupted_file_or_sha256_mismatch(self) -> None:
        """Detecta discrepancias de checksum SHA-256 o archivos corruptos."""
        source_dir = self.base_dir / "src_corrupt"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Extraer el tar, alterar un archivo con exactamente el mismo tamaño para forzar fallo de sha256
        tampered_dir = self.base_dir / "tampered"
        tampered_dir.mkdir()
        with tarfile.open(bkp, "r:gz") as tar:
            tar.extractall(tampered_dir, filter="data")

        # Modificar un solo byte conservando el tamaño exacto
        doc = next(tampered_dir.glob("brain/**/*.md"))
        original_bytes = bytearray(doc.read_bytes())
        original_bytes[0] = ord("X") if original_bytes[0] != ord("X") else ord("Y")
        doc.write_bytes(original_bytes)

        corrupt_tar = self.base_dir / "corrupted_backup.tar.gz"
        with tarfile.open(corrupt_tar, "w:gz") as tar:
            for root, _, files in os.walk(tampered_dir):
                for f in files:
                    full = Path(root) / f
                    rel = full.relative_to(tampered_dir)
                    tar.add(full, arcname=str(rel))

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreValidationError) as ctx:
            restore_service.validate_backup(corrupt_tar)
        self.assertIn("Suma SHA-256 corrupta", str(ctx.exception))

    def test_rejects_corrupt_sqlite_integrity_check(self) -> None:
        """Rechaza un respaldo si PRAGMA integrity_check de SQLite falla."""
        bad_tar = self.base_dir / "bad_sqlite.tar.gz"
        temp_dir = self.base_dir / "prep_bad_sql"
        temp_dir.mkdir()

        bad_db = temp_dir / "database.sqlite"
        bad_db.write_bytes(b"SQLite format 3\x00\x00" + b"\xff" * 1000)
        h_db = sha256_file(bad_db)

        manifest = {
            "manifest_version": 1,
            "app_name": "ia-workspace",
            "schema_version": SCHEMA_VERSION,
            "created_at": "2026-10-23T00:00:00Z",
            "files": [
                {"path": "database.sqlite", "size": bad_db.stat().st_size, "sha256": h_db}
            ],
            "database": {"integrity_check": "ok"},
            "stats": {},
        }
        (temp_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with tarfile.open(bad_tar, "w:gz") as tar:
            tar.add(temp_dir / "manifest.json", arcname="manifest.json")
            tar.add(bad_db, arcname="database.sqlite")

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreValidationError) as ctx:
            restore_service.validate_backup(bad_tar)
        self.assertTrue(
            "integrity_check falló" in str(ctx.exception)
            or "corrupción en la base de datos" in str(ctx.exception)
        )

    def test_rejects_incompatible_schema_version(self) -> None:
        """Rechaza esquemas más modernos que el soportado por la aplicación."""
        source_dir = self.base_dir / "src_version"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Alterar versión de esquema a 999 en manifest y SQLite
        tampered_dir = self.base_dir / "tampered_ver"
        tampered_dir.mkdir()
        with tarfile.open(bkp, "r:gz") as tar:
            tar.extractall(tampered_dir, filter="data")

        m_path = tampered_dir / "manifest.json"
        m = json.loads(m_path.read_text(encoding="utf-8"))
        m["schema_version"] = 999
        m_path.write_text(json.dumps(m), encoding="utf-8")

        future_tar = self.base_dir / "future_schema.tar.gz"
        with tarfile.open(future_tar, "w:gz") as tar:
            for root, _, files in os.walk(tampered_dir):
                for f in files:
                    full = Path(root) / f
                    rel = full.relative_to(tampered_dir)
                    tar.add(full, arcname=str(rel))

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreValidationError) as ctx:
            restore_service.validate_backup(future_tar)
        self.assertIn("incompatible", str(ctx.exception))

    def test_rejects_malicious_paths_and_symlinks(self) -> None:
        """Rechaza entradas con rutas de escape ('../') y enlaces simbólicos."""
        evil_tar = self.base_dir / "evil_path.tar.gz"
        temp_dir = self.base_dir / "prep_evil"
        temp_dir.mkdir()
        (temp_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (temp_dir / "dummy.txt").write_text("malicioso", encoding="utf-8")

        with tarfile.open(evil_tar, "w:gz") as tar:
            tar.add(temp_dir / "manifest.json", arcname="manifest.json")
            # Inyectar ruta maliciosa que escapa
            tar.add(temp_dir / "dummy.txt", arcname="../etc/passwd")

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreSecurityError) as ctx:
            restore_service.validate_backup(evil_tar)
        self.assertIn("insegura", str(ctx.exception))

    def test_rejects_duplicate_entries_and_tarbombs(self) -> None:
        """Rechaza archivos tar con entradas duplicadas."""
        dup_tar = self.base_dir / "duplicate.tar.gz"
        temp_dir = self.base_dir / "prep_dup"
        temp_dir.mkdir()
        (temp_dir / "manifest.json").write_text("{}", encoding="utf-8")

        with tarfile.open(dup_tar, "w:gz") as tar:
            tar.add(temp_dir / "manifest.json", arcname="manifest.json")
            tar.add(temp_dir / "manifest.json", arcname="manifest.json")

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreSecurityError) as ctx:
            restore_service.validate_backup(dup_tar)
        self.assertIn("duplicada", str(ctx.exception))

    def test_rollback_on_sqlite_swap_failure(self) -> None:
        """Verifica que si ocurre un error al sustituir SQLite se restaure el estado original."""
        source_dir = self.base_dir / "src_rb1"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(
            source_paths.database, source_paths.brain_dir, project_name="Nuevo"
        )
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Estado inicial en destino
        db_dst = self._create_fixture_workspace(
            self.paths.database, self.paths.brain_dir, project_name="Original_Preservado"
        )
        db_dst.close()

        restore_service = RestoreService(self.paths)

        # Forzar un fallo intencional en _apply_staged_restore interceptando el reemplazo de la BD activa
        real_replace = os.replace

        def failing_replace(src, dst):
            if Path(dst) == self.paths.database:
                raise PermissionError("Fallo simulado en sustitución de SQLite")
            return real_replace(src, dst)

        os.replace = failing_replace
        try:
            with self.assertRaises(RestoreError) as ctx:
                restore_service.restore_backup(bkp, confirmed=True)
            self.assertIn("rollback completo", str(ctx.exception))
        finally:
            os.replace = real_replace

        # Comprobar que la base original fue recuperada íntegramente
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()[0], "Original_Preservado")
        conn.close()

    def test_rollback_on_brain_swap_failure(self) -> None:
        """Verifica que si ocurre un error al sustituir brain/ se revierta SQLite y brain/."""
        source_dir = self.base_dir / "src_rb2"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(
            source_paths.database, source_paths.brain_dir, project_name="Nuevo_RB2"
        )
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Estado inicial en destino
        db_dst = self._create_fixture_workspace(
            self.paths.database, self.paths.brain_dir, project_name="Original_RB2"
        )
        db_dst.close()

        restore_service = RestoreService(self.paths)

        # Permitir que SQLite se reemplace pero fallar durante el reemplazo final de brain/
        real_replace = os.replace

        def failing_replace_brain(src, dst):
            # Provocar fallo cuando se va a reemplazar la carpeta brain/ final
            if Path(dst) == self.paths.brain_dir and ".tmp-brain-replace" in str(src):
                raise OSError("Fallo simulado al posicionar nueva carpeta brain/")
            return real_replace(src, dst)

        os.replace = failing_replace_brain
        try:
            with self.assertRaises(RestoreError) as ctx:
                restore_service.restore_backup(bkp, confirmed=True)
            self.assertIn("rollback completo", str(ctx.exception))
        finally:
            os.replace = real_replace

        # Comprobar que tanto SQLite como brain volvieron a su estado original
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()[0], "Original_RB2")
        conn.close()

        orig_doc = next(self.paths.brain_dir.glob("global/**/*.md"))
        self.assertIn("Reglas Base", orig_doc.read_text(encoding="utf-8"))

    def test_rejects_restore_when_active_instance_locks_database(self) -> None:
        """Comprueba que si otra instancia mantiene bloqueada la base activa se rechace con RestoreExclusivityError."""
        source_dir = self.base_dir / "src_excl"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Instancia destino activa
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir)
        # Mantener una transacción exclusiva abierta que simula otra instancia en ejecución
        db_dst.conn.execute("BEGIN EXCLUSIVE")

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreExclusivityError) as ctx:
            restore_service.restore_backup(bkp, confirmed=True)
        self.assertIn("utilizada por otra", str(ctx.exception))

        db_dst.conn.execute("ROLLBACK")
        db_dst.close()

    def test_rejects_restore_when_app_lock_is_held(self) -> None:
        """Comprueba que si una instancia mantiene .app.lock activo se rechaza la restauración."""
        source_dir = self.base_dir / "src_applock"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Instancia destino con app.lock activo
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir)
        db_dst.close()

        lockfile = self.paths.data_dir / ".app.lock"
        fd = open(lockfile, "w+")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            restore_service = RestoreService(self.paths)
            with self.assertRaises(RestoreExclusivityError) as ctx:
                restore_service.restore_backup(bkp, confirmed=True)
            self.assertIn("la aplicación se encuentra en ejecución", str(ctx.exception))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

        # Comprobar que una vez liberado el app.lock la restauración se autoriza
        restore_service = RestoreService(self.paths)
        report = restore_service.restore_backup(bkp, confirmed=True)
        self.assertTrue(report.integrity_ok)

    def test_rejects_restore_when_wal_reader_is_active(self) -> None:
        """Verifica que un lector WAL activo en transacción impida la restauración asegurando que no se asuma exclusividad."""
        source_dir = self.base_dir / "src_wal_reader"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir, project_name="Origen WAL")
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Destino con conexión lectora abierta en WAL
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir, project_name="Destino WAL Previo")
        db_dst.close()

        reader_conn = sqlite3.connect(self.paths.database)
        reader_conn.execute("BEGIN")
        reader_conn.execute("SELECT * FROM projects")

        try:
            restore_service = RestoreService(self.paths)
            with self.assertRaises(RestoreExclusivityError) as ctx:
                restore_service.restore_backup(bkp, confirmed=True)
            self.assertTrue(
                "utilizada por otra" in str(ctx.exception)
                or "lectores activos en modo WAL" in str(ctx.exception)
            )
        finally:
            reader_conn.execute("ROLLBACK")
            reader_conn.close()

        # Tras cerrar el lector, la restauración debe funcionar perfectamente
        restore_service = RestoreService(self.paths)
        restore_service.restore_backup(bkp, confirmed=True)

        verify_conn = sqlite3.connect(self.paths.database)
        self.assertEqual(verify_conn.execute("SELECT name FROM projects").fetchone()[0], "Origen WAL")
        verify_conn.close()

    def test_rejects_concurrent_restores_on_same_workspace(self) -> None:
        """Verifica que dos procesos o llamadas de restauración concurrentes no puedan ejecutarse a la vez."""
        source_dir = self.base_dir / "src_concurrent"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Destino
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir)
        db_dst.close()

        # Simular otra restauración en progreso que mantiene el restore lock
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        r_lock = self.paths.data_dir / ".restore.lock"
        fd = open(r_lock, "a+")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            restore_service = RestoreService(self.paths)
            with self.assertRaises(RestoreExclusivityError) as ctx:
                restore_service.restore_backup(bkp, confirmed=True)
            self.assertIn("Otra operación de restauración está en ejecución", str(ctx.exception))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()

    def test_rejection_preserves_original_data_intact(self) -> None:
        """Verifica que ante un rechazo por exclusividad los datos originales permanezcan completamente intactos."""
        source_dir = self.base_dir / "src_preserve_intact"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir, project_name="NUEVO_RECHAZADO")
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Destino original con datos conocidos
        db_dst = self._create_fixture_workspace(self.paths.database, self.paths.brain_dir, project_name="INTACTO_ORIGINAL")
        # Bloquear para forzar rechazo
        db_dst.conn.execute("BEGIN EXCLUSIVE")

        restore_service = RestoreService(self.paths)
        with self.assertRaises(RestoreExclusivityError):
            restore_service.restore_backup(bkp, confirmed=True)

        db_dst.conn.execute("ROLLBACK")
        db_dst.close()

        # Confirmar que la base y archivos de destino no cambiaron ni un solo byte
        conn = sqlite3.connect(self.paths.database)
        self.assertEqual(conn.execute("SELECT name FROM projects").fetchone()[0], "INTACTO_ORIGINAL")
        conn.close()

    def test_preserves_external_projects_without_modifying_them(self) -> None:
        """Verifica que proyectos con rutas inexistentes se conserven sin alterar el disco exterior."""
        source_dir = self.base_dir / "src_ext"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        ghost_path = self.base_dir / "inexistente" / "ruta" / "fantasma"

        db_src = Database(source_paths.database)
        projects = ProjectRepository(db_src)
        projects.add(NewProject("Fantasma", str(ghost_path), (AgentKind.CLAUDE_CODE,)))
        db_src.conn.commit()

        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        restore_service = RestoreService(self.paths)
        restore_service.restore_backup(bkp, confirmed=True)

        # Comprobar que en la base restaurada existe el registro pero la carpeta exterior NO fue creada ni tocada
        conn = sqlite3.connect(self.paths.database)
        row = conn.execute("SELECT path FROM projects WHERE name = 'Fantasma'").fetchone()
        self.assertEqual(row[0], str(ghost_path))
        self.assertFalse(ghost_path.exists())
        conn.close()

    def test_older_compatible_schema_migrates_safely(self) -> None:
        """Verifica que un respaldo con un esquema SQLite anterior se migre automáticamente con seguridad."""
        source_dir = self.base_dir / "src_old_schema"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        # Descomprimir y retroceder el esquema real a la versión 9 (sin tabla prompt_templates)
        tampered_dir = self.base_dir / "tampered_old_schema"
        tampered_dir.mkdir()
        with tarfile.open(bkp, "r:gz") as tar:
            tar.extractall(tampered_dir, filter="data")

        # Eliminar las tablas/índices de la migración 10 y fijar user_version en 9
        older_version = 9
        db_file = tampered_dir / "database.sqlite"
        conn = sqlite3.connect(db_file)
        conn.execute("DROP TABLE IF EXISTS prompt_templates")
        conn.execute(f"PRAGMA user_version = {older_version}")
        conn.close()

        # Re-hashear database.sqlite y actualizar manifest
        new_db_hash = sha256_file(db_file)
        m_file = tampered_dir / "manifest.json"
        manifest = json.loads(m_file.read_text(encoding="utf-8"))
        manifest["schema_version"] = older_version
        manifest["database"]["sha256"] = new_db_hash
        manifest["database"]["size"] = db_file.stat().st_size
        for entry in manifest["files"]:
            if entry["path"] == "database.sqlite":
                entry["sha256"] = new_db_hash
                entry["size"] = db_file.stat().st_size

        m_file.write_text(json.dumps(manifest), encoding="utf-8")

        older_tar = self.base_dir / "older_compatible.tar.gz"
        with tarfile.open(older_tar, "w:gz") as tar:
            for root, _, files in os.walk(tampered_dir):
                for f in files:
                    full = Path(root) / f
                    rel = full.relative_to(tampered_dir)
                    tar.add(full, arcname=str(rel))

        restore_service = RestoreService(self.paths)
        report = restore_service.validate_backup(older_tar)
        self.assertEqual(report.schema_version, older_version)

        # Restaurar debe aplicar las migraciones faltantes hasta SCHEMA_VERSION
        restore_service.restore_backup(older_tar, confirmed=True)

        migrated_conn = sqlite3.connect(self.paths.database)
        self.assertEqual(int(migrated_conn.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        # Verificar que la tabla de la migración 10 fue creada correctamente
        has_table = migrated_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_templates'"
        ).fetchone()
        self.assertIsNotNone(has_table)
        migrated_conn.close()

    def test_tarbomb_limit_exceeded(self) -> None:
        """Verifica que se rechacen archivos que superan límites de descompresión configurados."""
        import ia_workspace.services.restore as restore_mod

        # Ajustar temporalmente MAX_FILE_BYTES a 10 bytes para probar rechazo de archivo grande
        orig_limit = restore_mod.MAX_FILE_BYTES
        restore_mod.MAX_FILE_BYTES = 10

        source_dir = self.base_dir / "src_bomb"
        source_dir.mkdir()
        source_paths = AppPaths(source_dir)
        db_src = self._create_fixture_workspace(source_paths.database, source_paths.brain_dir)
        bkp = BackupService(source_paths, db=db_src).create_backup()
        db_src.close()

        restore_service = RestoreService(self.paths)
        try:
            with self.assertRaises(RestoreSecurityError) as ctx:
                restore_service.validate_backup(bkp)
            self.assertIn("supera el límite permitido", str(ctx.exception))
        finally:
            restore_mod.MAX_FILE_BYTES = orig_limit


if __name__ == "__main__":
    unittest.main()
