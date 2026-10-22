"""Pruebas de Catppuccin (Latte y Mocha) y Copiar al seleccionar estilo Ghostty en VTE.

Verifica:
- Paletas oficiales Catppuccin Mocha y Latte para UI y terminales VTE (16 colores ANSI).
- Cambio dinámico de tema entre claro (Latte) y oscuro (Mocha).
- Copiar al seleccionar (Ghostty style) al soltar el ratón tras una selección.
- Notificación discreta de copiado con icono y texto.
- Desactivación y activación de «Copiar al seleccionar» y persistencia en configuración.
- Prevención de copias duplicadas o con selección vacía.
- Concurrencia e independencia entre múltiples terminales.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from helpers import temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Vte  # noqa: E402

from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions.models import SessionSpec
from ia_workspace.terminal.backend import COPY_ON_SELECT_KEY, VteTerminalHandle
from ia_workspace.terminal import theme as term_theme
from ia_workspace.ui import theme as ui_theme


class DummySink:
    def state_changed(self, state, detail: str = "", code: int | None = None) -> None:
        pass

    def append_output(self, text: str) -> None:
        pass


class MockSettings:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.data[key] = str(value)


HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


class CatppuccinThemeTests(unittest.TestCase):
    """Verifica las paletas oficiales Catppuccin y el generador CSS."""

    def test_official_palette_values(self) -> None:
        # Mocha
        self.assertEqual(ui_theme.MOCHA["base"], "#1e1e2e")
        self.assertEqual(ui_theme.MOCHA["text"], "#cdd6f4")
        self.assertEqual(ui_theme.MOCHA["blue"], "#89b4fa")
        self.assertEqual(ui_theme.MOCHA["green"], "#a6e3a1")
        self.assertEqual(ui_theme.MOCHA["red"], "#f38ba8")
        self.assertEqual(ui_theme.MOCHA["yellow"], "#f9e2af")
        self.assertEqual(ui_theme.MOCHA["surface0"], "#313244")

        # Latte
        self.assertEqual(ui_theme.LATTE["base"], "#eff1f5")
        self.assertEqual(ui_theme.LATTE["text"], "#4c4f69")
        self.assertEqual(ui_theme.LATTE["blue"], "#1e66f5")
        self.assertEqual(ui_theme.LATTE["green"], "#40a02b")
        self.assertEqual(ui_theme.LATTE["red"], "#d20f39")
        self.assertEqual(ui_theme.LATTE["yellow"], "#df8e1d")
        self.assertEqual(ui_theme.LATTE["surface0"], "#ccd0da")

    def test_vte_ansi_palettes(self) -> None:
        self.assertEqual(len(term_theme.MOCHA_ANSI), 16)
        self.assertEqual(len(term_theme.LATTE_ANSI), 16)
        # Primeros 8 colores ANSI estándar
        self.assertEqual(term_theme.MOCHA_ANSI[0], "#45475a")  # Black
        self.assertEqual(term_theme.MOCHA_ANSI[1], "#f38ba8")  # Red
        self.assertEqual(term_theme.MOCHA_ANSI[2], "#a6e3a1")  # Green
        self.assertEqual(term_theme.MOCHA_ANSI[4], "#89b4fa")  # Blue

        self.assertEqual(term_theme.LATTE_ANSI[0], "#5c5f77")  # Black
        self.assertEqual(term_theme.LATTE_ANSI[1], "#d20f39")  # Red
        self.assertEqual(term_theme.LATTE_ANSI[2], "#40a02b")  # Green
        self.assertEqual(term_theme.LATTE_ANSI[4], "#1e66f5")  # Blue

    def test_css_generation_mocha_and_latte(self) -> None:
        mocha_css = ui_theme.get_catppuccin_css(dark=True)
        self.assertIn("@define-color window_bg_color #1e1e2e;", mocha_css)
        self.assertIn("@define-color accent_color #89b4fa;", mocha_css)
        self.assertIn("--accent-color: #89b4fa;", mocha_css)

        latte_css = ui_theme.get_catppuccin_css(dark=False)
        self.assertIn("@define-color window_bg_color #eff1f5;", latte_css)
        self.assertIn("@define-color accent_color #1e66f5;", latte_css)
        self.assertIn("--accent-color: #1e66f5;", latte_css)

    @unittest.skipUnless(HAS_DISPLAY, "requiere display")
    def test_dynamic_terminal_theme_application(self) -> None:
        terminal = Vte.Terminal()
        # Aplicar Mocha
        term_theme.apply(terminal, dark=True)
        # Aplicar Latte sin error ni interrupción
        term_theme.apply(terminal, dark=False)


@unittest.skipUnless(HAS_DISPLAY, "requiere display y VTE")
class CopyOnSelectTests(unittest.TestCase):
    """Comprueba el copiado al seleccionar estilo Ghostty y las notificaciones."""

    def test_copy_on_select_default_enabled(self) -> None:
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                self.assertTrue(handle.is_copy_on_select_enabled())
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_copy_on_select_persisted_setting(self) -> None:
        settings = MockSettings()
        settings.set(COPY_ON_SELECT_KEY, "false")
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args), settings=settings)
            try:
                self.assertFalse(handle.is_copy_on_select_enabled())
                # Activar mediante método
                handle.set_copy_on_select(True)
                self.assertTrue(handle.is_copy_on_select_enabled())
                self.assertEqual(settings.get(COPY_ON_SELECT_KEY), "true")
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_copy_on_select_context_menu_action(self) -> None:
        settings = MockSettings()
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args), settings=settings)
            try:
                self.assertTrue(handle._act_copy_on_select.get_state().get_boolean())
                # Simular conmutación en el menú
                handle._act_copy_on_select.change_state(GLib.Variant.new_boolean(False))
                self.assertFalse(handle.is_copy_on_select_enabled())
                self.assertEqual(settings.get(COPY_ON_SELECT_KEY), "false")
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_revealer_notification_lifecycle(self) -> None:
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                revealer = handle.copy_revealer()
                self.assertIsInstance(revealer, Gtk.Widget)
                self.assertFalse(revealer.get_reveal_child())

                # Disparar notificación
                handle.show_copy_notification()
                self.assertTrue(revealer.get_reveal_child())

                # Ocultar
                handle._hide_copy_notification()
                self.assertFalse(revealer.get_reveal_child())
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_copy_selection_triggers_notification(self) -> None:
        with temp_dir() as folder:
            w = Gtk.Window()
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            w.set_child(handle.widget())
            w.present()
            try:
                notified = []
                handle.add_copy_listener(lambda: notified.append(True))

                # Sin selección: copy_selection() devuelve False y no notifica
                res = handle.copy_selection()
                self.assertFalse(res)
                self.assertEqual(len(notified), 0)

                # Con selección simulada (interceptando selected_text y has_selection)
                handle._terminal.get_has_selection = lambda: True
                handle.selected_text = lambda: "Texto copiado con éxito"

                res = handle.copy_selection()
                self.assertTrue(res)
                self.assertEqual(len(notified), 1)
                self.assertTrue(handle.copy_revealer().get_reveal_child())
            finally:
                handle.dispose()
                handle.stop(force=True)
                w.set_child(None)
                w.destroy()

    def test_maybe_copy_on_select_prevents_duplicate_and_empty(self) -> None:
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                notified_count = 0
                def on_copy():
                    nonlocal notified_count
                    notified_count += 1
                handle.add_copy_listener(on_copy)

                # 1. Selección vacía o whitespace -> no copia
                handle._terminal.get_has_selection = lambda: True
                handle.selected_text = lambda: "   \n  "
                handle._maybe_copy_on_select()
                self.assertEqual(notified_count, 0)

                # 2. Selección con texto válido -> copia
                handle.selected_text = lambda: "git status"
                handle._maybe_copy_on_select()
                self.assertEqual(notified_count, 1)

                # 3. Mismo texto sin cambio -> no duplica copia
                handle._maybe_copy_on_select()
                self.assertEqual(notified_count, 1)

                # 4. Deseleccionar -> resetea _last_copied_text
                handle._terminal.get_has_selection = lambda: False
                handle._on_selection_changed(handle._terminal)
                self.assertIsNone(handle._last_copied_text)

                # 5. Volver a seleccionar el mismo texto -> copia nuevamente
                handle._terminal.get_has_selection = lambda: True
                handle.selected_text = lambda: "git status"
                handle._maybe_copy_on_select()
                self.assertEqual(notified_count, 2)
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_copy_on_select_disabled_does_not_autocopy(self) -> None:
        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                handle.set_copy_on_select(False)
                notified = []
                handle.add_copy_listener(lambda: notified.append(True))

                handle._terminal.get_has_selection = lambda: True
                handle.selected_text = lambda: "texto ignorado"
                handle._maybe_copy_on_select()
                self.assertEqual(len(notified), 0)

                # Pero manual sí funciona
                handle.copy_selection()
                self.assertEqual(len(notified), 1)
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_multiple_terminals_independent_state(self) -> None:
        with temp_dir() as folder:
            spec1 = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="term1", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            spec2 = SessionSpec(project_id=2, kind=AgentKind.SHELL, title="term2", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            h1 = VteTerminalHandle(spec1, DummySink(), list(spec1.args))
            h2 = VteTerminalHandle(spec2, DummySink(), list(spec2.args))
            try:
                self.assertNotEqual(h1.copy_revealer(), h2.copy_revealer())
                h1.show_copy_notification()
                self.assertTrue(h1.copy_revealer().get_reveal_child())
                self.assertFalse(h2.copy_revealer().get_reveal_child())
            finally:
                h1.dispose()
                h2.dispose()
                h1.stop(force=True)
                h2.stop(force=True)

    def test_copy_notification_visual_integration(self) -> None:
        """Verifica que el aviso se integra correctamente al árbol visual de GTK sobre VTE."""
        with temp_dir() as folder:
            w = Gtk.Window()
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="term_visual", cwd=folder, args=("/bin/sh", "-c", "sleep 5"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                host = Gtk.ScrolledWindow(child=handle.widget(), vexpand=True, hexpand=True)
                overlay = Gtk.Overlay(vexpand=True, hexpand=True)
                overlay.set_child(host)
                notif = handle.copy_revealer()
                overlay.add_overlay(notif)
                w.set_child(overlay)
                w.present()

                # El overlay no debe capturar foco ni eventos
                self.assertFalse(notif.get_can_target())
                self.assertFalse(notif.get_can_focus())
                self.assertFalse(notif.get_child().get_can_target())

                # Estado inicial: oculto
                self.assertFalse(notif.get_visible())
                self.assertFalse(notif.get_reveal_child())

                # Disparar notificación
                handle.show_copy_notification()
                self.assertTrue(notif.get_visible())
                self.assertTrue(notif.get_reveal_child())
                self.assertAlmostEqual(notif.get_opacity(), 1.0)
                self.assertTrue(notif.get_child_revealed())

                # Ocultar
                notif.hide_notification()
                self.assertFalse(notif.get_visible())
                self.assertFalse(notif.get_reveal_child())
                self.assertAlmostEqual(notif.get_opacity(), 0.0)
            finally:
                handle.dispose()
                handle.stop(force=True)
                w.set_child(None)
                w.destroy()


if __name__ == "__main__":
    unittest.main()
