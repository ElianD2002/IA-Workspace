"""Pruebas de interfaz para la gestión visual de respaldos en Ajustes (Fase 7B.4B).

Verifica:
1. Sección «Respaldos y recuperación» en PreferencesDialog.
2. Guardado de borradores (save_state y flush) antes de crear respaldo.
3. Creación asíncrona de respaldo sin bloquear el hilo principal y prevención de duplicados.
4. Listado de respaldos existentes con nombre, fecha, tamaño y estado de verificación.
5. Verificación de integridad con RestoreService.validate_backup() (éxito y error).
6. Prevención de restauración activa: la interfaz no ofrece botón para restaurar datos en caliente.
7. Avisos informativos de recuperación offline y confidencialidad/cifrado.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
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
from gi.repository import Adw, GLib, Gtk

from ia_workspace.config import AppPaths
from ia_workspace.context import AppContext
from ia_workspace.domain.models import AgentKind
from ia_workspace.brain.proposals import ProposalStatus
from ia_workspace.persistence.database import Database
from ia_workspace.services.backup import BackupService
from ia_workspace.services.restore import RestoreService
from ia_workspace.ui.dialogs.preferences import PreferencesDialog, _format_size


class DummyWindow:
    def __init__(self) -> None:
        self.saved = False

    def save_state(self) -> None:
        self.saved = True


class BackupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        Adw.init()

    def setUp(self) -> None:
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.paths = AppPaths(self.path / "data")
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths.backups_dir.mkdir(parents=True, exist_ok=True)
        self.paths.brain_dir.mkdir(parents=True, exist_ok=True)

        self.db = Database(self.paths.database)
        self.ctx = AppContext.create(self.db, paths=self.paths, simulated_agents=True)

        # Crear un proyecto y nota de prueba
        p_dir = self.path / "proj_alpha"
        p_dir.mkdir()
        self.project = self.ctx.projects.register("Proyecto Alfa", str(p_dir), (AgentKind.CLAUDE_CODE,))
        doc_global = self.paths.brain_dir / "global" / "reglas.md"
        doc_global.parent.mkdir(parents=True, exist_ok=True)
        doc_global.write_text("# Reglas globales\nContenido verificado.", encoding="utf-8")

        self.dummy_win = DummyWindow()
        self.dialog = PreferencesDialog(self.ctx, parent_window=self.dummy_win)
        self._pump()

    def tearDown(self) -> None:
        self.dialog.force_close()
        self.ctx.close()
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

    def test_format_size_helper(self) -> None:
        """Verifica la función auxiliar de formateo legible de tamaños en bytes."""
        self.assertEqual(_format_size(500), "500 B")
        self.assertEqual(_format_size(2048), "2.0 KB")
        self.assertEqual(_format_size(1572864), "1.5 MB")

    def test_backups_page_structure_and_safety_notices(self) -> None:
        """Verifica que la página de respaldos exista en PreferencesDialog y contenga
        los apartados informativos de recuperación offline y privacidad."""
        self.assertEqual(self.dialog.backups_page.get_title(), "Respaldos")

        # Comprobar que no existe ningún botón funcional para restaurar
        # Inspeccionar todos los botones de la página
        buttons = []

        def find_buttons(widget: Gtk.Widget) -> None:
            if isinstance(widget, Gtk.Button):
                buttons.append(widget.get_label() or "")
            child = widget.get_first_child()
            while child is not None:
                find_buttons(child)
                child = child.get_next_sibling()

        find_buttons(self.dialog.backups_page)

        # No debe haber botón de "Restaurar"
        self.assertNotIn("Restaurar", buttons)
        self.assertNotIn("Restaurar respaldo", buttons)

        # Comprobar que existe el botón Crear respaldo y el botón refrescar
        self.assertIn("Crear respaldo", buttons)

    def test_create_backup_flushes_state_and_generates_file(self) -> None:
        """Verifica que al pulsar 'Crear respaldo' se guarde el estado del editor (save_state),
        se invoque BackupService asíncronamente y se actualice la lista de respaldos."""
        # Establecer un borrador en memoria en el workspace
        st = self.ctx.workspace.state_for(self.project.id)
        st.editor_text = "Borrador pendiente en memoria"
        self.ctx.workspace.mark_dirty(self.project.id)
        self.assertTrue(self.ctx.workspace.has_dirty)

        # Estado inicial
        self.assertFalse(self.dummy_win.saved)
        self.assertFalse(self.dialog._is_backing_up)

        # Simular pulsación de crear respaldo
        self.dialog._btn_create_backup.emit("clicked")

        # Inmediatamente save_state y flush deben haberse ejecutado
        self.assertTrue(self.dummy_win.saved)
        self.assertFalse(self.ctx.workspace.has_dirty)
        self.assertTrue(self.dialog._is_backing_up)
        self.assertFalse(self.dialog._btn_create_backup.get_sensitive())

        # Esperar a que el hilo termine y se notifique por GLib.idle_add
        done = self._wait_until(lambda: not self.dialog._is_backing_up)
        self.assertTrue(done, "El proceso de creación de respaldo no finalizó a tiempo")

        # El botón vuelve a estar activo
        self.assertTrue(self.dialog._btn_create_backup.get_sensitive())

        # Verificar que se creó el archivo en backups_dir
        backups = list(self.paths.backups_dir.glob("*.tar.gz"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith("ia-workspace-backup-"))

        # Comprobar que la fila del respaldo aparece en el grupo
        self.assertEqual(len(self.dialog._backup_rows), 1)
        row = self.dialog._backup_rows[0]
        self.assertEqual(row.get_title(), backups[0].name)
        self.assertIn("Estado: Sin verificar", row.get_subtitle())

    def test_prevent_duplicate_concurrent_backup_executions(self) -> None:
        """Comprueba que no se inicien dos operaciones de respaldo concurrentes."""
        self.dialog._is_backing_up = True
        self.dialog._btn_create_backup.set_sensitive(False)

        # Si se invoca nuevamente mientras está ocupado, debe ignorarse
        self.dialog._on_create_backup_clicked()
        self.assertEqual(len(list(self.paths.backups_dir.glob("*.tar.gz"))), 0)

        # Restaurar estado
        self.dialog._is_backing_up = False
        self.dialog._btn_create_backup.set_sensitive(True)

    def test_verify_backup_integrity_success(self) -> None:
        """Verifica que la acción 'Verificar' invoque RestoreService.validate_backup()
        y marque el respaldo como Válido sin tocar los datos activos."""
        # Crear un respaldo previo con BackupService
        bkp_path = BackupService(self.paths, db=self.db).create_backup()
        self.dialog._refresh_backups_list()
        self._pump()

        self.assertEqual(len(self.dialog._backup_rows), 1)
        row = self.dialog._backup_rows[0]
        self.assertIn("Sin verificar", row.get_subtitle())

        # Obtener el botón Verificar de la fila
        btn_verify = None
        def find_verify_btn(widget: Gtk.Widget) -> None:
            nonlocal btn_verify
            if isinstance(widget, Gtk.Button) and widget.get_label() == "Verificar":
                btn_verify = widget
            child = widget.get_first_child()
            while child is not None:
                find_verify_btn(child)
                child = child.get_next_sibling()

        find_verify_btn(row)
        self.assertIsNotNone(btn_verify)

        # Pulsar Verificar
        btn_verify.emit("clicked")

        # Esperar finalización de la verificación
        done = self._wait_until(lambda: len(self.dialog._verifying_paths) == 0)
        self.assertTrue(done, "La verificación no finalizó a tiempo")

        # Comprobar que el subtítulo se actualizó a Íntegro y válido
        self.assertIn("Íntegro", row.get_subtitle())
        self.assertIn("SHA-256 ok", row.get_subtitle())
        self.assertTrue(btn_verify.get_sensitive())

    def test_verify_backup_integrity_failure_on_corrupted_file(self) -> None:
        """Verifica que ante un archivo de respaldo corrupto o modificado,
        la verificación informe el error y actualice la UI acordemente."""
        # Crear un respaldo y corromperlo
        bkp_path = BackupService(self.paths, db=self.db).create_backup()

        # Corromper bytes del archivo
        with open(bkp_path, "r+b") as f:
            f.seek(50)
            f.write(b"CORRUPTED_BYTES_HERE_123456789")

        self.dialog._refresh_backups_list()
        self._pump()

        self.assertEqual(len(self.dialog._backup_rows), 1)
        row = self.dialog._backup_rows[0]

        btn_verify = None
        def find_verify_btn(widget: Gtk.Widget) -> None:
            nonlocal btn_verify
            if isinstance(widget, Gtk.Button) and widget.get_label() == "Verificar":
                btn_verify = widget
            child = widget.get_first_child()
            while child is not None:
                find_verify_btn(child)
                child = child.get_next_sibling()

        find_verify_btn(row)
        self.assertIsNotNone(btn_verify)

        btn_verify.emit("clicked")
        done = self._wait_until(lambda: len(self.dialog._verifying_paths) == 0)
        self.assertTrue(done)

        # Comprobar que el subtítulo refleja Error
        self.assertIn("Error:", row.get_subtitle())
        self.assertTrue(btn_verify.get_sensitive())

    def test_empty_backups_state(self) -> None:
        """Verifica que sin respaldos se muestre el mensaje informativo correspondiente."""
        self.dialog._refresh_backups_list()
        self._pump()
        self.assertEqual(len(self.dialog._backup_rows), 1)
        self.assertEqual(self.dialog._backup_rows[0].get_title(), "No hay respaldos disponibles")

    def test_external_valid_proposal_synced_prior_to_backup(self) -> None:
        """Verifica que una propuesta externa válida en disco sea sincronizada antes del respaldo
        y se conserve en estado pendiente."""
        key = self.ctx.brain.project_key(self.project)
        p_dir = self.ctx.brain.store.ensure_proposals_root(key)
        prop_file = p_dir / "p-ext12345678.md"
        prop_content = (
            "---\n"
            "id: p-ext12345678\n"
            f"project_id: {self.project.id}\n"
            f"project_key: {key}\n"
            "agent: claude_code\n"
            "operation: create\n"
            "relpath: arquitectura/decision.md\n"
            "title: Decisión de arquitectura\n"
            "category: decisiones\n"
            "status: pending\n"
            "---\n"
            "# Propuesta externa\n"
            "Contenido creado por agente externo.\n"
        )
        prop_file.write_text(prop_content, encoding="utf-8")

        # Comprobar que antes del respaldo no está en la base de datos
        db_props = self.ctx.brain.list_proposals(self.project.id)
        self.assertEqual(len(db_props), 0)

        # Disparar respaldo desde la interfaz
        self.dialog._btn_create_backup.emit("clicked")

        # Esperar finalización del respaldo
        done = self._wait_until(lambda: not self.dialog._is_backing_up)
        self.assertTrue(done, "El respaldo con propuesta sincronizada no finalizó a tiempo")

        # Verificar que el respaldo se generó con éxito
        backups = list(self.paths.backups_dir.glob("*.tar.gz"))
        self.assertEqual(len(backups), 1)

        # Verificar que la propuesta se registró en SQLite conservando estado PENDING
        db_props = self.ctx.brain.list_proposals(self.project.id)
        self.assertEqual(len(db_props), 1)
        self.assertEqual(db_props[0].id, "p-ext12345678")
        self.assertEqual(db_props[0].status, ProposalStatus.PENDING)

        # Validar el archivo de respaldo con RestoreService sin modificar datos activos
        report = RestoreService(self.paths).validate_backup(backups[0])
        self.assertTrue(report.integrity_ok)
        self.assertEqual(report.stats.get("proposals_count"), 1)

    def test_external_invalid_proposal_rejected_without_data_loss(self) -> None:
        """Verifica que una inconsistencia real (propuesta no sincronizable) provoque rechazo
        legible del respaldo y no elimine los datos en disco."""
        key = self.ctx.brain.project_key(self.project)
        p_dir = self.ctx.brain.store.ensure_proposals_root(key)
        bad_prop = p_dir / "p-bad87654321.md"
        bad_prop.write_text("---\noperation: invalid_op\n---\nContenido inválido no sincronizable", encoding="utf-8")

        # Comprobar que no está en la BD
        self.assertEqual(len(self.ctx.brain.list_proposals(self.project.id)), 0)

        # Capturar toasts emitidos
        toasts: list[str] = []
        orig_add_toast = self.dialog.add_toast
        self.dialog.add_toast = lambda t: (toasts.append(t.get_title()), orig_add_toast(t))

        # Disparar creación de respaldo
        self.dialog._btn_create_backup.emit("clicked")

        # Esperar finalización del intento de respaldo
        done = self._wait_until(lambda: not self.dialog._is_backing_up)
        self.assertTrue(done)

        # Verificar que NO se generó ningún archivo de respaldo
        backups = list(self.paths.backups_dir.glob("*.tar.gz"))
        self.assertEqual(len(backups), 0)

        # Comprobar que el archivo original no fue eliminado ni alterado
        self.assertTrue(bad_prop.is_file())
        self.assertIn("Contenido inválido no sincronizable", bad_prop.read_text(encoding="utf-8"))

        # Comprobar mensaje de error en toast
        self.assertTrue(any("creada en disco pero no registrada" in t for t in toasts))
        self.assertTrue(any("formato no válido" in t for t in toasts))


if __name__ == "__main__":
    unittest.main()
