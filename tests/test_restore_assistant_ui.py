"""Pruebas de interfaz para el asistente gráfico de recuperación offline (Fase 7B.5B).

Verifica:
1. Respaldo válido e inválido.
2. Cancelación sin modificar datos.
3. Rechazo con aplicación activa (.app.lock).
4. Recuperación confirmada ("RESTAURAR").
5. Base activa dañada (asistente sigue operativo e inspecciona/restaura).
6. Conservación de WebKit, respaldos y proyectos externos.
7. Prevención de operaciones duplicadas y bloqueo de cierre accidental durante la restauración.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

tests_dir = Path(__file__).resolve().parent
src_dir = tests_dir.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ia_workspace.config import AppPaths
from ia_workspace.domain.models import AgentKind, NewProject
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import ProjectRepository
from ia_workspace.services.backup import BackupService, sha256_file
from ia_workspace.ui.dialogs.restore_assistant import (
    APP_RESTORE_ID,
    RestoreAssistantApplication,
    RestoreAssistantWindow,
)


class RestoreAssistantUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        Adw.init()

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = AppPaths(self.data_dir)
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        self.paths.brain_dir.mkdir(parents=True, exist_ok=True)

        self.app = RestoreAssistantApplication(paths=self.paths)
        self.windows: list[RestoreAssistantWindow] = []

    def tearDown(self) -> None:
        for win in self.windows:
            try:
                win.destroy()
            except Exception:
                pass
        self._pump()
        self.tmp.cleanup()

    def _pump(self) -> None:
        while GLib.MainContext.default().iteration(False):
            pass

    def _wait_until(self, predicate, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while GLib.MainContext.default().iteration(False):
                pass
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def _create_synthetic_workspace(
        self,
        project_name: str = "Proyecto Test",
        doc_text: str = "# Test Doc\nContenido sintético.",
    ) -> tuple[Database, Path]:
        """Crea base SQLite, notas en brain_dir y proyecto sintético."""
        db = Database(self.paths.database)
        projects = ProjectRepository(db)
        p = projects.add(NewProject(project_name, str(self.base_dir / project_name), (AgentKind.CLAUDE_CODE,)))

        mem_key = "proy-test-123456789abc"
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
                "Nota Test",
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
        db.conn.commit()

        # Carpeta externa del proyecto
        ext_proj_dir = self.base_dir / project_name
        ext_proj_dir.mkdir(parents=True, exist_ok=True)
        (ext_proj_dir / "codigo.py").write_text("print('codigo externo intacto')", encoding="utf-8")

        return db, doc_path

    def _create_window(self, initial_backup: Path | None = None) -> RestoreAssistantWindow:
        win = RestoreAssistantWindow(app=None, paths=self.paths, initial_backup=initial_backup)
        self.windows.append(win)
        self._pump()
        return win

    def test_window_ui_elements_and_default_state(self) -> None:
        """Verifica que los elementos UI iniciales se carguen en estado seguro."""
        win = self._create_window()
        self.assertEqual(win.get_title(), "Recuperación de Respaldo — IA Workspace")
        self.assertFalse(win._btn_restore.get_sensitive())
        self.assertFalse(win._entry_confirm.get_sensitive())
        self.assertEqual(win._entry_confirm.get_text(), "")

    def test_valid_backup_inspection_and_confirmation_enabling(self) -> None:
        """Verifica que un respaldo válido habilite el campo de confirmación y muestre detalles."""
        db, _ = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        win = self._create_window(initial_backup=bkp)

        # Esperar validación asíncrona
        self.assertTrue(self._wait_until(lambda: win._validation_report is not None))
        self.assertTrue(win._entry_confirm.get_sensitive())
        self.assertIn("1 proyecto", win._row_items.get_subtitle())
        self.assertIn("CST", win._row_date.get_subtitle())

        # El botón de restaurar sigue deshabilitado hasta escribir RESTAURAR
        self.assertFalse(win._btn_restore.get_sensitive())

        win._entry_confirm.set_text("restaurar")
        self._pump()
        self.assertFalse(win._btn_restore.get_sensitive())

        win._entry_confirm.set_text("RESTAURAR")
        self._pump()
        self.assertTrue(win._btn_restore.get_sensitive())

    def test_invalid_backup_rejected_and_cannot_restore(self) -> None:
        """Verifica que un archivo corrupto sea detectado como inválido y bloquee la confirmación."""
        db, _ = self._create_synthetic_workspace()
        db.conn.close()

        corrupted = self.paths.backups_dir / "corrupted.tar.gz"
        corrupted.write_bytes(b"CABECERA_TAR_TOTALMENTE_INVALIDA")

        win = self._create_window(initial_backup=corrupted)

        # Esperar a que la verificación falle
        self.assertTrue(self._wait_until(lambda: "corrupto" in win._row_items.get_subtitle()))
        self.assertIsNone(win._validation_report)
        self.assertFalse(win._entry_confirm.get_sensitive())
        self.assertFalse(win._btn_restore.get_sensitive())

    def test_cancel_does_not_modify_any_files(self) -> None:
        """Verifica que salir o cancelar no toque ningún archivo en disco."""
        db, doc_path = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        initial_db_bytes = self.paths.database.read_bytes()
        initial_doc_text = doc_path.read_text(encoding="utf-8")

        win = self._create_window(initial_backup=bkp)
        self.assertTrue(self._wait_until(lambda: win._validation_report is not None))

        # Pulsar salir
        win._btn_cancel.emit("clicked")
        self._pump()

        self.assertEqual(self.paths.database.read_bytes(), initial_db_bytes)
        self.assertEqual(doc_path.read_text(encoding="utf-8"), initial_doc_text)

    def test_restore_rejected_when_application_is_active(self) -> None:
        """Verifica que si IA Workspace mantiene .app.lock abierto, se impida la restauración."""
        db, _ = self._create_synthetic_workspace()
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # Simular aplicación principal activa reteniendo .app.lock
        app_lock = self.data_dir / ".app.lock"
        fd = os.open(app_lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            win = self._create_window(initial_backup=bkp)
            self.assertTrue(self._wait_until(lambda: win._validation_report is not None))

            win._entry_confirm.set_text("RESTAURAR")
            self._pump()
            self.assertTrue(win._btn_restore.get_sensitive())

            # Intentar restaurar
            win._btn_restore.emit("clicked")
            self._pump()

            # Esperar a que regrese del hilo
            self.assertTrue(self._wait_until(lambda: not win._is_working))
            # La ventana no debe haberse cerrado y debe volver a estar en estado seguro
            self.assertFalse(win._is_working)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_confirmed_restoration_success_and_preservation(self) -> None:
        """Verifica recuperación atómica confirmada y preservación de WebKit y proyectos externos."""
        # 1. Crear estado inicial con un proyecto A
        db, _ = self._create_synthetic_workspace("Proyecto Original", "# Documento Original")
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # 2. Agregar archivos locales que deben conservarse
        webkit_dir = self.data_dir / "webkit" / "Default"
        webkit_dir.mkdir(parents=True, exist_ok=True)
        cookies_file = webkit_dir / "Cookies"
        cookies_file.write_bytes(b"WEBKIT_LOCAL_SESSION_DATA")

        other_bkp = self.paths.backups_dir / "respaldo_archivado.tar.gz"
        other_bkp.write_bytes(b"DUMMY_BACKUP_TAR")

        ext_code = self.base_dir / "Proyecto Original" / "codigo.py"

        # 3. Mutar la base activa (simulando estado modificado o dañado posteriormente)
        db2 = Database(self.paths.database)
        projects2 = ProjectRepository(db2)
        projects2.add(NewProject("Proyecto B Nuevo", str(self.base_dir / "otro"), (AgentKind.CLAUDE_CODE,)))
        db2.conn.close()

        win = self._create_window(initial_backup=bkp)
        self.assertTrue(self._wait_until(lambda: win._validation_report is not None))

        win._entry_confirm.set_text("RESTAURAR")
        self._pump()

        win._btn_restore.emit("clicked")
        self._pump()

        # Esperar finalización de la restauración
        self.assertTrue(self._wait_until(lambda: not win._is_working))

        # 4. Verificar que se recuperó el estado original exacto
        db_restored = Database(self.paths.database)
        projects_restored = ProjectRepository(db_restored)
        proj_list = projects_restored.list()
        self.assertEqual(len(proj_list), 1)
        self.assertEqual(proj_list[0].name, "Proyecto Original")
        db_restored.conn.close()

        # 5. Verificar preservación estricta de WebKit, otros respaldos y archivos externos
        self.assertTrue(cookies_file.exists())
        self.assertEqual(cookies_file.read_bytes(), b"WEBKIT_LOCAL_SESSION_DATA")
        self.assertTrue(other_bkp.exists())
        self.assertEqual(other_bkp.read_bytes(), b"DUMMY_BACKUP_TAR")
        self.assertTrue(ext_code.exists())
        self.assertEqual(ext_code.read_text(encoding="utf-8"), "print('codigo externo intacto')")

    def test_assistant_operates_with_damaged_active_database(self) -> None:
        """Verifica que el asistente inicie y permita restaurar incluso con la base SQLite activa rota."""
        # 1. Crear respaldo válido
        db, _ = self._create_synthetic_workspace("Proyecto Resiliente")
        backup_svc = BackupService(self.paths, db=db)
        bkp = backup_svc.create_backup()
        db.conn.close()

        # 2. Corromper deliberadamente la base SQLite activa
        self.paths.database.write_bytes(b"CORRUPTED_DATABASE_CRASH_HEADER_000000")

        # 3. El asistente debe iniciar sin crash
        win = self._create_window(initial_backup=bkp)
        self.assertTrue(self._wait_until(lambda: win._validation_report is not None))

        win._entry_confirm.set_text("RESTAURAR")
        self._pump()

        win._btn_restore.emit("clicked")
        self._pump()

        self.assertTrue(self._wait_until(lambda: not win._is_working))

        # 4. Tras la restauración, la base activa vuelve a ser un SQLite válido
        db_fixed = Database(self.paths.database)
        projects_fixed = ProjectRepository(db_fixed)
        self.assertEqual(len(projects_fixed.list()), 1)
        self.assertEqual(projects_fixed.list()[0].name, "Proyecto Resiliente")
        db_fixed.conn.close()

    def test_close_request_prevented_during_restoration(self) -> None:
        """Verifica que la ventana bloquee el cierre durante la ejecución transaccional."""
        win = self._create_window()
        win._is_working = True  # Simular proceso activo

        close_blocked = win._on_close_request(win)
        self.assertTrue(close_blocked)

        win._is_working = False
        close_allowed = win._on_close_request(win)
        self.assertFalse(close_allowed)

    def test_prevent_duplicate_operations(self) -> None:
        """Verifica que no se puedan disparar validaciones ni restauraciones concurrentes mientras _is_working es True."""
        win = self._create_window()
        win._selected_backup = self.paths.backups_dir / "dummy.tar.gz"
        win._validation_report = None
        win._entry_confirm.set_text("RESTAURAR")

        # Marcar en ejecución
        win._set_working_state(True)
        self.assertTrue(win._is_working)
        self.assertFalse(win._btn_restore.get_sensitive())
        self.assertFalse(win._btn_browse.get_sensitive())
        self.assertFalse(win._btn_validate.get_sensitive())
        self.assertFalse(win._btn_cancel.get_sensitive())

        # Disparar _on_restore_clicked no debe hacer nada si _is_working es True
        # Probamos llamándolo directamente
        win._on_restore_clicked(win._btn_restore)
        # El estado de los controles sigue protegido
        self.assertFalse(win._btn_restore.get_sensitive())


if __name__ == "__main__":
    unittest.main()
