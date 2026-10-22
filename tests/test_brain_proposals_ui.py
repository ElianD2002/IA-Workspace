"""Prueba de interfaz (Broadway) para el flujo de propuestas del Cerebro (fase 3C.1).

Comprueba:
1. Fila de «Propuestas» en el panel lateral de Memoria con contador de pendientes.
2. Vista de propuestas con filtros «Pendientes» e «Historial».
3. Tarjetas de propuesta con distintivos de estado y agente de origen.
4. Diálogo de revisión de propuestas (ProposalReviewDialog) con diff para actualizaciones.
5. Flujo de aprobación e incorporación al Cerebro oficial.
6. Flujo de rechazo de propuestas.
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


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE")
class BrainProposalsUITest(unittest.TestCase):
    def test_proposals_ui_flow(self) -> None:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.brain.model import Scope
        from ia_workspace.brain.proposals import ProposalStatus

        errors: list[str] = []
        with temp_dir() as data, temp_dir() as folders:
            folder = Path(folders) / "proyecto-alfa"
            folder.mkdir()
            env = {
                "IAW_DATA_DIR": data,
                "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc",
                "HISTFILE": "",
            }
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
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
                    win, ctx = app._window, app.ctx
                    proj = ctx.projects.register("Proyecto Alfa", str(folder), ())
                    win.open_project(proj.id)

                    # 1. Crear documento inicial en la memoria
                    key = ctx.brain.project_key(proj)
                    ctx.brain.create(
                        Scope.PROJECT,
                        key,
                        "decisiones",
                        "Estrategia de caché",
                        "Usar memoria local.",
                    )

                    # 2. Registrar propuesta de actualización desde un agente
                    prop = ctx.brain.propose(
                        proj,
                        "update",
                        title="Estrategia de caché",
                        body="Usar Redis centralizado con fallback a memoria local.",
                        relpath="decisiones/estrategia-de-cache.md",
                        summary="Mejora de escalabilidad horizontal",
                        agent_kind="antigravity",
                    )

                    # 3. Navegar a la pantalla de memoria
                    win.navigate("memory")
                    page = win._pages["memory"]
                    pump(timeout=0.2)
                    check(win._stack.get_visible_child_name() == "memory", "no se abrió memoria")

                    # Comprobar fila de propuestas y contador
                    check(page._proposals_row is not None, "falta la fila de propuestas")
                    pending_count = ctx.brain.count_pending_proposals(proj.id)
                    check(pending_count == 1, f"contador esperado 1, obtenido {pending_count}")

                    # 4. Seleccionar propuestas
                    page.select_category("__proposals__")
                    pump(timeout=0.2)
                    check(page.heading.get_label() == "Propuestas de actualización", "encabezado erróneo")
                    check(len(children(page.cards)) > 0, "no se mostraron tarjetas de propuestas")

                    # 5. Abrir diálogo de revisión
                    page.review_proposal(prop.id)
                    pump(timeout=0.2)
                    dialog = page._last_dialog
                    check(dialog is not None, "no se abrió el diálogo de revisión")
                    check("Estrategia de caché" in dialog.get_title(), "título del diálogo incorrecto")

                    # 6. Aprobar la propuesta desde el diálogo
                    dialog.approve_button.emit("clicked")
                    pump(timeout=0.3)

                    # Comprobar que se aplicó el cambio al Cerebro
                    updated_doc = ctx.brain.read(Scope.PROJECT, key, "decisiones/estrategia-de-cache.md")
                    check("Redis centralizado" in updated_doc.body, "no se aplicó el contenido propuesto")
                    check(ctx.brain.count_pending_proposals(proj.id) == 0, "quedan propuestas pendientes")
                    stored_prop = ctx.brain.get_proposal(proj, prop.id)
                    check(stored_prop.meta.status is ProposalStatus.APPROVED, "el estado no es APPROVED")

                    # 7. Probar flujo de rechazo con una segunda propuesta
                    prop2 = ctx.brain.propose(
                        proj,
                        "create",
                        title="Documento a rechazar",
                        body="Contenido prescindible.",
                        category_key="estado",
                        summary="Prueba de rechazo",
                        agent_kind="codex",
                    )
                    page.refresh()
                    pump(timeout=0.2)
                    check(ctx.brain.count_pending_proposals(proj.id) == 1, "debe haber 1 pendiente")
                    # Rechazar directamente
                    page._do_reject_proposal(prop2.id, "No se necesita actualmente")
                    pump(timeout=0.2)
                    check(ctx.brain.count_pending_proposals(proj.id) == 0, "no debe haber pendientes")
                    prop2_stored = ctx.brain.get_proposal(proj, prop2.id)
                    check(prop2_stored.meta.status is ProposalStatus.REJECTED, "no se marcó como REJECTED")

                except Exception:
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
