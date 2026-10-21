"""ChatGPT integrado (WebKitGTK 6.0) con un servidor local que simula el sitio.

Nunca se contacta con chatgpt.com: las conversaciones vinculadas (enlaces reales de
chatgpt.com) se cargan desde un servidor HTTP en 127.0.0.1 mediante la variable de
pruebas ``IAW_CHATGPT_BASE_URL``, que solo admite http en loopback. Las páginas de
prueba reflejan su estado en el título; la prueba no inyecta ni evalúa JavaScript.
"""

from __future__ import annotations

import http.server
import os
import re
import socket
import sys
import threading
import time
import traceback
import unittest
from pathlib import Path

from helpers import temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402,F401

    HAS_WEBKIT = True
except (ValueError, ImportError):
    HAS_WEBKIT = False
try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: E402,F401

    HAS_VTE = True
except (ValueError, ImportError):
    HAS_VTE = False

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
SESSION_COOKIE = "sim_session_7f3a"  # valor que nunca debe aparecer en SQLite ni en registros

CONV_1, CONV_2, CONV_B = "aaaa1111-0001", "aaaa2222-0002", "bbbb1111-0001"
INSECURE, NOTIFY, DOWNLOAD = "insecure-0001", "notify-00001", "download-0001"


class FakeChatGPT(http.server.BaseHTTPRequestHandler):
    """Imita lo justo de chatgpt.com: una sesión por cookie HttpOnly y páginas por conversación."""

    def log_message(self, *args) -> None:
        pass

    def _html(self, body: str, cookie: bool = False, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if cookie:
            self.send_header("Set-Cookie", f"iaw_sim={SESSION_COOKIE}; Max-Age=3600; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        match = re.fullmatch(r"/c/([A-Za-z0-9-]+)", self.path)
        if not match:
            self.send_response(404)
            self.end_headers()
            return
        conv = match.group(1)
        if conv == INSECURE:  # intenta llevar la vista a una página sin HTTPS
            self._html('<title>insecure</title><meta http-equiv="refresh" content="0;url=http://example.invalid/">')
        elif conv == NOTIFY:  # pide permiso de notificaciones: debe denegarse
            self._html("<title>notify</title><script>Notification.requestPermission()"
                       ".then(function(p){document.title='perm-'+p})</script>")
        elif conv == DOWNLOAD:
            self._html("datos binarios", ctype="application/octet-stream")
        else:
            had = SESSION_COOKIE in (self.headers.get("Cookie") or "")
            self._html(f"<title>conv-{conv}|{'sesion' if had else 'nueva'}</title>", cookie=not had)


def pump(until=lambda: False, timeout: float = 8.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def webkit_children() -> list[int]:
    me = os.getpid()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        name = stat[stat.find("(") + 1: stat.rfind(")")]
        fields = stat[stat.rfind(")") + 2:].split()
        if int(fields[1]) == me and "WebKit" in name and fields[0] not in ("Z", "X"):
            found.append((int(entry.name), name))
    return found


def rows(listbox: Gtk.ListBox) -> list:
    result, child = [], listbox.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


@unittest.skipUnless(HAS_DISPLAY and HAS_WEBKIT and HAS_VTE, "requiere display, WebKitGTK 6.0 y VTE")
class ChatGPTWebTest(unittest.TestCase):
    def test_integrated_chatgpt(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeChatGPT)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with temp_dir() as data, temp_dir() as folders:
            dir_a, dir_b = Path(folders) / "alfa", Path(folders) / "beta"
            dir_a.mkdir()
            dir_b.mkdir()
            env = {"IAW_DATA_DIR": data, "IAW_CHATGPT_BASE_URL": base,
                   "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc", "HISTFILE": ""}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            opened: list[str] = []
            try:
                errors = self._run(lambda app, check: self._first(app, check, dir_a, dir_b, opened, server))
                server = http.server.ThreadingHTTPServer(("127.0.0.1", server.server_address[1]), FakeChatGPT)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                errors += self._run(lambda app, check: self._second(app, check))
            finally:
                server.shutdown()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            # Las cookies quedan en el almacenamiento de WebKitGTK, nunca en la base de datos.
            web_data = Path(data) / "webkit" / "data"
            if not (web_data / "cookies.sqlite").is_file():
                errors.append("no se creó el almacenamiento persistente de WebKitGTK")
            db_bytes = b"".join(p.read_bytes() for p in Path(data).glob("ia-workspace.db*"))
            if SESSION_COOKIE.encode() in db_bytes:
                errors.append("la cookie de la sesión web apareció en la base de datos de IA Workspace")
            if any(not url.startswith("https://chatgpt.com/") for url in opened):
                errors.append(f"se abrió en el navegador algo que no es chatgpt.com: {opened}")
            leftovers = [p for p in webkit_children() if p[1].startswith("WebKitWebProc")]
            deadline = time.monotonic() + 10
            while leftovers and time.monotonic() < deadline:
                time.sleep(0.1)
                leftovers = [p for p in webkit_children() if p[1].startswith("WebKitWebProc")]
            if leftovers:
                errors.append(f"procesos web de WebKit vivos tras cerrar: {leftovers}")
        self.assertEqual(errors, [], "\n".join(errors))

    def test_no_webkit_process_survives_the_app_process(self) -> None:
        import subprocess

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeChatGPT)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with temp_dir() as data:
                probe = Path(__file__).with_name("webkit_exit_probe.py")
                url = f"http://127.0.0.1:{server.server_address[1]}/c/{CONV_1}"
                out = subprocess.run([sys.executable, str(probe), url, data], capture_output=True, text=True,
                                     timeout=60, env=dict(os.environ)).stdout
                self.assertIn(f"TITLE conv-{CONV_1}|nueva", out)
                pids = [int(x) for x in next(l for l in out.splitlines() if l.startswith("PIDS")).split()[1:]]
                self.assertTrue(pids, "el proceso auxiliar no llegó a crear procesos de WebKit")
                deadline = time.monotonic() + 10
                alive = [p for p in pids if Path(f"/proc/{p}").exists()]
                while alive and time.monotonic() < deadline:
                    time.sleep(0.1)
                    alive = [p for p in pids if Path(f"/proc/{p}").exists()]
                self.assertEqual(alive, [], "procesos de WebKit huérfanos tras terminar la aplicación")
        finally:
            server.shutdown()

    # ---------------------------------------------------------------------------------

    def _run(self, body) -> list[str]:
        from ia_workspace.app import IAWorkspaceApplication

        errors: list[str] = []
        app = IAWorkspaceApplication()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        previous = sys.excepthook
        sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

        def check(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        def scenario() -> bool:
            try:
                Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                body(app, check)
            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())
            app._shutdown_sessions_and_quit()
            return GLib.SOURCE_REMOVE

        def timeout() -> bool:
            errors.append("tiempo agotado")
            app.quit()
            return GLib.SOURCE_REMOVE

        app.connect("activate", lambda *_: GLib.idle_add(scenario))
        watchdog = GLib.timeout_add_seconds(120, timeout)
        try:
            app.run([])
        finally:
            if "tiempo agotado" not in errors:
                GLib.source_remove(watchdog)
            sys.excepthook = previous
        return errors

    def _first(self, app, check, dir_a: Path, dir_b: Path, opened: list[str], server) -> None:
        from ia_workspace.sessions.models import SessionState

        win, ctx = app._window, app.ctx
        a = ctx.projects.register("Alfa", str(dir_a), ())
        b = ctx.projects.register("Beta", str(dir_b), ())
        for conv, title in ((CONV_1, "Arquitectura"), (CONV_2, "Pruebas"), (INSECURE, "Insegura"),
                            (NOTIFY, "Permisos"), (DOWNLOAD, "Descarga")):
            ctx.chat_links.link(a.id, f"https://chatgpt.com/c/{conv}", title)
        ctx.chat_links.link(b.id, f"https://chatgpt.com/c/{CONV_B}", "Solo de Beta")

        # Una terminal de A trabaja durante toda la prueba.
        win.open_project(a.id)
        view_a = win._workspaces[a.id]
        view_a._open_uri = opened.append  # nunca abrir el navegador real
        term = view_a.new_terminal()
        check(pump(lambda: ctx.sessions.get(term).state is SessionState.IDLE), "la terminal no arrancó")
        ctx.sessions.send_input(term, "for i in $(seq 1 400); do echo TICK $i; sleep 0.05; done\n")

        def ticks() -> int:
            found = re.findall(r"TICK (\d+)", ctx.sessions.handle(term).text())
            return max(map(int, found)) if found else 0

        term_pid = ctx.sessions.handle(term).pid

        def row_for(view, title: str):
            return next((r for r in rows(view._chat_list) if r.get_title() == title), None)

        # 1. Abrir una conversación vinculada dentro de IA Workspace.
        row_for(view_a, "Arquitectura").emit("activated")
        browser = view_a._browser
        check(browser is not None, "no se creó la vista web")
        check(pump(lambda: browser.title.startswith(f"conv-{CONV_1}")), f"no cargó: {browser.title!r}")
        check(browser.title.endswith("|nueva"), "la primera visita no debería traer sesión")
        check(view_a._chat_stack.get_visible_child_name() == "web", "no se mostró la vista web")
        check(browser.view.get_parent() is view_a._web_host, "la vista web no está en el panel")

        # 2. Otra conversación: misma vista web, y la sesión (cookie HttpOnly) ya se envía.
        first_view = browser.view
        check(view_a.open_conversation(next(l.id for l in ctx.chat_links.list(a.id) if l.title == "Pruebas")),
              "no se abrió la segunda conversación")
        check(pump(lambda: browser.title == f"conv-{CONV_2}|sesion"), f"sin sesión: {browser.title!r}")
        check(browser.view is first_view, "se recreó la vista web")
        current = browser.link_id
        check(not browser.open(current, browser.url), "reabrir la misma conversación no debería recargarla")

        # 3. Volver a la lista y abrir fuera: siempre la URL canónica de chatgpt.com.
        view_a._show_chat_list()
        check(view_a._chat_stack.get_visible_child_name() == "list", "no se volvió a la lista")
        pills = [r for r in rows(view_a._chat_list) if r.get_title() == "Pruebas"]
        check(bool(pills), "falta la fila de la conversación abierta")
        view_a._web_external()
        check(opened[-1:] == [f"https://chatgpt.com/c/{CONV_2}"], f"apertura externa: {opened}")

        # 4. Seguridad: sin HTTPS no se navega; permisos denegados; descargas bloqueadas.
        def open_titled(title: str) -> None:
            view_a.open_conversation(next(l.id for l in ctx.chat_links.list(a.id) if l.title == title))

        open_titled("Insegura")
        check(pump(lambda: browser.notice is not None and "HTTPS" in browser.notice),
              f"no se bloqueó la navegación sin HTTPS: notice={browser.notice!r}")
        check("example.invalid" not in (browser.view.get_uri() or ""), "la vista llegó a la página insegura")
        check(view_a._web_banner.get_revealed(), "no se mostró el aviso de seguridad")
        open_titled("Permisos")
        check(pump(lambda: browser.title == "perm-denied"), f"permiso no denegado: {browser.title!r}")
        open_titled("Descarga")
        check(pump(lambda: browser.notice is not None and "descargas" in browser.notice.lower()),
              f"la descarga no se bloqueó: {browser.notice!r}")

        # 5. Vista ampliada: la misma vista web, solo con las conversaciones de A.
        view_a.open_conversation(next(l.id for l in ctx.chat_links.list(a.id) if l.title == "Arquitectura"))
        pump(lambda: browser.title.startswith(f"conv-{CONV_1}"))
        view_a.open_chat_expanded()
        dialog = view_a._expanded
        check(dialog is not None, "no se abrió la vista ampliada")
        model = dialog.selector.get_model()
        titles = [model.get_string(i) for i in range(model.get_n_items())]
        check(sorted(titles) == sorted(["Arquitectura", "Pruebas", "Insegura", "Permisos", "Descarga"]),
              f"la vista ampliada ofrece conversaciones ajenas: {titles}")
        check(browser.view.get_parent() is dialog.host, "la vista web no pasó a la vista ampliada")
        dialog.selector.set_selected(titles.index("Pruebas"))
        check(pump(lambda: browser.title.startswith(f"conv-{CONV_2}")), "el selector no cambió de conversación")
        check(browser.view is first_view, "la vista ampliada recreó la vista web")
        dialog.force_close()
        pump(timeout=0.2)
        check(view_a._expanded is None and browser.view.get_parent() is view_a._web_host,
              "la vista web no volvió al panel")

        # 6. Ventana estrecha: el panel se oculta y la cabecera ofrece la vista ampliada.
        view_a._on_narrow(None, "show_chat", True)
        check(view_a._chat_header_button.get_visible(), "no aparece el acceso a ChatGPT en ventana estrecha")
        view_a._chat_header_button.emit("clicked")
        check(view_a._expanded is not None, "el botón de la cabecera no abrió la vista ampliada")
        view_a._expanded.force_close()
        pump(timeout=0.2)
        view_a._on_narrow(None, "show_chat", False)
        check(not view_a._chat_header_button.get_visible(), "el acceso de la cabecera debería ocultarse")

        # 7. Aislamiento: B solo ve lo suyo, con su propia vista web y la sesión compartida.
        before = ticks()
        win.open_project(b.id)
        view_b = win._workspaces[b.id]
        view_b._open_uri = opened.append
        check([r.get_title() for r in rows(view_b._chat_list)] == ["Solo de Beta"],
              "B muestra conversaciones de otro proyecto")
        rows(view_b._chat_list)[0].emit("activated")
        check(view_b._browser is not None and view_b._browser.view is not first_view,
              "B debería tener su propia vista web")
        check(pump(lambda: view_b._browser.title == f"conv-{CONV_B}|sesion"),
              f"la sesión no se comparte entre proyectos: {view_b._browser.title!r}")
        check(not view_b.open_conversation(ctx.chat_links.list(a.id)[0].id),
              "B abrió una conversación de A")
        check(pump(lambda: ticks() > before + 5, 5), "la terminal de A dejó de avanzar al cambiar de proyecto")
        check(ctx.sessions.handle(term).pid == term_pid and ctx.sessions.get(term).state.is_active,
              "la terminal de A se reinició o se detuvo")

        # 8. Desvincular la conversación abierta cierra la vista web de ese proyecto.
        win.open_project(a.id)
        current_link = view_a._current_link()
        ctx.chat_links.unlink(current_link.id)
        check(browser.link_id is None and view_a._chat_stack.get_visible_child_name() == "list",
              "la vista siguió mostrando una conversación desvinculada")

        # 9. Fallo de red: aviso con «Reintentar», sin perder la vista.
        view_a.open_conversation(next(l.id for l in ctx.chat_links.list(a.id) if l.title == "Arquitectura"))
        pump(lambda: browser.title.startswith(f"conv-{CONV_1}"))
        server.shutdown()
        server.server_close()
        view_a._web_reload()
        check(pump(lambda: browser.error is not None), "no se detectó el fallo de red")
        check(view_a._web_banner.get_revealed() and view_a._web_banner.get_button_label() == "Reintentar",
              "no se ofreció reintentar")
        check(ctx.sessions.get(term).state.is_active, "la terminal se detuvo durante la prueba")

    def _second(self, app, check) -> None:
        win, ctx = app._window, app.ctx
        projects = {p.name: p for p in ctx.projects.list()}
        win.open_project(projects["Beta"].id)
        view_b = win._workspaces[projects["Beta"].id]
        view_b._open_uri = lambda _uri: None
        rows(view_b._chat_list)[0].emit("activated")
        check(pump(lambda: view_b._browser is not None and view_b._browser.title.startswith(f"conv-{CONV_B}")),
              "no cargó tras reiniciar")
        check(view_b._browser.title == f"conv-{CONV_B}|sesion",
              f"la sesión web no persistió tras reiniciar: {view_b._browser.title!r}")

        # «Borrar datos web»: la sesión del sitio desaparece; las conversaciones vinculadas no.
        from ia_workspace.ui.chatgpt_browser import clear_website_data

        done: dict = {}
        clear_website_data(ctx.paths.data_dir / "webkit", lambda ok: done.setdefault("ok", ok))
        check(pump(lambda: "ok" in done) and done["ok"], "no se borraron los datos web")
        view_b._browser.reload()
        check(pump(lambda: view_b._browser.title == f"conv-{CONV_B}|nueva"),
              f"la sesión sigue tras borrar los datos web: {view_b._browser.title!r}")
        check([l.title for l in ctx.chat_links.list(projects["Beta"].id)] == ["Solo de Beta"],
              "borrar los datos web afectó a las conversaciones vinculadas")
        win.open_project(projects["Alfa"].id)
        view_a = win._workspaces[projects["Alfa"].id]
        check(sorted(r.get_title() for r in rows(view_a._chat_list)) == ["Arquitectura", "Descarga", "Insegura",
                                                                         "Permisos"],
              "las conversaciones de A no persistieron como se esperaba")


if __name__ == "__main__":
    unittest.main()
