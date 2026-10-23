"""Pruebas para la herramienta CLI de restauración offline (iaw-restore, Fase 7B.5A).

Verifica:
1. Listado y validación de respaldos sin modificar ningún dato.
2. Confirmación cancelada por el usuario: ningún archivo modificado.
3. Respaldo inválido o dañado: restauración rechazada antes de aplicar cambios.
4. Aplicación activa (.app.lock retenido): restauración rechazada con código de error.
5. Restauración confirmada ("RESTAURAR"): recuperación atómica de datos sintéticos.
6. Preservación estricta de WebKitGTK, respaldos existentes y proyectos externos.
7. Errores y códigos de salida apropiados.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

tests_dir = Path(__file__).resolve().parent
if str(tests_dir) not in sys.path:
    sys.path.insert(0, str(tests_dir))

SRC_DIR = tests_dir.parent / "src"

from helpers import temp_dir

from ia_workspace.config import AppPaths
from ia_workspace.domain.models import AgentKind, NewProject
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.restore_cli import cmd_list, format_size, main, resolve_backup_file
from ia_workspace.services.backup import BackupService, sha256_file


class RestoreCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_dir()
        self.base_dir = Path(self.tmp.name)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(self.data_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_synthetic_workspace(
        self,
        project_name: str = "Proyecto Alfa",
        doc_text: str = "# Alfa\nContenido sintético original.",
    ) -> tuple[Database, Path]:
        """Crea base SQLite, notas en brain_dir y proyecto sintético."""
        db = Database(self.paths.database)
        projects = ProjectRepository(db)
        p = projects.add(NewProject(project_name, str(self.base_dir / project_name), (AgentKind.CLAUDE_CODE,)))

        mem_key = "proy-alfa-123456789abc"
        db.conn.execute(
            "INSERT INTO brain_projects (project_id, memory_key, created_at) VALUES (?, ?, ?)",
            (p.id, mem_key, "2026-10-23T00:00:00Z"),
        )

        proj_dir = self.paths.brain_dir / "projects" / mem_key / "doc"
        proj_dir.mkdir(parents=True, exist_ok=True)
        doc_path = proj_dir / "nota.md"
        doc_path.write_text(doc_text, encoding="utf-8")
        h = sha256_file(doc_path)

        db.conn.execute(
            """
            INSERT INTO brain_documents (
                scope, project_key, relpath, title, category, module, tags, summary, size, mtime_ns, sha256, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "project",
                mem_key,
                "doc/nota.md",
                "Nota Alfa",
                "doc",
                "",
                "",
                "Resumen",
                doc_path.stat().st_size,
                doc_path.stat().st_mtime_ns,
                h,
                "2026-10-23T00:00:00Z",
            ),
        )

        # Crear plantilla sintética
        db.conn.execute(
            """
            INSERT INTO prompt_templates (
                name, description, category, scope, project_id, content, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Plantilla 1",
                "Descripción plantilla",
                "General",
                "global",
                None,
                "Contenido de prompt",
                "2026-10-23T00:00:00Z",
                "2026-10-23T00:00:00Z",
            ),
        )
        db.conn.commit()

        # Crear archivo en el proyecto externo
        ext_proj_dir = self.base_dir / project_name
        ext_proj_dir.mkdir(parents=True, exist_ok=True)
        (ext_proj_dir / "codigo.py").write_text("print('código externo')", encoding="utf-8")

        return db, doc_path

    def test_format_size(self) -> None:
        self.assertEqual(format_size(100), "100 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")
        self.assertEqual(format_size(1024 * 1024 * 1024), "1.0 GB")

    def test_resolve_backup_file(self) -> None:
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        test_file = self.paths.backups_dir / "backup-test.tar.gz"
        test_file.write_bytes(b"dummy")

        # Resuelve por nombre relativo en backups_dir
        res = resolve_backup_file(self.paths, "backup-test.tar.gz")
        self.assertEqual(res, test_file.resolve())

        # Resuelve por ruta absoluta
        res_abs = resolve_backup_file(self.paths, str(test_file))
        self.assertEqual(res_abs, test_file.resolve())

    def test_list_backups_empty_and_populated(self) -> None:
        """Verifica que el comando list liste adecuadamente y no modifique nada."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.data_dir), "list"])
        self.assertEqual(code, 0)
        self.assertIn("No existe el directorio de respaldos", out.getvalue())

        # Crear respaldo sintético
        db, _ = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.data_dir), "list"])
        self.assertEqual(code, 0)
        self.assertIn(bkp.name, out.getvalue())
        self.assertIn("Total: 1 respaldo(s)", out.getvalue())

    def test_info_and_validate_subcommands_do_not_modify_data(self) -> None:
        """Verifica que info y validate lean y verifiquen el respaldo sin alterar datos."""
        db, doc_path = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        initial_db_bytes = self.paths.database.read_bytes()
        initial_doc_text = doc_path.read_text(encoding="utf-8")

        # 1. info
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.data_dir), "info", bkp.name])
        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("Información del respaldo:", output)
        self.assertIn("Proyectos registrados:      1", output)
        self.assertIn("Integridad:        Válida", output)

        # 2. validate
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--data-dir", str(self.data_dir), "validate", bkp.name])
        self.assertEqual(code, 0)
        self.assertIn("VÁLIDO", out.getvalue())

        # Verificar que los datos no cambiaron
        self.assertEqual(self.paths.database.read_bytes(), initial_db_bytes)
        self.assertEqual(doc_path.read_text(encoding="utf-8"), initial_doc_text)

    def test_query_commands_work_with_damaged_active_database(self) -> None:
        """Verifica que list, info y validate funcionen incluso si la base SQLite activa está dañada."""
        db, _ = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # Dañar intencionadamente la base de datos activa con bytes aleatorios
        self.paths.database.write_bytes(b"CORRUPTED_SQLITE_HEADER_GARBAGE_1234567890")

        # 1. list debe funcionar sin errores
        out_list = io.StringIO()
        with contextlib.redirect_stdout(out_list):
            code_list = main(["--data-dir", str(self.data_dir), "list"])
        self.assertEqual(code_list, 0)
        self.assertIn(bkp.name, out_list.getvalue())

        # 2. validate debe validar el respaldo de forma aislada sin fallar
        out_val = io.StringIO()
        with contextlib.redirect_stdout(out_val):
            code_val = main(["--data-dir", str(self.data_dir), "validate", bkp.name])
        self.assertEqual(code_val, 0)
        self.assertIn("VÁLIDO", out_val.getvalue())

        # 3. info debe reportar los metadatos sin fallar
        out_info = io.StringIO()
        with contextlib.redirect_stdout(out_info):
            code_info = main(["--data-dir", str(self.data_dir), "info", bkp.name])
        self.assertEqual(code_info, 0)
        self.assertIn("Integridad:        Válida", out_info.getvalue())

    def test_restore_canceled_by_user_no_files_modified(self) -> None:
        """Verifica que si el usuario cancela la confirmación escrita, ningún archivo se modifica."""
        db, doc_path = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        initial_db_bytes = self.paths.database.read_bytes()
        initial_doc_text = doc_path.read_text(encoding="utf-8")

        # Simular respuestas negativas o cancelaciones
        for cancel_input in ["", "cancelar\n", "no\n", "s\n", "restaurar\n"]:
            with patch("sys.stdin", io.StringIO(cancel_input)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(["--data-dir", str(self.data_dir), "restore", bkp.name])
                self.assertEqual(code, 1)
                self.assertIn("cancelada por el usuario", out.getvalue())
                self.assertEqual(self.paths.database.read_bytes(), initial_db_bytes)
                self.assertEqual(doc_path.read_text(encoding="utf-8"), initial_doc_text)

    def test_restore_invalid_backup_rejected(self) -> None:
        """Verifica que un archivo de respaldo corrupto sea rechazado con código de error."""
        db, _ = self._create_synthetic_workspace()
        db.conn.close()
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        corrupted = self.paths.backups_dir / "corrupted.tar.gz"
        corrupted.write_bytes(b"esto no es un archivo tar valido")

        initial_db_bytes = self.paths.database.read_bytes()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.data_dir), "restore", corrupted.name])
        self.assertEqual(code, 1)
        self.assertIn("[ERROR DE VALIDACIÓN]", err.getvalue())
        self.assertEqual(self.paths.database.read_bytes(), initial_db_bytes)

    def test_restore_rejected_when_application_is_active(self) -> None:
        """Verifica que si IA Workspace mantiene .app.lock abierto, la restauración sea rechazada."""
        db, _ = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # Simular que la aplicación GTK está abierta adquiriendo .app.lock
        app_lock = self.data_dir / ".app.lock"
        fd = os.open(app_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with patch("sys.stdin", io.StringIO("RESTAURAR\n")):
                out = io.StringIO()
                err = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(["--data-dir", str(self.data_dir), "restore", bkp.name])
                self.assertEqual(code, 1)
                self.assertIn("[ERROR DE EXCLUSIVIDAD]", err.getvalue())
                self.assertIn("la aplicación se encuentra en ejecución", err.getvalue())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_confirmed_restoration_success(self) -> None:
        """Verifica que con confirmación explícita 'RESTAURAR' se recuperen fielmente los datos."""
        # 1. Crear estado inicial y respaldarlo
        db, _ = self._create_synthetic_workspace("Proyecto Original", "# Documento Original")
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # 2. Mutar el estado activo (simulando modificaciones o daño posterior)
        db2 = Database(self.paths.database)
        projects2 = ProjectRepository(db2)
        projects2.add(NewProject("Proyecto Modificado", str(self.base_dir / "otro"), (AgentKind.CLAUDE_CODE,)))
        # Eliminar el documento de brain
        mem_key = "proy-alfa-123456789abc"
        doc_file = self.paths.brain_dir / "projects" / mem_key / "doc" / "nota.md"
        doc_file.write_text("# Contenido Corrupto", encoding="utf-8")
        db2.conn.close()

        # 3. Ejecutar restauración con confirmación 'RESTAURAR'
        with patch("sys.stdin", io.StringIO("RESTAURAR\n")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--data-dir", str(self.data_dir), "restore", bkp.name])

        self.assertEqual(code, 0)
        self.assertIn("RESTAURACIÓN COMPLETADA CON ÉXITO", out.getvalue())

        # 4. Comprobar que los datos activos fueron recuperados fielmente
        db_restored = Database(self.paths.database)
        projects_restored = ProjectRepository(db_restored)
        proj_list = projects_restored.list()
        self.assertEqual(len(proj_list), 1)
        self.assertEqual(proj_list[0].name, "Proyecto Original")
        self.assertEqual(doc_file.read_text(encoding="utf-8"), "# Documento Original")
        db_restored.conn.close()

    def test_preserves_webkit_backups_and_external_projects(self) -> None:
        """Verifica la preservación estricta de WebKit, otros respaldos y carpetas externas."""
        db, _ = self._create_synthetic_workspace("Proyecto Conservado")
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # Simular datos locales que no deben ser tocados
        webkit_dir = self.data_dir / "webkit" / "Default"
        webkit_dir.mkdir(parents=True, exist_ok=True)
        cookies_file = webkit_dir / "Cookies"
        cookies_file.write_bytes(b"DATOS_WEBKIT_COOKIES_123")

        other_bkp = self.paths.backups_dir / "otro_respaldo.tar.gz"
        other_bkp.write_bytes(b"OTRO_RESPALDO_DUMMY")

        ext_file = self.base_dir / "Proyecto Conservado" / "codigo.py"
        ext_file.write_text("código preservado intacto", encoding="utf-8")

        # Restaurar
        with patch("sys.stdin", io.StringIO("RESTAURAR\n")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--data-dir", str(self.data_dir), "restore", bkp.name])
        self.assertEqual(code, 0)

        # Comprobar preservación
        self.assertTrue(cookies_file.exists())
        self.assertEqual(cookies_file.read_bytes(), b"DATOS_WEBKIT_COOKIES_123")
        self.assertTrue(other_bkp.exists())
        self.assertEqual(other_bkp.read_bytes(), b"OTRO_RESPALDO_DUMMY")
        self.assertTrue(ext_file.exists())
        self.assertEqual(ext_file.read_text(encoding="utf-8"), "código preservado intacto")

    def test_nonexistent_backup_file_returns_error_code(self) -> None:
        """Verifica que solicitar un archivo inexistente retorne código 1 y mensaje claro."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.data_dir), "info", "no-existe.tar.gz"])
        self.assertEqual(code, 1)
        self.assertIn("no se encontró el archivo de respaldo", err.getvalue())

    def test_bash_script_wrapper_execution(self) -> None:
        """Verifica que el script scripts/iaw-restore se pueda invocar directamente."""
        script_path = tests_dir.parent / "scripts" / "iaw-restore"
        self.assertTrue(script_path.is_file())
        self.assertTrue(os.access(script_path, os.X_OK))

        res = subprocess.run(
            [str(script_path), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("iaw-restore", res.stdout)
        self.assertIn("Herramienta segura de recuperación", res.stdout)


if __name__ == "__main__":
    unittest.main()
