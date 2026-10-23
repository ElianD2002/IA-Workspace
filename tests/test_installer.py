"""Pruebas automatizadas de instalación nativa de usuario (Fase 7C.2).

Verifica la instalación, idempotencia, actualización, desinstalación,
descubrimiento en PATH, resolución de recursos y preservación de datos
utilizando exclusivamente entornos HOME y XDG temporales y aislados.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class LocalInstallerTests(unittest.TestCase):
    """Conjunto de pruebas para el instalador nativo scripts/instalador-local.sh."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.installer_script = self.repo_root / "scripts" / "instalador-local.sh"

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.fake_home = Path(self.tmp_dir.name)
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.fake_home)
        self.env["XDG_DATA_HOME"] = str(self.fake_home / ".local" / "share")
        self.env["XDG_BIN_HOME"] = str(self.fake_home / ".local" / "bin")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _run_installer(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.installer_script), *args]
        return subprocess.run(
            cmd,
            env=self.env,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )

    def test_clean_installation_and_file_structure(self) -> None:
        """Comprueba que la instalación inicial genere exactamente los archivos y directorios esperados."""
        res = self._run_installer("install")
        self.assertEqual(res.returncode, 0, f"Error en install: {res.stderr}")

        bin_dir = self.fake_home / ".local" / "bin"
        apps_dir = self.fake_home / ".local" / "share" / "applications"
        icons_dir = self.fake_home / ".local" / "share" / "icons" / "hicolor"
        lib_dir = self.fake_home / ".local" / "share" / "ia-workspace-app"

        # 1. Comprobar ejecutables
        self.assertTrue((bin_dir / "ia-workspace").is_file())
        self.assertTrue((bin_dir / "iaw-restore-gui").is_file())
        self.assertTrue((bin_dir / "iaw-restore").is_file())
        self.assertTrue(os.access(bin_dir / "ia-workspace", os.X_OK))
        self.assertTrue(os.access(bin_dir / "iaw-restore-gui", os.X_OK))

        # 2. Comprobar lanzadores .desktop
        self.assertTrue((apps_dir / "io.github.elianmartinez.IAWorkspace.desktop").is_file())
        self.assertTrue((apps_dir / "io.github.elianmartinez.IAWorkspace.Restore.desktop").is_file())

        # Validar sintaxis con desktop-file-validate
        for desktop_file in apps_dir.glob("*.desktop"):
            val_res = subprocess.run(["desktop-file-validate", str(desktop_file)], capture_output=True)
            self.assertEqual(val_res.returncode, 0, f"Error validando {desktop_file.name}: {val_res.stderr.decode()}")

        # 3. Comprobar árbol de código y CSS
        self.assertTrue((lib_dir / "ia_workspace" / "__init__.py").is_file())
        self.assertTrue((lib_dir / "ia_workspace" / "ui" / "style.css").is_file())

        # 4. Comprobar iconos oficiales de ambas aplicaciones en todas las dimensiones
        sizes = [16, 24, 32, 48, 64, 128, 256, 512]
        for s in sizes:
            icon_p = icons_dir / f"{s}x{s}" / "apps" / "io.github.elianmartinez.IAWorkspace.png"
            restore_icon_p = icons_dir / f"{s}x{s}" / "apps" / "io.github.elianmartinez.IAWorkspace.Restore.png"
            self.assertTrue(icon_p.is_file(), f"Falta icono principal {s}x{s}")
            self.assertTrue(restore_icon_p.is_file(), f"Falta icono asistente {s}x{s}")
            self.assertNotEqual(icon_p.read_bytes(), restore_icon_p.read_bytes(), f"Iconos iguales en {s}x{s}")

    def test_installer_is_idempotent(self) -> None:
        """Comprueba que ejecutar la instalación dos veces sucesivas sea limpio y mantenga el estado."""
        res1 = self._run_installer("install")
        self.assertEqual(res1.returncode, 0)

        res2 = self._run_installer("install")
        self.assertEqual(res2.returncode, 0)

        res_status = self._run_installer("status")
        self.assertEqual(res_status.returncode, 0)
        self.assertIn("16 de 16 instalados", res_status.stdout)

    def test_installed_executables_help_and_runtime(self) -> None:
        """Comprueba que los binarios instalados respondan correctamente a invocación."""
        self._run_installer("install")
        bin_dir = self.fake_home / ".local" / "bin"

        # ia-workspace --help
        res_main = subprocess.run([str(bin_dir / "ia-workspace"), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(res_main.returncode, 0)
        self.assertIn("io.github.elianmartinez.IAWorkspace", res_main.stdout)

        # iaw-restore-gui --help
        res_gui = subprocess.run([str(bin_dir / "iaw-restore-gui"), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(res_gui.returncode, 0)
        self.assertIn("iaw-restore-gui", res_gui.stdout)

        # iaw-restore --help
        res_cli = subprocess.run([str(bin_dir / "iaw-restore"), "--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(res_cli.returncode, 0)
        self.assertIn("iaw-restore", res_cli.stdout)

    def test_data_preservation_on_uninstall(self) -> None:
        """Verifica que desinstalar limpie los binarios/recursos pero preserve intactos los datos de usuario."""
        self._run_installer("install")

        # Simular datos de usuario preexistentes
        user_data = self.fake_home / ".local" / "share" / "ia-workspace"
        user_data.mkdir(parents=True, exist_ok=True)
        (user_data / "ia-workspace.db").write_text("sqlite-user-data")
        (user_data / "brain").mkdir()
        (user_data / "brain" / "project_note.md").write_text("# Nota de usuario")
        (user_data / "backups").mkdir()
        (user_data / "backups" / "backup_2026.tar.gz").write_text("tar-backup-data")

        # Desinstalar
        res_uninst = self._run_installer("uninstall")
        self.assertEqual(res_uninst.returncode, 0)

        bin_dir = self.fake_home / ".local" / "bin"
        apps_dir = self.fake_home / ".local" / "share" / "applications"
        lib_dir = self.fake_home / ".local" / "share" / "ia-workspace-app"

        # Archivos de la aplicación eliminados
        self.assertFalse((bin_dir / "ia-workspace").exists())
        self.assertFalse((bin_dir / "iaw-restore-gui").exists())
        self.assertFalse((apps_dir / "io.github.elianmartinez.IAWorkspace.desktop").exists())
        self.assertFalse((apps_dir / "io.github.elianmartinez.IAWorkspace.Restore.desktop").exists())
        self.assertFalse(lib_dir.exists())

        # DATOS DE USUARIO PRESERVADOS
        self.assertTrue((user_data / "ia-workspace.db").is_file())
        self.assertEqual((user_data / "ia-workspace.db").read_text(), "sqlite-user-data")
        self.assertTrue((user_data / "brain" / "project_note.md").is_file())
        self.assertEqual((user_data / "brain" / "project_note.md").read_text(), "# Nota de usuario")
        self.assertTrue((user_data / "backups" / "backup_2026.tar.gz").is_file())
        self.assertEqual((user_data / "backups" / "backup_2026.tar.gz").read_text(), "tar-backup-data")

    def test_installer_blocks_when_active_app_lock_held(self) -> None:
        """Verifica que el instalador rechace actualizar o desinstalar si IA Workspace está en ejecución."""
        user_data = self.fake_home / ".local" / "share" / "ia-workspace"
        user_data.mkdir(parents=True, exist_ok=True)
        lock_file = user_data / ".app.lock"

        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = self._run_installer("install")
            self.assertEqual(res.returncode, 3)
            self.assertIn("IA Workspace se encuentra en ejecución", res.stderr)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_installer_blocks_when_restore_lock_held(self) -> None:
        """Verifica que el instalador rechace actualizar o desinstalar si el Asistente de Recuperación está activo."""
        user_data = self.fake_home / ".local" / "share" / "ia-workspace"
        user_data.mkdir(parents=True, exist_ok=True)
        lock_file = user_data / ".restore.lock"

        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            res = self._run_installer("install")
            self.assertEqual(res.returncode, 3)
            self.assertIn("Asistente de Recuperación se encuentra en ejecución", res.stderr)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_desktop_files_exec_works_with_minimal_gnome_path(self) -> None:
        """Comprueba que los Exec de los archivos .desktop funcionen con un PATH mínimo sin ~/.local/bin."""
        self._run_installer("install")
        apps_dir = self.fake_home / ".local" / "share" / "applications"

        minimal_env = dict(self.env)
        minimal_env["PATH"] = "/usr/bin:/bin"  # Simula sesión gráfica GNOME sin ~/.local/bin en PATH

        app_desktop = (apps_dir / "io.github.elianmartinez.IAWorkspace.desktop").read_text()
        for line in app_desktop.splitlines():
            if line.startswith("Exec="):
                cmd = line.split("=", 1)[1].strip().strip('"')
                res = subprocess.run([cmd, "--help"], env=minimal_env, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Fallo al ejecutar {cmd} con PATH mínimo")

        restore_desktop = (apps_dir / "io.github.elianmartinez.IAWorkspace.Restore.desktop").read_text()
        for line in restore_desktop.splitlines():
            if line.startswith("Exec="):
                cmd = line.split("=", 1)[1].split()[0].strip().strip('"')
                res = subprocess.run([cmd, "--help"], env=minimal_env, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, f"Fallo al ejecutar {cmd} con PATH mínimo")

    def test_alien_files_not_overwritten_or_deleted(self) -> None:
        """Verifica que la instalación y desinstalación no toque archivos ajenos en bin, applications ni icons."""
        bin_dir = self.fake_home / ".local" / "bin"
        apps_dir = self.fake_home / ".local" / "share" / "applications"
        icons_dir = self.fake_home / ".local" / "share" / "icons" / "hicolor" / "512x512" / "apps"

        bin_dir.mkdir(parents=True, exist_ok=True)
        apps_dir.mkdir(parents=True, exist_ok=True)
        icons_dir.mkdir(parents=True, exist_ok=True)

        alien_bin = bin_dir / "my-custom-tool"
        alien_bin.write_text("#!/bin/sh\necho custom")
        alien_bin.chmod(0o755)

        alien_desktop = apps_dir / "org.gnome.Calculator.desktop"
        alien_desktop.write_text("[Desktop Entry]\nName=Calculator")

        alien_icon = icons_dir / "org.gnome.Calculator.png"
        alien_icon.write_text("IMAGE_DATA")

        # Instalar
        res_inst = self._run_installer("install")
        self.assertEqual(res_inst.returncode, 0)
        self.assertEqual(alien_bin.read_text(), "#!/bin/sh\necho custom")
        self.assertEqual(alien_desktop.read_text(), "[Desktop Entry]\nName=Calculator")
        self.assertEqual(alien_icon.read_text(), "IMAGE_DATA")

        # Desinstalar
        res_uninst = self._run_installer("uninstall")
        self.assertEqual(res_uninst.returncode, 0)
        self.assertTrue(alien_bin.is_file(), "El ejecutable ajeno fue eliminado indebidamente")
        self.assertTrue(alien_desktop.is_file(), "El archivo .desktop ajeno fue eliminado")
        self.assertTrue(alien_icon.is_file(), "El icono ajeno fue eliminado")

    def test_preexisting_alien_file_in_exact_path_is_rejected(self) -> None:
        """Verifica que si existe un archivo ajeno en una ruta exacta (ej. binario o .desktop), se rechace la instalación."""
        bin_dir = self.fake_home / ".local" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        conflicting_bin = bin_dir / "ia-workspace"
        conflicting_bin.write_text("#!/bin/sh\necho alien-ia-workspace")
        conflicting_bin.chmod(0o755)

        # La instalación debe fallar y negarse a sobrescribirlo
        res = self._run_installer("install")
        self.assertEqual(res.returncode, 4)
        self.assertIn("ya existe y no pertenece a una instalación previa", res.stderr)
        self.assertEqual(conflicting_bin.read_text(), "#!/bin/sh\necho alien-ia-workspace")

        # Intentar desinstalar tampoco debe borrar el binario ajeno
        res_uninst = self._run_installer("uninstall")
        self.assertEqual(res_uninst.returncode, 0)
        self.assertTrue(conflicting_bin.is_file())
        self.assertEqual(conflicting_bin.read_text(), "#!/bin/sh\necho alien-ia-workspace")

    def test_failed_update_rollbacks_cleanly_and_preserves_previous_installation(self) -> None:
        """Verifica que si una actualización falla a mitad del proceso, se revierta la instalación previa completa."""
        # 1. Instalación inicial exitosa
        self.assertEqual(self._run_installer("install").returncode, 0)

        lib_dir = self.fake_home / ".local" / "share" / "ia-workspace-app"
        canary = lib_dir / "ia_workspace" / "version_canary.txt"
        canary.write_text("canary-v1")

        bin_file = self.fake_home / ".local" / "bin" / "ia-workspace"
        bin_content_v1 = bin_file.read_text()

        desktop_file = self.fake_home / ".local" / "share" / "applications" / "io.github.elianmartinez.IAWorkspace.desktop"
        desktop_content_v1 = desktop_file.read_text()

        user_db = self.fake_home / ".local" / "share" / "ia-workspace" / "ia-workspace.db"
        user_db.parent.mkdir(parents=True, exist_ok=True)
        user_db.write_text("active-user-db")

        # 2. Provocar un fallo durante la actualización (haciendo que applications sea de solo lectura)
        apps_dir = self.fake_home / ".local" / "share" / "applications"
        apps_dir.chmod(0o555)
        try:
            res_update = self._run_installer("install")
            self.assertNotEqual(res_update.returncode, 0)
        finally:
            apps_dir.chmod(0o755)

        # 3. Comprobar que la instalación previa sigue intacta con su canary, binarios y lanzadores
        self.assertTrue(canary.is_file(), "El archivo canary de la versión anterior no fue restaurado")
        self.assertEqual(canary.read_text(), "canary-v1")
        self.assertEqual(bin_file.read_text(), bin_content_v1)
        self.assertEqual(desktop_file.read_text(), desktop_content_v1)

        # 4. Comprobar que los datos de usuario siguen intactos
        self.assertTrue(user_db.is_file())
        self.assertEqual(user_db.read_text(), "active-user-db")


if __name__ == "__main__":
    unittest.main()
