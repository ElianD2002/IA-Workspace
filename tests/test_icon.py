"""Icono oficial y lanzador de escritorio (sin display).

El lanzador se prueba instalándolo en un XDG_DATA_HOME temporal dentro de ``var/``:
nunca en la carpeta personal real.
"""

from __future__ import annotations

import configparser
import hashlib
import importlib.util
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import ROOT, temp_dir

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from ia_workspace import APP_ID, APP_NAME  # noqa: E402

SOURCE = ROOT / "data" / "icons" / "app-icon.png"
DESKTOP = ROOT / "data" / f"{APP_ID}.desktop"
LAUNCHER = ROOT / "scripts" / "desktop-launcher.sh"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def load_builder():
    spec = importlib.util.spec_from_file_location("build_icons", ROOT / "scripts" / "build_icons.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def read_desktop(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # las claves del .desktop distinguen mayúsculas
    parser.read(path, encoding="utf-8")
    return parser["Desktop Entry"]


class IconAssetTests(unittest.TestCase):
    def test_original_is_the_approved_png(self) -> None:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(SOURCE))
        self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (1254, 1254))
        self.assertTrue(pixbuf.get_has_alpha())

    def test_generated_sizes_are_up_to_date_and_faithful(self) -> None:
        builder = load_builder()
        self.assertEqual(builder.SIZES, SIZES)
        for size in SIZES:
            path = builder.target(size)
            self.assertEqual(path.name, f"{APP_ID}.png")
            self.assertEqual(path.read_bytes(), builder.render(size), f"{size}px desactualizado")
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (size, size))
            self.assertTrue(pixbuf.get_has_alpha(), f"{size}px perdió la transparencia")
            pixels, stride, channels = pixbuf.get_pixels(), pixbuf.get_rowstride(), pixbuf.get_n_channels()
            corner_alpha = pixels[3]
            center_alpha = pixels[(size // 2) * stride + (size // 2) * channels + 3]
            self.assertLess(corner_alpha, 32, f"{size}px: la esquina debería ser transparente")
            self.assertGreater(center_alpha, 200, f"{size}px: el centro debería ser opaco")

    def test_building_never_modifies_the_original(self) -> None:
        before = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
        self.assertEqual(load_builder().main(["--check"]), 0)
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), before)


class DesktopEntryTests(unittest.TestCase):
    def test_identity_is_coherent(self) -> None:
        entry = read_desktop(DESKTOP)
        self.assertEqual(DESKTOP.stem, APP_ID)  # GNOME Wayland asocia ventana y lanzador por este nombre
        self.assertEqual(entry["Name"], APP_NAME)
        self.assertEqual(entry["Icon"], APP_ID)
        self.assertEqual(entry["StartupWMClass"], APP_ID)
        self.assertEqual(entry["Type"], "Application")

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate no disponible")
    def test_validates(self) -> None:
        result = subprocess.run(["desktop-file-validate", str(DESKTOP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class LauncherInstallTests(unittest.TestCase):
    def run_launcher(self, data_home: str, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, XDG_DATA_HOME=data_home)
        return subprocess.run([str(LAUNCHER), *args], capture_output=True, text=True, env=env)

    def test_install_is_reversible_and_leaves_other_files_alone(self) -> None:
        with temp_dir() as data_home:
            base = Path(data_home)
            foreign = base / "applications" / "otra-app.desktop"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("[Desktop Entry]\nName=Otra\n")
            foreign_icon = base / "icons" / "hicolor" / "48x48" / "apps" / "otra-app.png"
            foreign_icon.parent.mkdir(parents=True)
            foreign_icon.write_bytes(b"png ajeno")

            first = self.run_launcher(data_home, "install")
            self.assertEqual(first.returncode, 0, first.stderr)
            installed = base / "applications" / f"{APP_ID}.desktop"
            entry = read_desktop(installed)
            self.assertEqual(entry["Exec"], f'"{ROOT}/scripts/run.sh"')
            self.assertEqual(entry["TryExec"], f"{ROOT}/scripts/run.sh")
            self.assertEqual(entry["Icon"], APP_ID)
            for size in SIZES:
                icon = base / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
                self.assertEqual(icon.read_bytes(), load_builder().target(size).read_bytes())

            again = self.run_launcher(data_home, "install")  # idempotente
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("8 de 8", self.run_launcher(data_home, "status").stdout)

            removed = self.run_launcher(data_home, "uninstall")
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(installed.exists())
            leftovers = [p for p in base.rglob("*") if p.is_file() and APP_ID in p.name]
            self.assertEqual(leftovers, [])
            self.assertTrue(foreign.is_file(), "se borró un lanzador ajeno")
            self.assertTrue(foreign_icon.is_file(), "se borró un icono ajeno")
            self.assertIn("0 de 8", self.run_launcher(data_home, "status").stdout)

    def test_rejects_unknown_command(self) -> None:
        with temp_dir() as data_home:
            result = self.run_launcher(data_home, "borrar-todo")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(list(Path(data_home).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
