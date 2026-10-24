"""Pruebas de adjuntar imágenes del portapapeles y selección de archivos en ChatGPT integrado.

Verifica:
1. Una imagen PNG en el portapapeles se entrega con éxito al input de archivos web vía WebKitFileChooserRequest.
2. Si el portapapeles no tiene imagen (vacío o texto), se notifica de forma comprensible y permite elegir archivo o cancelar.
3. La cancelación del diálogo cancela la solicitud sin errores ni archivos residuales.
4. La selección tradicional de archivos («Elegir archivo…») permanece completamente disponible e intacta.
5. Los archivos temporales se crean con permisos 0600 en directorio privado 0700 y se limpian de forma segura al cerrar.
6. Aislamiento estricto: los archivos temporales no tocan proyectos externos ni directorios ajenos.
7. Comprobación del diálogo nativo Adw.AlertDialog con las opciones disponibles.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

tests_dir = Path(__file__).resolve().parent
if str(tests_dir) not in sys.path:
    sys.path.insert(0, str(tests_dir))

from helpers import ROOT  # noqa: F401

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402,F401

    HAS_WEBKIT = True
except (ValueError, ImportError):
    HAS_WEBKIT = False

from ia_workspace.ui.chatgpt_browser import ChatBrowser, network_session  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


def _create_sample_png() -> bytes:
    """Genera bytes de una imagen PNG válida de 2x2 píxeles."""
    buf = bytearray()
    buf.extend(b"\x89PNG\r\n\x1a\n")

    def chunk(tag: bytes, data: bytes) -> None:
        buf.extend(struct.pack(">I", len(data)))
        buf.extend(tag)
        buf.extend(data)
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        buf.extend(struct.pack(">I", crc))

    chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00\xff\x00\x00\x00\x00\xff\x00\x00\xff\x00"
    chunk(b"IDAT", zlib.compress(raw))
    chunk(b"IEND", b"")
    return bytes(buf)


class FileUploadServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        html = """<!DOCTYPE html>
<html>
<head><title>File Upload Test</title></head>
<body>
<input type="file" id="file-uploader" accept="image/*">
<script>
window.testResult = "waiting";
window.manualReadResult = "waiting";
window.performManualRead = function() {
    var file = document.getElementById('file-uploader').files[0];
    if (!file) {
        window.manualReadResult = "no-file";
        return;
    }
    var reader = new FileReader();
    reader.onload = function() {
        window.manualReadResult = "ok:" + reader.result.byteLength;
    };
    reader.onerror = function() {
        window.manualReadResult = "error:" + (reader.error ? reader.error.name : "unknown");
    };
    reader.readAsArrayBuffer(file);
};
document.getElementById('file-uploader').addEventListener('change', function(e) {
    var file = e.target.files[0];
    if (!file) {
        window.testResult = JSON.stringify({ error: "no-file" });
        return;
    }
    var reader = new FileReader();
    reader.onload = function() {
        var bytes = new Uint8Array(reader.result.slice(0, 4));
        var isPng = bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4E && bytes[3] === 0x47;
        window.testResult = JSON.stringify({
            name: file.name,
            size: file.size,
            type: file.type,
            isPng: isPng
        });
    };
    reader.readAsArrayBuffer(file);
});
</script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))


