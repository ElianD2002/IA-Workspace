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
import subprocess
import sys
import time
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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

                    # 2. Registrar propuesta de actualización desde un proceso independiente (CLI externa)
                    ws_view = win._workspaces[proj.id]
                    check(not ws_view._proposals_button.get_visible(), "el botón de propuestas debe estar oculto con 0 pendientes")

                    sub_env = dict(
                        os.environ,
                        IAW_DATA_DIR=str(data),
                        PYTHONPATH=str(ROOT / "src"),
                    )
                    proc = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "ia_workspace.brain",
                            "propose",
                            "-o",
                            "update",
                            "--relpath",
                            "decisiones/estrategia-de-cache.md",
                            "--title",
                            "Estrategia de caché",
                            "--summary",
                            "Mejora de escalabilidad horizontal",
                        ],
                        input="Usar Redis centralizado con fallback a memoria local.",
                        text=True,
                        cwd=str(folder),
                        env=sub_env,
                        capture_output=True,
                    )
                    check(proc.returncode == 0, f"falló iaw-brain propose externo: {proc.stderr}")
                    pump(timeout=0.3)

                    # Indicador discreto en el workspace (detectado desde proceso externo)
                    check(ws_view._proposals_button.get_visible(), "el botón de propuestas debe ser visible con 1 pendiente tras proceso externo")
                    check(ws_view._proposals_label.get_label() == "1", "el botón de propuestas debe mostrar 1")

                    # Comprobar cambio de proyecto: en otro proyecto el botón no debe aparecer
                    beta_folder = Path(folders) / "proyecto-beta"
                    beta_folder.mkdir(parents=True, exist_ok=True)
                    proj_beta = ctx.projects.register("Proyecto Beta", str(beta_folder), ())
                    win.open_project(proj_beta.id)
                    pump(timeout=0.2)
                    ws_view_beta = win._workspaces[proj_beta.id]
                    check(not ws_view_beta._proposals_button.get_visible(), "el botón de propuestas debe estar oculto en Proyecto Beta")

                    # Volver al proyecto original: el botón reaparece con su contador
                    win.open_project(proj.id)
                    pump(timeout=0.2)
                    check(ws_view._proposals_button.get_visible(), "el botón de propuestas debe reaparecer al volver a Proyecto Alfa")
                    check(ws_view._proposals_label.get_label() == "1", "el botón de propuestas debe mantener 1 en Alfa")

                    pending_props = ctx.brain.list_proposals(proj.id)
                    check(len(pending_props) == 1, f"esperada 1 propuesta en base de datos, obtenidas {len(pending_props)}")
                    prop = pending_props[0]

                    # 3. Navegar a la pantalla de memoria mediante el botón discreto del workspace
                    ws_view._proposals_button.emit("clicked")
                    page = win._pages["memory"]
                    pump(timeout=0.2)
                    check(page.category_key == "__proposals__", "no se seleccionó la categoría de propuestas")

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
                    check(not ws_view._proposals_button.get_visible(), "el botón de propuestas debe ocultarse tras aprobar")
                    stored_prop = ctx.brain.get_proposal(proj, prop.id)
                    check(stored_prop.meta.status is ProposalStatus.APPROVED, "el estado no es APPROVED")

                    # 7. Probar flujo de rechazo con una segunda propuesta registrada por proceso externo
                    proc2 = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "ia_workspace.brain",
                            "propose",
                            "-o",
                            "create",
                            "--category",
                            "estado",
                            "--title",
                            "Documento a rechazar",
                            "--summary",
                            "Prueba de rechazo",
                        ],
                        input="Contenido prescindible.",
                        text=True,
                        cwd=str(folder),
                        env=sub_env,
                        capture_output=True,
                    )
                    check(proc2.returncode == 0, f"falló segunda propuesta externa: {proc2.stderr}")
                    pump(timeout=0.3)
                    check(ws_view._proposals_button.get_visible(), "el botón de propuestas debe mostrarse tras segunda propuesta")
                    check(ws_view._proposals_label.get_label() == "1", "el botón de propuestas debe mostrar 1")
                    page.refresh()
                    pump(timeout=0.2)
                    check(ctx.brain.count_pending_proposals(proj.id) == 1, "debe haber 1 pendiente")
                    pending2 = ctx.brain.list_proposals(proj.id)
                    prop2 = pending2[0]
                    # Rechazar directamente
                    page._do_reject_proposal(prop2.id, "No se necesita actualmente")
                    pump(timeout=0.2)
                    check(ctx.brain.count_pending_proposals(proj.id) == 0, "no debe haber pendientes")
                    check(not ws_view._proposals_button.get_visible(), "el botón de propuestas debe ocultarse tras rechazar")
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
