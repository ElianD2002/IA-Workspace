"""Pantalla «Cerebro compartido» con la aplicación real (fase 3A).

Necesita display (Broadway aislado en ``scripts/check.sh``). Una terminal real trabaja
durante toda la prueba para comprobar que la memoria no interrumpe ninguna sesión.
"""

from __future__ import annotations

import os
import re
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

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: E402,F401

    HAS_VTE = True
except (ValueError, ImportError):
    HAS_VTE = False

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None


def pump(until=lambda: False, timeout: float = 3.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def children(box: Gtk.Widget) -> list[Gtk.Widget]:
    result, child = [], box.get_first_child()
    while child is not None:
        result.append(child)
        child = child.get_next_sibling()
    return result


def card_titles(page) -> list[str]:
    titles = []
    for card in children(page.cards):
        if hasattr(card, "relpath"):
            titles.append(children(children(card)[0])[0].get_label())
    return titles


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE")
class BrainScreenTest(unittest.TestCase):
    def test_brain_screen(self) -> None:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.brain.model import Scope
        from ia_workspace.sessions.models import SessionState

        errors: list[str] = []
        with temp_dir() as data, temp_dir() as folders:
            for name in ("alfa", "beta"):
                (Path(folders) / name).mkdir()
            env = {"IAW_DATA_DIR": data, "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc", "HISTFILE": ""}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            app = IAWorkspaceApplication()
            app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
            previous = sys.excepthook
            sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

            def check(condition: bool, message: str) -> None:
                if not condition:
                    errors.append(message)

            def respond(page, response: str) -> None:
                page._last_dialog.emit("response", response)
                page._last_dialog.force_close()
                pump(timeout=0.1)

            def scenario() -> bool:
                try:
                    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                    win, ctx = app._window, app.ctx
                    a = ctx.projects.register("Alfa", str(Path(folders) / "alfa"), ())
                    b = ctx.projects.register("Beta", str(Path(folders) / "beta"), ())

                    # Una terminal real trabaja durante toda la prueba.
                    win.open_project(a.id)
                    view_a = win._workspaces[a.id]
                    term = view_a.new_terminal()
                    check(pump(lambda: ctx.sessions.get(term).state is SessionState.IDLE, 5), "terminal sin arrancar")
                    ctx.sessions.send_input(term, "for i in $(seq 1 600); do echo TICK $i; sleep 0.05; done\n")
                    term_pid = ctx.sessions.handle(term).pid

                    def ticks() -> int:
                        found = re.findall(r"TICK (\d+)", ctx.sessions.handle(term).text())
                        return max(map(int, found)) if found else 0

                    # La pantalla abre la memoria del último proyecto activo.
                    win.navigate("memory")
                    page = win._pages["memory"]
                    pump(timeout=0.2)
                    check(win._stack.get_visible_child_name() == "memory", "no se abrió el Cerebro")
                    check(page.scope is Scope.PROJECT, "debería empezar en la memoria del proyecto")
                    check(page.scope_toggle.get_toggle_by_name("project").get_label() == "Alfa",
                          "el selector no muestra el proyecto")
                    check(len(page._category_rows) == 7, f"categorías de proyecto: {list(page._category_rows)}")
                    check("Memoria del proyecto «Alfa»" in page.scope_label.get_label(), "no distingue la memoria")

                    # Crear un documento con el editor.
                    page.select_category("decisiones")
                    page.request_new()
                    editor = page._last_dialog
                    editor.title_row.set_text("ADR-006 · Fechas en UTC")
                    editor.tags_row.set_text("fechas, utc")
                    editor.body_view.get_buffer().set_text(
                        "Las fechas se guardan en **UTC**.\n\n## Consecuencias\n- Convertir al presentar.")
                    check(editor.save(), f"no se guardó: {editor.error.get_label()}")
                    pump(timeout=0.2)
                    check(card_titles(page) == ["ADR-006 · Fechas en UTC"], f"tarjetas: {card_titles(page)}")
                    check(page._category_counts["decisiones"].get_label() == "1", "el contador no se actualizó")
                    key_a = ctx.brain.project_key(a)
                    doc_path = ctx.brain.root / "projects" / key_a / "decisiones" / "adr-006-fechas-en-utc.md"
                    check(doc_path.is_file(), "no se creó el archivo Markdown")
                    check("[ADR-006 · Fechas en UTC]" in ctx.brain.index_path(Scope.PROJECT, key_a).read_text(),
                          "INDEX.md no se actualizó")

                    # Un documento con credenciales no se guarda; el texto escrito se conserva.
                    page.request_new()
                    editor = page._last_dialog
                    editor.title_row.set_text("Claves")
                    editor.body_view.get_buffer().set_text("token: ghp_" + "A" * 36)
                    check(not editor.save(), "se guardó un documento con un token")
                    check(editor.error.get_visible() and "credenciales" in editor.error.get_label(),
                          "no se explicó el rechazo")
                    check(editor.body().startswith("token: ghp_"), "se perdió el texto escrito")
                    editor.force_close()
                    check(card_titles(page) == ["ADR-006 · Fechas en UTC"], "apareció un documento rechazado")

                    # Ver el documento: Markdown renderizado, no el texto en bruto.
                    page.open_document("decisiones/adr-006-fechas-en-utc.md")
                    viewer = page._last_dialog
                    rendered = viewer.rendered_text()
                    check("Consecuencias" in rendered and "##" not in rendered and "**" not in rendered,
                          f"Markdown sin renderizar: {rendered!r}")
                    check("• Convertir al presentar." in rendered, "la lista no se renderizó")
                    viewer.force_close()

                    # Búsqueda selectiva: fragmento y categoría, sin abrir nada más.
                    page.set_query("conversion")
                    check(page.heading.get_label() == "Resultados de «conversion»", "no entró en modo búsqueda")
                    check(card_titles(page) == [], "una búsqueda sin coincidencias mostró documentos")
                    page.set_query("presentar")
                    check(card_titles(page) == ["ADR-006 · Fechas en UTC"], "la búsqueda no encontró el documento")
                    page.set_query("")
                    check(page.heading.get_label() == "Decisiones técnicas", "no volvió a la categoría")

                    # Editar (la ruta se conserva) y eliminar con confirmación.
                    page.request_edit("decisiones/adr-006-fechas-en-utc.md")
                    editor = page._last_dialog
                    check(not editor.module_row.get_visible(), "decisiones no lleva módulo")
                    editor.title_row.set_text("ADR-006 · Fechas siempre en UTC")
                    check(editor.save(), "no se guardó la edición")
                    check(card_titles(page) == ["ADR-006 · Fechas siempre en UTC"], "la edición no se reflejó")
                    check(doc_path.is_file(), "editar cambió la ruta del documento")
                    page.request_delete("decisiones/adr-006-fechas-en-utc.md")
                    respond(page, "cancel")
                    check(doc_path.is_file(), "se eliminó sin confirmar")
                    page.request_delete("decisiones/adr-006-fechas-en-utc.md")
                    respond(page, "delete")
                    check(not doc_path.exists() and card_titles(page) == [], "no se eliminó")
                    check(any((ctx.brain.root / ".trash").rglob("*.md")), "no está en la papelera")

                    # Documentación por módulo.
                    page.select_category("modulos")
                    page.request_new()
                    editor = page._last_dialog
                    check(editor.module_row.get_visible(), "falta el campo de módulo")
                    editor.title_row.set_text("Webhooks")
                    editor.module_row.set_text("Pagos")
                    editor.body_view.get_buffer().set_text("Reciben eventos firmados.")
                    check(editor.save(), f"no se guardó el documento de módulo: {editor.error.get_label()}")

                    # Memoria global: distinta y separada de la del proyecto.
                    page.select_scope(Scope.GLOBAL)
                    check(len(page._category_rows) == 3 and "Memoria global" in page.scope_label.get_label(),
                          "no se distingue la memoria global")
                    check(not page.banner.get_button_label(), "la memoria global no ofrece instrucción de agente")
                    page.select_category("preferencias")
                    page.request_new()
                    editor = page._last_dialog
                    editor.title_row.set_text("Idioma")
                    editor.body_view.get_buffer().set_text("Responde siempre en español.")
                    check(editor.save(), "no se guardó en la memoria global")
                    check((ctx.brain.root / "global" / "preferencias" / "idioma.md").is_file(), "global no persistió")
                    page.select_scope(Scope.PROJECT)
                    page.set_query("idioma")
                    check(card_titles(page) == [], "la memoria global apareció dentro del proyecto")
                    page.set_query("")

                    # Aislamiento: Beta no ve nada de Alfa.
                    page.select_project(b.id)
                    check(page.scope_toggle.get_toggle_by_name("project").get_label() == "Beta", "no cambió de proyecto")
                    check(sum(int(l.get_label()) for l in page._category_counts.values()) == 0,
                          "Beta muestra documentos de Alfa")
                    page.set_query("webhooks")
                    check(card_titles(page) == [], "la búsqueda de Beta encontró documentos de Alfa")
                    page.set_query("")
                    page.select_project(a.id)

                    # Instrucción para agentes: referencia al índice, nunca el contenido.
                    brief = page.copy_agent_brief()
                    check(brief is not None and str(ctx.brain.index_path(Scope.PROJECT, key_a)) in brief,
                          "la instrucción no apunta a INDEX.md")
                    check("Reciben eventos firmados" not in (brief or ""), "la instrucción incluye la memoria")
                    clip: dict = {}
                    page.get_clipboard().read_text_async(None, lambda c, r: clip.setdefault("t", c.read_text_finish(r)))
                    check(pump(lambda: "t" in clip) and clip["t"] == brief, "no se copió la instrucción")

                    # El panel «Memoria» del workspace refleja la memoria del proyecto.
                    win.open_project(a.id)
                    check("1 documento" in view_a._memory_hint.get_label(), view_a._memory_hint.get_label())

                    # Las sesiones siguen intactas.
                    before = ticks()
                    check(pump(lambda: ticks() > before + 5, 5), "la terminal dejó de avanzar")
                    check(ctx.sessions.handle(term).pid == term_pid and ctx.sessions.get(term).state.is_active,
                          "la terminal se reinició o se detuvo")
                    check(view_a._browser is None, "el Cerebro no debería tocar la vista de ChatGPT")
                except Exception:  # noqa: BLE001
                    errors.append(traceback.format_exc())
                app._shutdown_sessions_and_quit()
                return GLib.SOURCE_REMOVE

            app.connect("activate", lambda *_: GLib.idle_add(scenario))
            try:
                app.run([])
            finally:
                sys.excepthook = previous
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
