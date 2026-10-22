"""Copiar al seleccionar y selector de Apariencia con la aplicación real.

La selección se hace con **eventos reales de ratón** inyectados en el servidor Broadway
aislado (``broadway_input``): pasan por el sistema de eventos de GTK y por los gestos
propios de VTE, igual que con un ratón físico. Así se prueba la interacción completa
—VTE selecciona, IA Workspace copia— y no solo los callbacks. Las terminales ejecutan un
bash sin perfil ni historial; no se lanza ningún agente.
"""

from __future__ import annotations

import os
import re
import sys
import time
import traceback
import unittest
from pathlib import Path

from broadway_input import CONTROL_MASK, SHIFT_MASK, BroadwayInput, broadway_port
from helpers import temp_dir

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

try:
    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: E402,F401

    HAS_VTE = True
except (ValueError, ImportError):
    HAS_VTE = False

HAS_DISPLAY = Gtk.init_check() and Gdk.Display.get_default() is not None
PORT = broadway_port()
MARK = "MARCA_COPIA_uno"
LATTE_BASE, MOCHA_BASE = "#eff1f5", "#1e1e2e"


def pump(until=lambda: False, timeout: float = 3.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.004)
    return until()


def hex_color(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue)))


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE and PORT, "requiere Broadway aislado y VTE")
class CopyOnSelectRealInputTest(unittest.TestCase):
    def test_real_selection_preferences_and_themes(self) -> None:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.sessions.models import SessionState
        from ia_workspace.terminal.backend import COPY_ON_SELECT_KEY
        from ia_workspace.ui.dialogs.preferences import SCHEME_KEY, PreferencesDialog

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
            bw: BroadwayInput | None = None

            def check(condition: bool, message: str) -> None:
                if not condition:
                    errors.append(message)

            def scenario() -> bool:
                nonlocal bw
                try:
                    Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                    win, ctx = app._window, app.ctx
                    win.set_default_size(1200, 860)
                    a = ctx.projects.register("Alfa", str(Path(folders) / "alfa"), ())
                    b = ctx.projects.register("Beta", str(Path(folders) / "beta"), ())

                    def open_terminal(project) -> tuple[object, str]:
                        win.open_project(project.id)
                        view = win._workspaces[project.id]
                        sid = view.new_terminal()
                        check(pump(lambda: ctx.sessions.get(sid).state is SessionState.IDLE, 5), "terminal sin arrancar")
                        ctx.sessions.send_input(sid, f"PS1='$ '; clear; printf '%s\\n' {MARK}\n")
                        check(pump(lambda: re.search(rf"^{MARK}$", ctx.sessions.handle(sid).text(), re.M) is not None, 5),
                              "la terminal no mostró el texto de prueba")
                        pump(timeout=0.3)
                        return view, sid

                    view_a, term_a = open_terminal(a)
                    handle_a = ctx.sessions.handle(term_a)
                    pid_a = handle_a.pid
                    view_b, term_b = open_terminal(b)
                    handle_b = ctx.sessions.handle(term_b)
                    pid_b = handle_b.pid
                    win.open_project(a.id)
                    pump(timeout=0.5)

                    bw = BroadwayInput(PORT)
                    check(pump(lambda: bool(bw.surfaces), 3), "Broadway no informó de las superficies")
                    pump(timeout=0.5)
                    surface = bw.main_surface()
                    # Un primer evento que solo da foco a la ventana (como al acercar el ratón).
                    bw.move(surface, (5, 5), pump=lambda: pump(timeout=0.2))
                    pump(lambda: win.is_active(), 2)
                    tick = lambda: pump(timeout=0.03)  # noqa: E731

                    def cell(handle, col: float, text: str = MARK) -> tuple[float, float]:
                        term = handle.widget()
                        lines = handle.text().split("\n")
                        row = next(i for i, line in enumerate(lines) if line.rstrip() == text)
                        ok, point = term.compute_point(win, Graphene.Point().init(0, 0))
                        tx, ty = win.get_surface_transform()
                        return (tx + point.x + col * term.get_char_width(),
                                ty + point.y + (row + 0.5) * term.get_char_height())

                    def settle(handle) -> None:
                        """Espera a que la terminal deje de moverse o cambiar de tamaño."""
                        term = handle.widget()

                        def geometry():
                            ok, point = term.compute_point(win, Graphene.Point().init(0, 0))
                            return (round(point.x), round(point.y), term.get_width(), term.get_height())

                        last, stable_since = geometry(), time.monotonic()
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            pump(timeout=0.05)
                            now = geometry()
                            if now != last:
                                last, stable_since = now, time.monotonic()
                            elif time.monotonic() - stable_since > 0.4:
                                return

                    def select_mark(handle) -> None:
                        settle(handle)
                        bw.drag(surface, cell(handle, 0.3), cell(handle, len(MARK) + 0.4), pump=tick)
                        pump(timeout=0.3)

                    def clipboard_text() -> str | None:
                        got: dict = {}

                        def done(cb, result) -> None:
                            try:
                                got["t"] = cb.read_text_finish(result)
                            except GLib.Error:
                                got["t"] = None

                        win.get_clipboard().read_text_async(None, done)
                        pump(lambda: "t" in got, 2)
                        return got.get("t")

                    copies_a: list[int] = []
                    copies_b: list[int] = []
                    handle_a.add_copy_listener(lambda: copies_a.append(1))
                    handle_b.add_copy_listener(lambda: copies_b.append(1))
                    revealer_a = handle_a.copy_revealer()
                    check(revealer_a.get_parent() is not None
                          and revealer_a.get_parent() is handle_a.widget().get_parent().get_parent(),
                          "la notificación no está en el overlay de su terminal")

                    # 1-3. Selección real con el ratón → copia exacta + notificación que se oculta.
                    select_mark(handle_a)
                    check(handle_a.selected_text() == MARK, f"VTE seleccionó {handle_a.selected_text()!r}")
                    check(clipboard_text() == MARK, f"portapapeles: {clipboard_text()!r}")
                    check(len(copies_a) == 1, f"copias tras una selección: {len(copies_a)}")
                    check(revealer_a.get_reveal_child() and revealer_a.get_child_revealed(),
                          "no se mostró «Copiado al portapapeles»")
                    check(pump(lambda: not revealer_a.get_reveal_child(), 2.5), "la notificación no se ocultó sola")
                    check(not handle_b.copy_revealer().get_reveal_child(), "notificó en la terminal de otro proyecto")

                    # 4. Volver a seleccionar el mismo texto con el portapapeles cambiado: copia otra vez,
                    #    exactamente una notificación más.
                    win.get_clipboard().set("otro contenido")
                    select_mark(handle_a)
                    check(clipboard_text() == MARK, "no se volvió a copiar el mismo texto")
                    check(len(copies_a) == 2, f"copias tras repetir: {len(copies_a)}")

                    # 5. Ni un clic simple ni mover el ratón copian nada.
                    win.get_clipboard().set("sin cambios")
                    settle(handle_a)
                    bw.click(surface, cell(handle_a, 3.3), pump=tick)
                    pump(timeout=0.3)
                    for col in (1, 5, 9):
                        bw.move(surface, cell(handle_a, col), pump=tick)
                    pump(timeout=0.3)
                    check(clipboard_text() == "sin cambios" and len(copies_a) == 2, "un clic o movimiento copió")

                    # 6. Una TUI con ratón activado recibe el arrastre (VTE no selecciona): no se copia.
                    #    Un programa mínimo hace de TUI: activa el modo ratón, consume los informes
                    #    del ratón durante 2 s y lo desactiva.
                    ctx.sessions.send_input(
                        term_a,
                        "printf '\\033[?1000h'; echo RATON_TUI_ON; read -rs -t 2 -n 4096 _; "
                        "printf '\\033[?1000l'; clear; printf '%s\\n' " + MARK + "\n",
                    )
                    check(pump(lambda: re.search(r"^RATON_TUI_ON$", handle_a.text(), re.M) is not None, 3),
                          "no se activó el modo ratón")
                    select_mark(handle_a)
                    check(clipboard_text() == "sin cambios" and len(copies_a) == 2,
                          "se copió durante el modo ratón de una TUI")
                    check(pump(lambda: handle_a.text().split("\n")[0] == MARK, 5), "la TUI de prueba no terminó")
                    pump(timeout=0.3)

                    # 7. Desactivado desde Preferencias: no copia al seleccionar; Ctrl+Mayús+C sí.
                    prefs = PreferencesDialog(ctx)
                    prefs.present(win)
                    pump(timeout=0.2)
                    prefs.copy_on_select_row.set_active(False)
                    check(ctx.settings.get(COPY_ON_SELECT_KEY) == "false", "la preferencia no se guardó")
                    prefs.force_close()
                    pump(timeout=0.2)
                    select_mark(handle_a)
                    check(handle_a.selected_text() == MARK, "no hay selección para la prueba manual")
                    check(clipboard_text() == "sin cambios" and len(copies_a) == 2, "copió con la preferencia desactivada")
                    bw.key(ord("C"), CONTROL_MASK | SHIFT_MASK, pump=tick)
                    pump(timeout=0.3)
                    check(clipboard_text() == MARK and len(copies_a) == 3, "Ctrl+Mayús+C no copió")
                    win.get_clipboard().set("antes del menú")
                    handle_a.widget().activate_action("term.copy", None)
                    pump(timeout=0.2)
                    check(clipboard_text() == MARK, "«Copiar» del menú contextual no funcionó")
                    prefs = PreferencesDialog(ctx)
                    prefs.present(win)
                    check(not prefs.copy_on_select_row.get_active(), "Preferencias no refleja el ajuste guardado")
                    prefs.copy_on_select_row.set_active(True)
                    prefs.force_close()

                    # 10. Otra sesión de otro proyecto: copia en su terminal, notifica solo allí.
                    win.open_project(b.id)
                    pump(timeout=0.5)
                    before_a = len(copies_a)
                    select_mark(handle_b)
                    check(clipboard_text() == MARK and copies_b == [1] and len(copies_a) == before_a,
                          "la copia en Beta no fue independiente de Alfa")
                    check(handle_b.copy_revealer().get_reveal_child() and not revealer_a.get_reveal_child(),
                          "la notificación apareció en la terminal equivocada")

                    # 11. Teclado intacto: escribir de verdad en la terminal sigue funcionando.
                    bw.type_text("echo TECLADO_$((40+2))\n", pump=tick)
                    check(pump(lambda: "TECLADO_42" in handle_b.text(), 3), "el teclado dejó de llegar a la terminal")

                    # 8-9. Apariencia: Latte, Mocha y Automático con terminales abiertas.
                    manager = Adw.StyleManager.get_default()
                    prefs = PreferencesDialog(ctx)
                    prefs.present(win)
                    for key, dark, base in (("light", False, LATTE_BASE), ("dark", True, MOCHA_BASE),
                                            ("light", False, LATTE_BASE)):
                        prefs.scheme_checks[key].set_active(True)
                        pump(timeout=0.2)
                        check(ctx.settings.get(SCHEME_KEY) == key, f"no se guardó la apariencia {key}")
                        check(manager.get_dark() is dark, f"{key}: el estilo no cambió")
                        for handle in (handle_a, handle_b):
                            got = hex_color(handle.widget().get_color_background_for_draw())
                            check(got == base, f"{key}: fondo de terminal {got}, se esperaba {base}")
                    prefs.scheme_checks["system"].set_active(True)
                    pump(timeout=0.2)
                    check(manager.get_color_scheme() is Adw.ColorScheme.DEFAULT, "Automático no sigue al sistema")
                    expected = MOCHA_BASE if manager.get_dark() else LATTE_BASE
                    check(hex_color(handle_a.widget().get_color_background_for_draw()) == expected,
                          "Automático: la terminal no sigue el modo del sistema")
                    check(sum(c.get_active() for c in prefs.scheme_checks.values()) == 1, "más de una opción marcada")
                    prefs.scheme_checks["dark"].set_active(True)  # queda Mocha para la prueba de reinicio
                    prefs.force_close()

                    # Las sesiones siguen intactas tras todos los cambios.
                    for sid, pid in ((term_a, pid_a), (term_b, pid_b)):
                        info = ctx.sessions.get(sid)
                        check(info.state.is_active and ctx.sessions.handle(sid).pid == pid,
                              "una terminal se reinició o se detuvo")
                except Exception:  # noqa: BLE001
                    errors.append(traceback.format_exc())
                if bw is not None:
                    bw.close()
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

            # 7. Persistencia tras reiniciar: se leen de la base de datos de esa sesión.
            from ia_workspace.context import AppContext
            from ia_workspace.persistence.database import Database
            from ia_workspace.ui.dialogs.preferences import apply_color_scheme

            db = Database(Path(data) / "ia-workspace.db")
            ctx = AppContext.create(db)
            try:
                self.assertEqual(ctx.settings.get(SCHEME_KEY), "dark")
                self.assertEqual(ctx.settings.get(COPY_ON_SELECT_KEY), "true")
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
                apply_color_scheme(ctx)  # lo que hace la aplicación al arrancar
                self.assertIs(Adw.StyleManager.get_default().get_color_scheme(), Adw.ColorScheme.FORCE_DARK)
            finally:
                Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
                db.close()
        self.assertEqual(errors, [], "\n".join(errors))


if __name__ == "__main__":
    unittest.main()
