"""Pruebas de solicitud de permisos de micrófono y ciclo de vida de captura para ChatGPT integrado.

Verifica:
1. Solo se solicita autorización para captura de audio (sin vídeo) y origen legítimo.
2. La solicitud interactiva nativa (Adw.AlertDialog) permite autorizar o denegar.
3. Cualquier intento de acceso a vídeo o desde orígenes no autorizados se deniega de inmediato.
4. Al cerrar la conversación o invocar stop_capture(), se detiene la captura activa.
5. Todas las pruebas multimedia usan dispositivos simulados (IAW_MOCK_MEDIA_DEVICES=1),
   sin grabar audio real del usuario.
"""

from __future__ import annotations

import http.server
import os
import threading
import time
import unittest
from pathlib import Path

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

try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # noqa: E402

    Gst.init(None)
    HAS_URITRANSCODEBIN = Gst.ElementFactory.find("uritranscodebin") is not None
except Exception:
    HAS_URITRANSCODEBIN = False

from ia_workspace.ui.chatgpt_browser import ChatBrowser, network_session  # noqa: E402

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


class MicTestServer(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        html = """<!DOCTYPE html>
<html>
<head><title>Mic Test</title></head>
<body>
<script>
window.testResult = "pending";

if (location.pathname === "/audio") {
    navigator.mediaDevices.getUserMedia({ audio: true, video: false })
        .then(function(stream) {
            window.testResult = "granted:" + stream.getAudioTracks().length;
        })
        .catch(function(err) {
            window.testResult = "denied:" + err.name;
        });
} else if (location.pathname === "/video") {
    navigator.mediaDevices.getUserMedia({ audio: false, video: true })
        .then(function(stream) {
            window.testResult = "video-granted";
        })
        .catch(function(err) {
            window.testResult = "video-denied:" + err.name;
        });
} else if (location.pathname === "/recorder") {
    navigator.mediaDevices.getUserMedia({ audio: true, video: false })
        .then(function(stream) {
            try {
                var mr = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus" });
                var recordedSize = 0;
                mr.ondataavailable = function(e) {
                    if (e.data && e.data.size > 0) {
                        recordedSize += e.data.size;
                    }
                };
                mr.onerror = function(err) {
                    window.testResult = "mr-error:" + (err.error ? err.error.name : String(err));
                };
                mr.onstop = function() {
                    window.testResult = "stopped:" + recordedSize;
                };
                mr.start(100);
                setTimeout(function() {
                    mr.stop();
                    stream.getTracks().forEach(function(t) { t.stop(); });
                }, 350);
            } catch (e) {
                window.testResult = "start-exception:" + e.name;
            }
        })
        .catch(function(err) {
            window.testResult = "gum-denied:" + err.name;
        });
}
</script>
</body>
</html>"""
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(HAS_DISPLAY and HAS_WEBKIT, "requiere display y WebKitGTK 6.0")
class ChatGPTMicrophoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MicTestServer)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.orig_mock = os.environ.get("IAW_MOCK_MEDIA_DEVICES")
        os.environ["IAW_MOCK_MEDIA_DEVICES"] = "1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.orig_mock is None:
            os.environ.pop("IAW_MOCK_MEDIA_DEVICES", None)
        else:
            os.environ["IAW_MOCK_MEDIA_DEVICES"] = self.orig_mock

    def _run_app_scenario(self, setup_fn) -> dict:
        results: dict = {}
        app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)

        def on_activate(a: Adw.Application) -> None:
            win = Adw.ApplicationWindow(application=a)
            setup_fn(a, win, results)

        app.connect("activate", on_activate)
        app.run([])
        return results

    def test_microphone_permission_granted_delivers_audio_stream(self) -> None:
        """Verifica que al autorizar la solicitud, el stream de audio simulado se entrega."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            prompted: list[WebKit.UserMediaPermissionRequest] = []

            def on_prompt(req: WebKit.UserMediaPermissionRequest, cb) -> None:
                prompted.append(req)
                cb(True)  # Usuario pulsa "Permitir"

            session = network_session(None)
            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                on_permission_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()
            browser.open(1, f"{self.base_url}/audio")

            def check():
                def on_eval(v, r):
                    res = v.evaluate_javascript_finish(r).to_string()
                    results["prompted"] = prompted
                    results["js"] = res
                    browser.stop_capture()
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(1000, check)

        res = self._run_app_scenario(scenario)
        prompted = res.get("prompted", [])
        self.assertEqual(len(prompted), 1)
        req = prompted[0]
        self.assertTrue(req.props.is_for_audio_device)
        self.assertFalse(req.props.is_for_video_device)
        self.assertEqual(res.get("js"), "granted:1")

    def test_microphone_permission_denied_rejects(self) -> None:
        """Verifica que al denegar la solicitud, se emite NotAllowedError."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            prompted: list[WebKit.UserMediaPermissionRequest] = []

            def on_prompt(req: WebKit.UserMediaPermissionRequest, cb) -> None:
                prompted.append(req)
                cb(False)  # Usuario pulsa "Denegar"

            session = network_session(None)
            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                on_permission_prompt=on_prompt,
            )
            win.set_content(browser.view)
            win.present()
            browser.open(2, f"{self.base_url}/audio")

            def check():
                def on_eval(v, r):
                    res = v.evaluate_javascript_finish(r).to_string()
                    results["prompted"] = prompted
                    results["js"] = res
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(1000, check)

        res = self._run_app_scenario(scenario)
        self.assertEqual(len(res.get("prompted", [])), 1)
        self.assertEqual(res.get("js"), "denied:NotAllowedError")

    def test_video_permission_is_denied_without_prompt(self) -> None:
        """Verifica que solicitudes de cámara (vídeo) se deniegan sin mostrar diálogo."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            prompted: list[WebKit.UserMediaPermissionRequest] = []

            session = network_session(None)
            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                on_permission_prompt=lambda req, cb: prompted.append(req),
            )
            win.set_content(browser.view)
            win.present()
            browser.open(3, f"{self.base_url}/video")

            def check():
                def on_eval(v, r):
                    res = v.evaluate_javascript_finish(r).to_string()
                    results["prompted"] = prompted
                    results["js"] = res
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(1000, check)

        res = self._run_app_scenario(scenario)
        self.assertEqual(res.get("prompted", []), [])
        self.assertEqual(res.get("js"), "video-denied:NotAllowedError")

    def test_untrusted_origin_is_denied_without_prompt(self) -> None:
        """Verifica que orígenes no autorizados se deniegan de inmediato sin preguntar al usuario."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            prompted: list[WebKit.UserMediaPermissionRequest] = []

            session = network_session(None)
            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=False,
                on_permission_prompt=lambda req, cb: prompted.append(req),
            )
            win.set_content(browser.view)
            win.present()
            # Cargar HTML con origen ajeno a ChatGPT
            html = """<!DOCTYPE html><html><body><script>
                navigator.mediaDevices.getUserMedia({ audio: true })
                    .then(function() { window.testResult = "granted"; })
                    .catch(function(e) { window.testResult = "denied:" + e.name; });
            </script></body></html>"""
            browser.view.load_html(html, "https://example.com/")

            def check():
                def on_eval(v, r):
                    res = v.evaluate_javascript_finish(r).to_string()
                    results["prompted"] = prompted
                    results["js"] = res
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(1000, check)

        res = self._run_app_scenario(scenario)
        self.assertEqual(res.get("prompted", []), [])
        self.assertEqual(res.get("js"), "denied:NotAllowedError")

    def test_native_alert_dialog_construction_and_response(self) -> None:
        """Verifica que el diálogo nativo Adw.AlertDialog se construye con textos y respuestas correctas."""
        session = network_session(None)
        browser = ChatBrowser(
            session,
            on_changed=lambda: None,
            on_external=lambda _: None,
            allow_loopback_http=True,
        )

        class DummyRequest:
            class Props:
                is_for_audio_device = True
                is_for_video_device = False

            props = Props()
            allowed = False
            denied = False

            def allow(self):
                self.allowed = True

            def deny(self):
                self.denied = True

        req = DummyRequest()
        browser._prompt_microphone_permission(req)  # type: ignore[arg-type]

        dialog = browser._last_permission_dialog
        self.assertIsNotNone(dialog)
        self.assertIn("micrófono", dialog.get_heading().lower())
        self.assertIn("ChatGPT", dialog.get_body())
        self.assertEqual(dialog.get_default_response(), "allow")
        self.assertEqual(dialog.get_close_response(), "deny")

        # Emitir respuesta 'allow'
        dialog.emit("response", "allow")
        self.assertTrue(req.allowed)
        self.assertFalse(req.denied)

        # Probar cierre limpio
        browser.close()
        self.assertIsNone(browser._last_permission_dialog)

    def test_media_recorder_behavior_and_serialization(self) -> None:
        """Verifica la compatibilidad de MediaRecorder y su dependencia del transcodificador GStreamer."""
        def scenario(app: Adw.Application, win: Adw.ApplicationWindow, results: dict) -> None:
            session = network_session(None)
            browser = ChatBrowser(
                session,
                on_changed=lambda: None,
                on_external=lambda _: None,
                allow_loopback_http=True,
                on_permission_prompt=lambda req, cb: cb(True),
            )
            win.set_content(browser.view)
            win.present()
            browser.open(4, f"{self.base_url}/recorder")

            def check():
                def on_eval(v, r):
                    res = v.evaluate_javascript_finish(r).to_string()
                    results["js"] = res
                    browser.close()
                    app.quit()

                browser.view.evaluate_javascript("window.testResult", -1, None, None, None, on_eval)

            GLib.timeout_add(1200, check)

        res = self._run_app_scenario(scenario)
        js_result = res.get("js", "")
        if HAS_URITRANSCODEBIN:
            self.assertTrue(
                js_result.startswith("stopped:"),
                f"Se esperaba detención y emisión de blob grabado, se obtuvo: {js_result}",
            )
            size = int(js_result.split(":")[1])
            self.assertGreater(size, 0, "El blob de audio grabado debe tener tamaño superior a cero")
        else:
            self.assertEqual(
                js_result,
                "start-exception:NotSupportedError",
                "Sin uritranscodebin (gstreamer1.0-plugins-bad), MediaRecorder.start() debe fallar con NotSupportedError",
            )


if __name__ == "__main__":
    unittest.main()
