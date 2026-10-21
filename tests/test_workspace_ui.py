"""Panel derecho plegable y conversaciones vinculadas, con la aplicación real.

Se arranca dos veces sobre la misma base de datos para comprobar la persistencia
tras reiniciar. La apertura del navegador se sustituye por un registro de la URL:
la prueba nunca abre el navegador ni contacta con ChatGPT.
"""

from __future__ import annotations

import os
import sys
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

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
CID_A = "6718a2b4-1c2d-8000-9f3a-0123456789ab"
CID_B = "aaaabbbb-cccc-8000-9999-000011112222"


def pump(until=lambda: False, timeout: float = 3.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def run_app(scenario_body) -> list[str]:
    from ia_workspace.app import IAWorkspaceApplication

    errors: list[str] = []
    app = IAWorkspaceApplication()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    previous_hook = sys.excepthook
    sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    def scenario() -> bool:
        try:
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
            scenario_body(app, check)
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())
        app.quit()
        return GLib.SOURCE_REMOVE

    app.connect("activate", lambda *_: GLib.idle_add(scenario))
    try:
        app.run([])
    finally:
        sys.excepthook = previous_hook
    return errors


def relayout(win, widget, until=lambda: True) -> int:
    """Broadway sin navegador apenas avanza el reloj de fotogramas: se repite la petición de
    distribución hasta que se aplica (o 3 s). En un escritorio real ocurre en el siguiente fotograma."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        win.queue_resize()
        widget.queue_allocate()
        pump(timeout=0.1)
        if widget.get_width() > 0 and until():
            break
    return widget.get_width()


def list_rows(view) -> list:
    rows, child = [], view._chat_list.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def respond(view, response: str) -> None:
    dialog = view._last_dialog
    dialog.emit("response", response)
    dialog.force_close()
    pump(timeout=0.1)


@unittest.skipUnless(HAS_DISPLAY, "sin display gráfico")
class WorkspaceFixesTest(unittest.TestCase):
    def test_right_panel_and_conversations(self) -> None:
        with temp_dir() as data, temp_dir() as folders:
            dir_a, dir_b = Path(folders) / "alfa", Path(folders) / "beta"
            dir_a.mkdir()
            dir_b.mkdir()
            os.environ["IAW_DATA_DIR"] = data
            opened: list[str] = []
            try:
                errors = run_app(lambda app, check: self._first_run(app, check, dir_a, dir_b, opened))
                errors += run_app(lambda app, check: self._second_run(app, check))
            finally:
                os.environ.pop("IAW_DATA_DIR")
            self.assertEqual(errors, [], "\n".join(errors))
            self.assertEqual(opened, [f"https://chatgpt.com/c/{CID_A}"], "solo se abre el enlace canónico elegido")

    # ---- primera ejecución -----------------------------------------------------------

    def _first_run(self, app, check, dir_a: Path, dir_b: Path, opened: list[str]) -> None:
        win, ctx = app._window, app.ctx
        a = ctx.projects.register("Alfa", str(dir_a), ())
        b = ctx.projects.register("Beta", str(dir_b), ())

        # --- Panel derecho -------------------------------------------------------------
        win.open_project(a.id)
        view_a = win._workspaces[a.id]
        relayout(win, view_a._center)
        check(view_a._context_toggle.get_active() and view_a._context_pane.get_visible(),
              "el panel derecho debería estar visible al principio")
        view_a._ctx_toggle.set_active_name("memory")
        memory_widget = view_a._ctx_stack.get_visible_child()
        width_before = relayout(win, view_a._center)

        view_a._context_toggle.set_active(False)  # el botón de la cabecera
        width_after = relayout(win, view_a._center,
                               lambda: view_a._center.get_width() == view_a._inner.get_width())
        check(not view_a._context_pane.get_visible(), "el panel derecho no se ocultó")
        check(width_after > width_before + 100 and width_after == view_a._inner.get_width(),
              f"el centro no ocupó el espacio libre ({width_before} → {width_after}, panel {view_a._inner.get_width()})")
        check(view_a._context_toggle.get_visible() and view_a._context_toggle.get_sensitive(),
              "el botón para volver a mostrarlo debe seguir accesible")
        check(not view_a._panel_switches["show_context"].get_active(), "el interruptor del menú no se sincronizó")

        view_a._context_toggle.set_active(True)
        pump(timeout=0.2)
        check(view_a._context_pane.get_visible(), "el panel derecho no volvió a mostrarse")
        check(view_a._ctx_toggle.get_active_name() == "memory", "se perdió la pestaña seleccionada")
        check(view_a._ctx_stack.get_visible_child() is memory_widget, "se recreó el contenido del panel")
        view_a._context_toggle.set_active(False)  # A queda oculto para el reinicio

        # Independiente por proyecto, y con el atajo Mayús+F9 (acción win.toggle-context).
        win.open_project(b.id)
        view_b = win._workspaces[b.id]
        pump(timeout=0.2)
        check(view_b._context_pane.get_visible(), "ocultar el panel en A lo ocultó en B")
        win.activate_action("win.toggle-context", None)
        check(not view_b._context_pane.get_visible(), "el atajo no ocultó el panel")
        win.activate_action("win.toggle-context", None)
        check(view_b._context_pane.get_visible(), "el atajo no volvió a mostrar el panel")
        win.open_project(a.id)
        pump(timeout=0.2)
        check(not view_a._context_pane.get_visible(), "A no conservó su panel oculto al volver")

        # --- Conversaciones --------------------------------------------------------------
        check(view_a._chat_stack.get_visible_child_name() == "empty", "falta el estado vacío")
        view_a._open_uri = opened.append  # nunca abrir el navegador en las pruebas

        view_a.request_link_conversation()
        dialog = view_a._last_dialog
        for bad in ("hola", "https://example.com/c/" + CID_A, f"http://chatgpt.com/c/{CID_A}"):
            dialog.url_row.set_text(bad)
            check(not dialog.get_response_enabled("save"), f"se permitió guardar {bad!r}")
            check(dialog.feedback.has_css_class("error"), f"sin mensaje de error para {bad!r}")
        dialog.url_row.set_text(f"https://chat.openai.com/c/{CID_A}?model=x")  # formato histórico
        dialog.title_row.set_text("Arquitectura del backend")
        check(dialog.get_response_enabled("save"), "un enlace válido no se puede guardar")
        check("privada" in dialog.feedback.get_label().lower(), dialog.feedback.get_label())
        respond(view_a, "save")
        links_a = ctx.chat_links.list(a.id)
        check([(l.title, l.url) for l in links_a] == [("Arquitectura del backend", f"https://chatgpt.com/c/{CID_A}")],
              f"conversaciones de A: {links_a}")
        check(view_a._chat_stack.get_visible_child_name() == "list" and len(list_rows(view_a)) == 1,
              "la conversación no aparece en el panel")

        # Duplicado (otro formato del mismo enlace): se rechaza.
        view_a.request_link_conversation()
        view_a._last_dialog.url_row.set_text(f"https://chatgpt.com/c/{CID_A}")
        check(not view_a._last_dialog.get_response_enabled("save"), "se permitió un duplicado")
        check("ya está vinculada" in view_a._last_dialog.feedback.get_label(), "sin aviso de duplicado")
        respond(view_a, "cancel")

        # Enlace compartido: se identifica como tal.
        view_a.request_link_conversation()
        view_a._last_dialog.url_row.set_text(f"https://chatgpt.com/share/{CID_B}")
        check("compartido" in view_a._last_dialog.feedback.get_label().lower(), "el enlace compartido no se distingue")
        respond(view_a, "save")
        rows = list_rows(view_a)
        check(len(rows) == 2 and rows[0].get_subtitle().startswith("Compartida"),
              f"subtítulo: {rows[0].get_subtitle() if rows else None}")
        check(rows[0].get_title() == "Conversación compartida de ChatGPT", rows[0].get_title() if rows else "")

        # Abrir en el navegador (registrado, no abierto) y copiar el enlace.
        private_row = next(r for r in rows if r.get_subtitle().startswith("Privada"))
        private_row.activate()
        pump(timeout=0.1)
        view_a._copy_link(private_row.link_id)
        clip: dict = {}
        view_a.get_clipboard().read_text_async(None, lambda c, r: clip.setdefault("t", c.read_text_finish(r)))
        check(pump(lambda: "t" in clip), "no se pudo leer el portapapeles")
        check(clip.get("t") == f"https://chatgpt.com/c/{CID_A}", f"portapapeles: {clip.get('t')!r}")

        # Renombrar y desvincular (con confirmación).
        view_a._rename_link(private_row.link_id)
        view_a._last_dialog.entry.set_text("Backend · decisiones")
        respond(view_a, "rename")
        check(ctx.chat_links.get(private_row.link_id).title == "Backend · decisiones", "no se renombró")
        shared_id = next(l.id for l in ctx.chat_links.list(a.id) if l.kind == "shared")
        view_a._unlink(shared_id)
        respond(view_a, "cancel")
        check(ctx.chat_links.get(shared_id) is not None, "se desvinculó sin confirmar")
        view_a._unlink(shared_id)
        respond(view_a, "unlink")
        check(ctx.chat_links.get(shared_id) is None and len(list_rows(view_a)) == 1, "no se desvinculó")

        # Aislamiento: B tiene su propia lista; cambiar de proyecto no mezcla nada.
        check(view_b._chat_stack.get_visible_child_name() == "empty", "B muestra conversaciones de A")
        win.open_project(b.id)
        view_b.request_link_conversation()
        view_b._last_dialog.url_row.set_text(f"https://chatgpt.com/c/{CID_A}")  # el mismo chat, otro proyecto
        check(view_b._last_dialog.get_response_enabled("save"), "el mismo enlace debe poder vincularse en B")
        respond(view_b, "save")
        check(len(list_rows(view_b)) == 1 and len(list_rows(view_a)) == 1, "las listas se mezclaron")
        check([l.title for l in ctx.chat_links.list(a.id)] == ["Backend · decisiones"], "A cambió al vincular en B")

    # ---- segunda ejecución (tras reiniciar) ------------------------------------------------

    def _second_run(self, app, check) -> None:
        win, ctx = app._window, app.ctx
        projects = {p.name: p for p in ctx.projects.list()}
        win.open_project(projects["Alfa"].id)
        view_a = win._workspaces[projects["Alfa"].id]
        pump(timeout=0.2)
        check(not view_a._context_pane.get_visible() and not view_a._context_toggle.get_active(),
              "A no recuperó su panel derecho oculto tras reiniciar")
        check(view_a.state.context_tab == "memory", "A no recuperó su pestaña de contexto")
        check([row.get_title() for row in list_rows(view_a)] == ["Backend · decisiones"],
              "las conversaciones de A no persistieron")
        win.open_project(projects["Beta"].id)
        view_b = win._workspaces[projects["Beta"].id]
        pump(timeout=0.2)
        check(view_b._context_pane.get_visible(), "B debería tener el panel visible tras reiniciar")
        check([row.get_title() for row in list_rows(view_b)] == ["Conversación de ChatGPT"],
              "las conversaciones de B no persistieron")


if __name__ == "__main__":
    unittest.main()
