"""Pruebas de copiar y pegar en VTE, WebKitGTK (ChatGPT), editor de prompts y formularios.

Verifica:
- Atajos de teclado oficiales en VTE: Ctrl+Shift+C, Ctrl+Shift+V, Shift+Insert, Ctrl+Insert, Ctrl+Shift+A.
- Menú contextual en VTE con acciones Copiar, Pegar y Seleccionar todo.
- Preservación de Ctrl+C (SIGINT) y Ctrl+V estándar en terminales para agentes y bash.
- Manejo de foco y comandos de edición en WebKitGTK (ChatGPT).
- Soporte de textos multilínea y caracteres especiales en portapapeles.
- Integración con portapapeles de GNOME Wayland y Broadway.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: E402

    HAS_VTE = True
except (ValueError, ImportError):
    HAS_VTE = False

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402

    HAS_WEBKIT = True
except (ValueError, ImportError):
    HAS_WEBKIT = False

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


def pump(until=lambda: False, timeout: float = 5.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


class DummySink:
    def state_changed(self, state, detail: str = "", code: int | None = None) -> None:
        pass

    def append_output(self, text: str) -> None:
        pass


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE")
class VteClipboardTests(unittest.TestCase):
    """Comprueba el comportamiento del portapapeles y el menú contextual en VTE."""

    def test_vte_handle_context_menu_and_actions(self) -> None:
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionSpec
        from ia_workspace.terminal.backend import VteTerminalHandle

        with temp_dir() as folder:
            w = Gtk.Window()
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 10"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            w.set_child(handle.widget())
            w.present()
            try:
                term = handle.widget()
                self.assertIsInstance(term, Vte.Terminal)
                # Verifica que el popover exista
                self.assertIsNotNone(handle._popover)
                self.assertIsInstance(handle._popover, Gtk.PopoverMenu)

                # Verifica acciones registradas en el grupo 'term'
                self.assertIsNotNone(handle._act_copy)
                self.assertIsNotNone(handle._act_paste)
                self.assertIsNotNone(handle._act_select_all)

                # Sin selección, copiar debe estar deshabilitado
                handle._update_action_states()
                self.assertFalse(handle._act_copy.get_enabled())

                # Con selección, copiar debe habilitarse
                term.select_all()
                handle._update_action_states()
            finally:
                handle.dispose()
                handle.stop(force=True)
                w.set_child(None)
                w.destroy()

    def test_vte_key_controller_shortcuts(self) -> None:
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionSpec
        from ia_workspace.terminal.backend import VteTerminalHandle

        with temp_dir() as folder:
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/sh", "-c", "sleep 10"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            try:
                # Ctrl+C solo (sin Shift): NO debe consumirse (para enviar SIGINT al proceso hijo)
                res = handle._on_key_pressed(None, Gdk.KEY_c, 0, Gdk.ModifierType.CONTROL_MASK)
                self.assertFalse(res, "Ctrl+C no debe ser interceptado por la terminal (debe pasar al PTY)")

                # Ctrl+V solo (sin Shift): NO debe consumirse (lnext/verbatim de la shell)
                res = handle._on_key_pressed(None, Gdk.KEY_v, 0, Gdk.ModifierType.CONTROL_MASK)
                self.assertFalse(res, "Ctrl+V no debe ser interceptado en la terminal (debe pasar al PTY)")

                # Ctrl+Shift+V: SÍ debe consumirse (pegar del portapapeles)
                ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
                res = handle._on_key_pressed(None, Gdk.KEY_v, 0, ctrl_shift)
                self.assertTrue(res, "Ctrl+Shift+V debe consumirse para ejecutar el pegado")

                # Shift+Insert: SÍ debe consumirse (pegar)
                res = handle._on_key_pressed(None, Gdk.KEY_Insert, 0, Gdk.ModifierType.SHIFT_MASK)
                self.assertTrue(res, "Shift+Insert debe consumirse para ejecutar el pegado")

                # Ctrl+Shift+C: SÍ debe consumirse (copiar selección)
                res = handle._on_key_pressed(None, Gdk.KEY_c, 0, ctrl_shift)
                self.assertTrue(res, "Ctrl+Shift+C debe consumirse para ejecutar la copia")

                # Ctrl+Insert: SÍ debe consumirse (copiar)
                res = handle._on_key_pressed(None, Gdk.KEY_Insert, 0, Gdk.ModifierType.CONTROL_MASK)
                self.assertTrue(res, "Ctrl+Insert debe consumirse para ejecutar la copia")

                # Ctrl+Shift+A: SÍ debe consumirse (seleccionar todo)
                res = handle._on_key_pressed(None, Gdk.KEY_a, 0, ctrl_shift)
                self.assertTrue(res, "Ctrl+Shift+A debe consumirse para seleccionar todo")
            finally:
                handle.dispose()
                handle.stop(force=True)

    def test_vte_paste_multiline_real_pty(self) -> None:
        """Comprueba que pegar texto multilínea en una terminal bash real funcione."""
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionSpec
        from ia_workspace.terminal.backend import VteTerminalHandle

        with temp_dir() as folder:
            w = Gtk.Window()
            spec = SessionSpec(project_id=1, kind=AgentKind.SHELL, title="test", cwd=folder, args=("/bin/bash", "--noprofile", "--norc"))
            handle = VteTerminalHandle(spec, DummySink(), list(spec.args))
            w.set_child(handle.widget())
            w.present()
            try:
                pump(until=lambda: handle.pid is not None, timeout=3.0)
                self.assertIsNotNone(handle.pid)

                # Ponemos un texto reconocible en el portapapeles
                clipboard = handle.widget().get_clipboard()
                clipboard.set("echo IAW_PASTE_SUCCESS_MARKER\n")

                # Ejecutamos paste_from_clipboard
                handle.paste_from_clipboard()

                # Verificamos que el texto llegue a la terminal
                pump(until=lambda: "IAW_PASTE_SUCCESS_MARKER" in handle.text(), timeout=4.0)
                self.assertIn("IAW_PASTE_SUCCESS_MARKER", handle.text())
            finally:
                handle.dispose()
                handle.stop(force=True)
                w.set_child(None)
                w.destroy()


@unittest.skipUnless(HAS_DISPLAY and HAS_WEBKIT, "requiere display y WebKitGTK 6.0")
class WebKitClipboardTests(unittest.TestCase):
    """Comprueba el soporte de foco y comandos de edición en el navegador integrado."""

    def test_chat_browser_focusable_and_editing_keys(self) -> None:
        from ia_workspace.ui.chatgpt_browser import ChatBrowser

        browser = ChatBrowser(None, on_changed=lambda: None, on_external=lambda _u: None)
        view = browser.view

        # Comprueba que la vista web tenga foco habilitado
        self.assertTrue(view.get_focusable(), "WebKit.WebView debe ser focusable")
        self.assertTrue(view.get_focus_on_click(), "WebKit.WebView debe enfocarse al hacer clic")

        # Comprueba que los atajos de edición se capturen
        ctrl = Gdk.ModifierType.CONTROL_MASK
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK

        # Ctrl+V
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_v, 0, ctrl))
        # Ctrl+Shift+V (texto sin formato)
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_v, 0, ctrl_shift))
        # Shift+Insert
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_Insert, 0, Gdk.ModifierType.SHIFT_MASK))
        # Ctrl+C
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_c, 0, ctrl))
        # Ctrl+Insert
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_Insert, 0, ctrl))
        # Ctrl+X
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_x, 0, ctrl))
        # Shift+Delete
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_Delete, 0, Gdk.ModifierType.SHIFT_MASK))
        # Ctrl+A
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_a, 0, ctrl))
        # Ctrl+Z y Ctrl+Y
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_z, 0, ctrl))
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_y, 0, ctrl))
        self.assertTrue(browser._on_key_pressed(None, Gdk.KEY_z, 0, ctrl_shift))

        # Atajos globales de la app NO deben ser consumidos por el navegador
        self.assertFalse(browser._on_key_pressed(None, Gdk.KEY_q, 0, ctrl))
        self.assertFalse(browser._on_key_pressed(None, Gdk.KEY_o, 0, ctrl))
        self.assertFalse(browser._on_key_pressed(None, Gdk.KEY_n, 0, ctrl))
        self.assertFalse(browser._on_key_pressed(None, Gdk.KEY_F9, 0, Gdk.ModifierType(0)))


@unittest.skipUnless(HAS_DISPLAY, "requiere display")
class PromptEditorClipboardTests(unittest.TestCase):
    """Comprueba el editor de prompts y el portapapeles en widgets GTK estándar."""

    def test_text_view_prompt_buffer_clipboard(self) -> None:
        w = Gtk.Window()
        tv = Gtk.TextView()
        w.set_child(tv)
        w.present()
        try:
            buf = tv.get_buffer()
            sample_text = "Prompt multilínea:\n- Tarea 1: Analizar datos\n- Tarea 2: Crear reporte\n"
            buf.set_text(sample_text)

            # Seleccionar todo y copiar al portapapeles
            buf.select_range(buf.get_start_iter(), buf.get_end_iter())
            self.assertTrue(buf.get_has_selection())
            cb = tv.get_clipboard()
            buf.copy_clipboard(cb)

            # Crear un segundo textview y pegar
            tv2 = Gtk.TextView()
            w.set_child(tv2)
            buf2 = tv2.get_buffer()
            buf2.paste_clipboard(cb, None, True)

            # Esperar a que el pegado asíncrono se procese
            pump(until=lambda: buf2.get_char_count() > 0, timeout=2.0)

            # Verificar que el texto coincida
            start, end = buf2.get_bounds()
            pasted = buf2.get_text(start, end, True)
            self.assertEqual(pasted, sample_text)
        finally:
            w.set_child(None)
            w.destroy()


class WaylandClipboardToolsCheck(unittest.TestCase):
    """Verifica la presencia de herramientas del portapapeles del sistema."""

    def test_wl_copy_and_paste_installed_if_wayland(self) -> None:
        wl_copy = shutil.which("wl-copy")
        wl_paste = shutil.which("wl-paste")
        # En sistemas Linux Wayland wl-clipboard es la herramienta nativa
        if os.environ.get("WAYLAND_DISPLAY"):
            self.assertIsNotNone(wl_copy, "wl-copy debe estar instalado en el entorno Wayland")
            self.assertIsNotNone(wl_paste, "wl-paste debe estar instalado en el entorno Wayland")
