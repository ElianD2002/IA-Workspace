"""Terminales VTE reales dentro de la aplicación real (fase 2A).

Requiere display (``scripts/check.sh`` usa Broadway aislado) y gir1.2-vte-3.91.
Usa bash sin perfiles ni historial y carpetas de proyecto en ``var/tmp-tests``:
no toca proyectos ni archivos del usuario.
"""

from __future__ import annotations

import os
import re
import subprocess
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
TEST_SHELL = "/bin/bash --noprofile --norc"


def pump(until=lambda: False, timeout: float = 5.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def last_tick(text: str, tag: str) -> int:
    values = [int(n) for n in re.findall(rf"{tag} (\d+)", text)]
    return max(values) if values else 0


def proc_alive(pid: int) -> bool:
    from ia_workspace.terminal.process import read_proc

    info = read_proc(pid)
    return info is not None and info.alive


def proc_cwd(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/cwd")


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE para GTK 4")
class TerminalLifecycleTest(unittest.TestCase):
    def test_terminals_end_to_end(self) -> None:
        with temp_dir() as data, temp_dir() as folders:
            dir_a = Path(folders) / "proyecto alfa"  # con espacio: rutas reales del usuario
            dir_b = Path(folders) / "proyecto-beta"
            dir_a.mkdir()
            dir_b.mkdir()
            env = {"IAW_DATA_DIR": data, "IAW_TERMINAL_SHELL": TEST_SHELL, "HISTFILE": ""}
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                errors, managed = self._run(dir_a, dir_b)
                # 9. Tras el cierre controlado no queda ningún proceso gestionado.
                leftovers = [pid for pid in managed if proc_alive(pid)]
                deadline = time.monotonic() + 3
                while leftovers and time.monotonic() < deadline:
                    time.sleep(0.05)
                    leftovers = [pid for pid in managed if proc_alive(pid)]
                if not managed:
                    errors.append("no se registraron procesos gestionados")
                if leftovers:
                    errors.append(f"procesos huérfanos tras salir: {leftovers}")
                if bystander.poll() is not None:
                    errors.append("se finalizó un proceso ajeno a IA Workspace")
            finally:
                bystander.kill()
                bystander.wait()
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            self.assertEqual(errors, [], "\n".join(errors))

    # ---------------------------------------------------------------------------

    def _run(self, dir_a: Path, dir_b: Path) -> tuple[list[str], set[int]]:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionState

        errors: list[str] = []
        managed: set[int] = set()
        app = IAWorkspaceApplication()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        previous_hook = sys.excepthook
        sys.excepthook = lambda *exc: errors.append("".join(traceback.format_exception(*exc)))

        def check(condition: bool, message: str) -> None:
            if not condition:
                errors.append(message)

        def scenario() -> bool:
            try:
                # Broadway sin navegador no completa animaciones: transiciones instantáneas.
                Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                win, ctx = app._window, app.ctx
                check(ctx.sessions.backend_for(AgentKind.SHELL) is not None, "backend VTE no registrado")
                a = ctx.projects.register("Alfa", str(dir_a), ())
                b = ctx.projects.register("Beta", str(dir_b), ())

                def session(sid):
                    return ctx.sessions.get(sid)

                def handle(sid):
                    return ctx.sessions.handle(sid)

                def text(sid) -> str:
                    return handle(sid).text()

                def run(sid, command: str) -> None:
                    ctx.sessions.send_input(sid, command + "\n")

                def ready(sid) -> bool:
                    return pump(lambda: session(sid).state is SessionState.IDLE and "$" in text(sid), 5)

                # 1-2. Terminal real en la carpeta del proyecto A.
                win.open_project(a.id)
                view_a = win._workspaces[a.id]
                a1 = view_a.new_terminal()
                check(ready(a1), f"la terminal de A no arrancó: {session(a1).state} {session(a1).detail}")
                pid_a1 = handle(a1).pid
                check(os.getsid(pid_a1) == pid_a1, "la terminal no tiene su propia sesión POSIX")
                check(proc_cwd(pid_a1) == str(dir_a), f"cwd de A incorrecto: {proc_cwd(pid_a1)}")
                run(a1, 'echo "IAW_PWD=$(pwd)"')
                check(pump(lambda: f"IAW_PWD={dir_a}" in text(a1)), "pwd no coincide con la carpeta de A")
                widget_a1 = handle(a1).widget()

                # 3. Proceso activo también en B, simultáneo al de A.
                run(a1, "for i in $(seq 1 200); do echo TICK_A $i; sleep 0.05; done")
                win.open_project(b.id)
                view_b = win._workspaces[b.id]
                b1 = view_b.new_terminal()
                check(ready(b1), "la terminal de B no arrancó")
                pid_b1 = handle(b1).pid
                check(proc_cwd(pid_b1) == str(dir_b), f"cwd de B incorrecto: {proc_cwd(pid_b1)}")
                run(b1, "for i in $(seq 1 200); do echo TICK_B $i; sleep 0.05; done")
                check(pump(lambda: last_tick(text(a1), "TICK_A") > 0 and last_tick(text(b1), "TICK_B") > 0),
                      "los dos proyectos no ejecutan procesos a la vez")

                # 4. Con A oculto (y luego sin ningún workspace visible) su proceso progresa.
                before = last_tick(text(a1), "TICK_A")
                pump(timeout=1.0)
                hidden_progress = last_tick(text(a1), "TICK_A")
                check(not widget_a1.get_mapped(), "la terminal de A debería estar oculta")
                check(hidden_progress > before + 5, f"A no progresó oculto ({before} → {hidden_progress})")
                win.navigate("agents")
                before_b = last_tick(text(b1), "TICK_B")
                pump(timeout=1.0)
                check(last_tick(text(b1), "TICK_B") > before_b + 5, "B no progresó al navegar a Agentes")
                check(handle(a1).pid == pid_a1 and handle(b1).pid == pid_b1, "un proceso se reinició")

                # 5. Minimizar y restaurar conserva las sesiones y su progreso.
                win.minimize()
                before = last_tick(text(a1), "TICK_A")
                pump(timeout=1.0)
                check(last_tick(text(a1), "TICK_A") > before + 5, "A no progresó con la ventana minimizada")
                win.present()
                pump(timeout=0.2)
                check(all(session(s).state is SessionState.IDLE for s in (a1, b1)), "minimizar alteró sesiones")

                # 6. Volver a A: misma terminal, mismo proceso, se puede seguir usando.
                win.open_project(a.id)
                pump(timeout=0.3)
                check(view_a.state.selected_session == a1, "A no recuperó su terminal seleccionada")
                check(handle(a1).widget() is widget_a1 and widget_a1.get_mapped(), "A no muestra su terminal original")
                check(pump(lambda: last_tick(text(a1), "TICK_A") >= 200, 25), "el contador de A no terminó")
                run(a1, "echo VUELTA_A_ALFA")
                check(pump(lambda: text(a1).count("VUELTA_A_ALFA") >= 2), "la terminal original de A no responde")
                check(handle(a1).pid == pid_a1, "la terminal de A se reinició")

                # Segunda terminal en A con un proceso en primer plano.
                a2 = view_a.new_terminal()
                check(ready(a2), "la segunda terminal de A no arrancó")
                check(a2 != a1 and handle(a2).pid != pid_a1, "las terminales no son independientes")
                run(a2, "sleep 300")
                check(pump(lambda: handle(a2).has_foreground_work()), "no se detecta el proceso en curso")
                a2_pids = handle(a2).managed_pids()

                # Seleccionar sesión: cambiar entre a1 y a2 no reinicia ninguna.
                view_a._select_session(a1)
                check(view_a._output_stack.get_visible_child_name() == a1, "no se mostró la terminal a1")
                view_a._select_session(a2)
                check(view_a._output_stack.get_visible_child_name() == a2, "no se mostró la terminal a2")

                # 7. Cerrar a2 (con confirmación) no afecta a otras terminales.
                view_a._close_selected()
                dialog = getattr(view_a, "_last_dialog", None)
                check(dialog is not None and session(a2).state.is_active, "cerrar con procesos no pidió confirmación")
                if dialog is not None:
                    dialog.emit("response", "confirm")
                    dialog.force_close()
                check(pump(lambda: session(a2) is None, 8), "la terminal a2 no se cerró")
                check(pump(lambda: not any(proc_alive(p) for p in a2_pids), 3), "quedaron procesos de a2")
                check(session(a1).state is SessionState.IDLE and proc_alive(pid_a1), "cerrar a2 afectó a a1")
                check(session(b1).state is SessionState.IDLE and proc_alive(pid_b1), "cerrar a2 afectó a B")

                # Detener una terminal (con confirmación) y conservar su salida final.
                b2 = view_b.new_terminal()
                check(ready(b2), "la segunda terminal de B no arrancó")
                run(b2, "echo SALIDA_FINAL_B2")
                pump(lambda: text(b2).count("SALIDA_FINAL_B2") >= 2)
                view_b._select_session(b2)
                view_b._confirm_stop_selected()
                view_b._last_dialog.emit("response", "confirm")
                view_b._last_dialog.force_close()
                check(pump(lambda: session(b2).state is SessionState.STOPPED, 8), "detener b2 no funcionó")
                check(session(b2).state.label == "Detenido por el usuario", session(b2).state.label)
                check("SALIDA_FINAL_B2" in text(b2), "la salida de b2 no se conservó al detenerla")

                # Fin natural del shell: DONE con su código de salida.
                b3 = view_b.new_terminal()
                check(ready(b3), "la tercera terminal de B no arrancó")
                run(b3, "exit 3")
                check(pump(lambda: session(b3).state is SessionState.FAILED, 5), "exit 3 no se marcó como error")
                check(session(b3).exit_code == 3, f"código de salida: {session(b3).exit_code}")
                check("código 3" in session(b3).detail, f"detalle: {session(b3).detail}")

                # Código 0: finalizado correctamente. La salida se conserva para diagnóstico.
                b4 = view_b.new_terminal()
                check(ready(b4), "la cuarta terminal de B no arrancó")
                run(b4, "echo ANTES_DE_SALIR; exit 0")
                check(pump(lambda: session(b4).state is SessionState.DONE, 5), f"exit 0: {session(b4).state}")
                check("ANTES_DE_SALIR" in text(b4), "se perdió la salida tras terminar")
                check(session(b4).state.label == "Finalizado correctamente", session(b4).state.label)

                # Una señal que no envió el usuario es un error, no «finalizado».
                b5 = view_b.new_terminal()
                check(ready(b5), "la quinta terminal de B no arrancó")
                run(b5, "echo VISIBLE_TRAS_SIGKILL")
                pump(lambda: text(b5).count("VISIBLE_TRAS_SIGKILL") >= 2)
                os.kill(handle(b5).pid, 9)
                check(pump(lambda: session(b5).state is SessionState.FAILED, 5), f"SIGKILL: {session(b5).state}")
                check("SIGKILL" in session(b5).detail, f"detalle: {session(b5).detail}")
                check("VISIBLE_TRAS_SIGKILL" in text(b5), "se perdió la salida tras el error")
                view_b._select_session(b5)
                check(view_b._session_meta.get_label() == session(b5).detail, "la barra no muestra el error")

                # Procesos que intentan sobrevivir al cierre.
                run(a1, "nohup sleep 300 >/dev/null 2>&1 &")
                run(a1, "(trap '' HUP TERM; exec sleep 301) &")
                run(b1, "sleep 302")
                check(pump(lambda: len(handle(a1).managed_pids()) >= 3 and handle(b1).has_foreground_work()),
                      "no arrancaron los procesos de prueba de cierre")
                for sid in (a1, b1):
                    managed.update(handle(sid).managed_pids())

                # 8. Cerrar la ventana con terminales activas pide confirmación.
                win.close()
                pump(timeout=0.3)
                check(win.get_visible(), "la ventana se cerró sin confirmar")
                check(all(proc_alive(p) for p in managed), "cerrar sin confirmar detuvo procesos")
                app._on_quit_response(None, "quit")  # confirmar «Finalizar y salir»
            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())
                app.quit()
            return GLib.SOURCE_REMOVE

        def timeout() -> bool:
            errors.append("tiempo agotado")
            app.quit()
            return GLib.SOURCE_REMOVE

        app.connect("activate", lambda *_: GLib.idle_add(scenario))
        watchdog = GLib.timeout_add_seconds(90, timeout)
        try:
            app.run([])
        finally:
            if "tiempo agotado" not in errors:
                GLib.source_remove(watchdog)
            sys.excepthook = previous_hook
        return errors, managed


if __name__ == "__main__":
    unittest.main()