@unittest.skipUnless(HAS_DISPLAY and HAS_WEBKIT, "Requiere entorno gráfico GTK4/Adwaita y WebKitGTK 6.0")
class ChatGPTClipboardAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FileUploadServer)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.temp_dir = tempfile.mkdtemp(prefix="iaw-test-attach-")
        self.data_dir = Path(self.temp_dir) / "webkit"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_app_scenario(self, setup_fn) -> dict:
        results: dict = {}
        app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)

        def on_activate(a: Adw.Application) -> None:
            win = Adw.ApplicationWindow(application=a)
            setup_fn(a, win, results)

        app.connect("activate", on_activate)
        app.run([])
        return results

    def test_clipboard_image_attachment_success(self) -> None:
        """Una imagen en el portapapeles se extrae y adjunta al input web vía 'clipboard'."""
        png_bytes = _create_sample_png()
        glib_bytes = GLib.Bytes.new(png_bytes)
        provider = Gdk.ContentProvider.new_for_bytes("image/png", glib_bytes)

        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)
            prompted = []

            def on_prompt(req, cb):
                prompted.append(req)
                cb("clipboard")

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()

            clipboard = win.get_clipboard()
            clipboard.set_content(provider)

            browser.open(1, f"{self.base_url}/upload")

            def trigger_click():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check():
                def on_eval(v, r):
                    try:
                        res = v.evaluate_javascript_finish(r).to_string()
                    except Exception:
                        res = "eval-error"
                    if res != "waiting":
                        results["prompted"] = prompted
                        results["js"] = res
                        results["captures_count"] = len(browser._temp_capture_files)
                        browser.close()
                        app.quit()
                    else:
                        GLib.timeout_add(100, check)

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(400, trigger_click)
            GLib.timeout_add(700, check)
            GLib.timeout_add(5000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertEqual(len(res.get("prompted", [])), 1)
        js_raw = res.get("js", "")
        self.assertTrue(js_raw.startswith("{"), f"Esperado JSON en testResult, recibido: {js_raw}")
        data = json.loads(js_raw)
        self.assertTrue(data.get("isPng"), "El archivo web debe ser un PNG válido")
        self.assertEqual(data.get("type"), "image/png")
        self.assertGreater(data.get("size", 0), 0)

        captures_dir = self.data_dir / "clipboard_captures"
        self.assertTrue(captures_dir.is_dir())
        self.assertEqual(oct(captures_dir.stat().st_mode & 0o777), "0o700")

    def test_clipboard_empty_or_text_fallback(self) -> None:
        """Si el portapapeles contiene texto o está vacío, se informa y permite cancelar."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)
            no_image_prompted = []

            def on_prompt(req, cb):
                cb("clipboard")

            def on_no_image(req, cb):
                no_image_prompted.append(req)
                cb("cancel")

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
                on_no_image_prompt=on_no_image,
            )
            win.set_content(browser.view)
            win.present()

            clipboard = win.get_clipboard()
            text_bytes = GLib.Bytes.new(b"Texto sin imagen")
            provider = Gdk.ContentProvider.new_for_bytes("text/plain", text_bytes)
            clipboard.set_content(provider)

            browser.open(1, f"{self.base_url}/upload")

            def trigger_click():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check():
                results["no_image_prompted"] = no_image_prompted
                browser.close()
                app.quit()

            GLib.timeout_add(400, trigger_click)
            GLib.timeout_add(900, check)
            GLib.timeout_add(4000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertEqual(len(res.get("no_image_prompted", [])), 1)

    def test_user_cancellation(self) -> None:
        """El usuario cancela la solicitud de archivo y no se envía nada."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)
            prompted = []

            def on_prompt(req, cb):
                prompted.append(req)
                cb("cancel")

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()

            browser.open(1, f"{self.base_url}/upload")

            def trigger_click():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check():
                def on_eval(v, r):
                    try:
                        res = v.evaluate_javascript_finish(r).to_string()
                    except Exception:
                        res = "error"
                    results["prompted"] = prompted
                    results["js"] = res
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(400, trigger_click)
            GLib.timeout_add(800, check)
            GLib.timeout_add(4000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertEqual(len(res.get("prompted", [])), 1)
        self.assertEqual(res.get("js"), "waiting")

    def test_traditional_file_chooser_selection(self) -> None:
        """La opción 'choose_file' permite seleccionar un archivo normal en disco."""
        sample_file = Path(self.temp_dir) / "documento_elegido.png"
        sample_file.write_bytes(_create_sample_png())

        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)
            prompted = []

            def on_prompt(req, cb):
                prompted.append(req)
                req.select_files([str(sample_file)])

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()

            browser.open(1, f"{self.base_url}/upload")

            def trigger_click():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check():
                def on_eval(v, r):
                    try:
                        res = v.evaluate_javascript_finish(r).to_string()
                    except Exception:
                        res = "eval-error"
                    if res != "waiting":
                        results["prompted"] = prompted
                        results["js"] = res
                        browser.close()
                        app.quit()
                    else:
                        GLib.timeout_add(100, check)

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(400, trigger_click)
            GLib.timeout_add(700, check)
            GLib.timeout_add(4000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertEqual(len(res.get("prompted", [])), 1)
        js_raw = res.get("js", "")
        self.assertTrue(js_raw.startswith("{"), f"Esperado JSON, recibido: {js_raw}")
        data = json.loads(js_raw)
        self.assertEqual(data.get("name"), "documento_elegido.png")
        self.assertTrue(data.get("isPng"))

    def test_temporary_file_security_and_cleanup(self) -> None:
        """Verifica permisos 0700/0600 y purgado de archivos antiguos."""
        session = network_session(self.data_dir)
        browser = ChatBrowser(
            session,
            on_changed=lambda: None,
            on_external=lambda _: None,
            allow_loopback_http=True,
            data_dir=self.data_dir,
        )

        clip_dir = browser._init_clipboard_dir()
        self.assertTrue(clip_dir.is_dir())
        self.assertEqual(oct(clip_dir.stat().st_mode & 0o777), "0o700")

        png_bytes = _create_sample_png()
        glib_bytes = GLib.Bytes.new(png_bytes)
        texture = Gdk.Texture.new_from_bytes(glib_bytes)

        class DummyRequest:
            def __init__(self):
                self.selected = []

            def select_files(self, files):
                self.selected = files

        req = DummyRequest()
        browser._save_and_select_texture(texture, req)

        self.assertEqual(len(req.selected), 1)
        created_file = Path(req.selected[0])
        self.assertTrue(created_file.is_file())
        self.assertEqual(oct(created_file.stat().st_mode & 0o777), "0o600")
        self.assertIn(created_file, browser._temp_capture_files)

        browser.close()
        self.assertFalse(created_file.exists(), "browser.close() debió eliminar el archivo temporal")
        self.assertEqual(len(browser._temp_capture_files), 0)

        stale_file = clip_dir / "clip_20261023_000000_stale.png"
        stale_file.write_bytes(b"stale")
        os.utime(stale_file, (time.time() - 1000, time.time() - 1000))
        self.assertTrue(stale_file.exists())

        browser._prune_stale_temp_captures(clip_dir)
        self.assertFalse(stale_file.exists(), "El archivo antiguo debió ser purgado")

    def test_project_isolation(self) -> None:
        """Comprueba que los temporales se limitan estrictamente a la carpeta designada."""
        outside_project_dir = Path(self.temp_dir) / "external_project"
        outside_project_dir.mkdir(parents=True, exist_ok=True)
        sensitive_file = outside_project_dir / "codigo_privado.py"
        sensitive_file.write_text("# datos confidenciales")

        session = network_session(self.data_dir)
        browser = ChatBrowser(
            session,
            on_changed=lambda: None,
            on_external=lambda _: None,
            allow_loopback_http=True,
            data_dir=self.data_dir,
        )

        clip_dir = browser._init_clipboard_dir()
        self.assertTrue(str(clip_dir).startswith(str(self.data_dir)))
        self.assertFalse(str(clip_dir).startswith(str(outside_project_dir)))
        self.assertTrue(sensitive_file.is_file())
        self.assertEqual(sensitive_file.read_text(), "# datos confidenciales")
        browser.close()

    def test_alert_dialog_construction(self) -> None:
        """Comprueba la construcción nativa de Adw.AlertDialog para la selección de archivo."""
        session = network_session(self.data_dir)
        browser = ChatBrowser(
            session,
            on_changed=lambda: None,
            on_external=lambda _: None,
            allow_loopback_http=True,
            data_dir=self.data_dir,
        )

        class DummyRequest:
            def cancel(self):
                pass

        win = Adw.ApplicationWindow()
        win.set_content(browser.view)

        browser._prompt_file_chooser_options(DummyRequest())
        self.assertIsNotNone(browser._last_file_chooser_dialog)
        dlg = browser._last_file_chooser_dialog
        self.assertEqual(dlg.get_heading(), "Adjuntar en ChatGPT")
        self.assertEqual(dlg.get_default_response(), "clipboard")
        self.assertEqual(dlg.get_close_response(), "cancel")
        browser.close()
        win.destroy()

    def test_delayed_read_preserves_file_until_conversation_lifecycle_ends(self) -> None:
        """Comprueba que lecturas retrasadas o subidas lentas no fallan por borrado prematuro.

        El archivo se mantiene intacto durante la conversación y se elimina solo al
        cambiar de conversación o cerrar la vista.
        """
        png_bytes = _create_sample_png()
        glib_bytes = GLib.Bytes.new(png_bytes)
        provider = Gdk.ContentProvider.new_for_bytes("image/png", glib_bytes)

        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)

            def on_prompt(req, cb):
                cb("clipboard")

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()

            clipboard = win.get_clipboard()
            clipboard.set_content(provider)

            browser.open(1, f"{self.base_url}/upload")

            def trigger_click():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def trigger_delayed_manual_read():
                # Comprobamos que el archivo existe en disco y está registrado
                temp_files = list(browser._temp_capture_files)
                results["temp_files_before_read"] = [str(f) for f in temp_files]
                results["files_exist_before_read"] = all(f.is_file() for f in temp_files)

                # Ejecutamos lectura manual retrasada simulando una subida lenta
                browser.view.evaluate_javascript(
                    "window.performManualRead()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check_manual_read():
                def on_eval(v, r):
                    try:
                        res = v.evaluate_javascript_finish(r).to_string()
                    except Exception:
                        res = "eval-error"
                    if res.startswith("ok:"):
                        results["manual_result"] = res
                        # Ahora simulamos cambio de conversación
                        browser.open(2, f"{self.base_url}/upload_2")
                        results["temp_files_after_open_new"] = len(browser._temp_capture_files)
                        browser.close()
                        app.quit()
                    else:
                        GLib.timeout_add(100, check_manual_read)

                browser.view.evaluate_javascript(
                    "window.manualReadResult", -1, None, None, None, on_eval
                )

            GLib.timeout_add(400, trigger_click)
            GLib.timeout_add(700, trigger_delayed_manual_read)
            GLib.timeout_add(1000, check_manual_read)
            GLib.timeout_add(5000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertTrue(res.get("files_exist_before_read"))
        self.assertTrue(res.get("manual_result", "").startswith("ok:"), f"Resultado: {res.get('manual_result')}")
        self.assertEqual(res.get("temp_files_after_open_new"), 0)

    def test_premature_unlink_reproduces_not_found_error(self) -> None:
        """Demuestra que desvincular el archivo antes de que WebKit termine produce NotFoundError."""
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(_create_sample_png())
        tmp.close()
        tmp_path = Path(tmp.name)

        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(self.data_dir)

            def on_prompt(req, _cb):
                req.select_files([str(tmp_path)])

            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                data_dir=self.data_dir,
                on_file_chooser_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()
            browser.open(1, f"{self.base_url}/upload")

            def step_select():
                browser.view.evaluate_javascript(
                    "document.getElementById('file-uploader').click()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def step_unlink_and_read():
                # Eliminación prematura forzada (riesgo identificado del temporizador de 120s)
                tmp_path.unlink(missing_ok=True)
                browser.view.evaluate_javascript(
                    "window.performManualRead()",
                    -1,
                    None,
                    None,
                    None,
                    lambda *_: None,
                )

            def check():
                def on_eval(v, r):
                    try:
                        res = v.evaluate_javascript_finish(r).to_string()
                    except Exception:
                        res = "error"
                    if res != "waiting":
                        results["read_result"] = res
                        browser.close()
                        app.quit()
                    else:
                        GLib.timeout_add(100, check)

                browser.view.evaluate_javascript("window.manualReadResult", -1, None, None, None, on_eval)

            GLib.timeout_add(400, step_select)
            GLib.timeout_add(700, step_unlink_and_read)
            GLib.timeout_add(1000, check)
            GLib.timeout_add(4000, app.quit)

        res = self._run_app_scenario(scenario)
        self.assertEqual(res.get("read_result"), "error:NotFoundError")


if __name__ == "__main__":
    unittest.main()
