"""Transferencia de prompts a agentes, con la aplicación real y CLI falsas (fase 2B.2).

Los ejecutables ``claude``, ``codex`` y ``agy`` de esta prueba son scripts que imitan
el mecanismo oficial de cada CLI para recibir un prompt inicial, sin contactar con
ningún servicio. El ``PATH`` excluye las herramientas reales, de modo que es
imposible consumir una sesión real de ningún agente.
"""

from __future__ import annotations

import os
import re
import sys
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

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

# Prompt de prueba: multilínea, con comillas, barras invertidas, unicode/emoji y una
# línea que empieza por "--" (para comprobar que nunca se confunde con una opción).
# Cada línea es corta a propósito para no depender de cómo VTE reparte las líneas
# largas al leer el texto de la terminal.
TRICKY_PROMPT = (
    "Refactoriza el módulo de pagos:\n"
    "- usa comillas \"dobles\" y 'simples'\n"
    "- cuidado con barras \\invertidas\\\n"
    "-- esto no es una opción, es parte del prompt\n"
    "incluye unicode: café, niño, 日本語, emoji 🚀\n"
    "fin."
)

# Vuelca cada argumento recibido entre marcadores, para comprobar que el prompt llega
# como un único elemento de argv, intacto (sin pasar por ningún intérprete de shell).
ARGS_DUMP = r'''
echo "ARGC=$#"
i=0
for a in "$@"; do
  i=$((i+1))
  echo "===ARG${i}-BEGIN==="
  printf '%s' "$a"
  echo
  echo "===ARG${i}-END==="
done
echo "FAKE-@NAME@-READY"
while IFS= read -r line; do echo "@NAME@> $line"; done
'''


def args_dump(name: str) -> str:
    return ARGS_DUMP.replace("@NAME@", name)


FAKE_CLAUDE = "#!/bin/bash\n" + (
    'if [ "$1" = auth ] && [ "$2" = status ]; then echo \'{"loggedIn": true}\'; exit 0; fi\n'
) + args_dump("CLAUDE")

FAKE_CODEX = "#!/bin/bash\n" + (
    'if [ "$1" = login ] && [ "$2" = status ]; then echo "Logged in using ChatGPT"; exit 0; fi\n'
) + args_dump("CODEX")

FAKE_AGY = "#!/bin/bash\n" + args_dump("AGY")


def pump(until=lambda: False, timeout: float = 5.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def install(bindir: Path, name: str, script: str) -> None:
    path = bindir / name
    path.write_text(script)
    path.chmod(0o755)


def proc_alive(pid: int) -> bool:
    from ia_workspace.terminal.process import read_proc

    info = read_proc(pid)
    return info is not None and info.alive


def parse_args(text: str) -> list[str]:
    """Reconstruye los argumentos recibidos a partir de los marcadores ``ARGS_DUMP``."""
    return [m.group(1) for m in re.finditer(r"===ARG\d+-BEGIN===\n(.*?)\n===ARG\d+-END===", text, re.S)]


def argc(text: str) -> int | None:
    match = re.search(r"ARGC=(\d+)", text)
    return int(match.group(1)) if match else None


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE para GTK 4")
class PromptTransferTest(unittest.TestCase):
    def test_transfer_end_to_end(self) -> None:
        with temp_dir() as data, temp_dir() as root:
            root_path = Path(root)
            dir_a, dir_b, bindir = root_path / "proyecto alfa", root_path / "proyecto-beta", root_path / "bin"
            for d in (dir_a, dir_b, bindir):
                d.mkdir()
            install(bindir, "claude", FAKE_CLAUDE)
            install(bindir, "codex", FAKE_CODEX)
            install(bindir, "agy", FAKE_AGY)
            env = {
                "IAW_DATA_DIR": data,
                "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc",
                "HISTFILE": "",
                "PATH": f"{bindir}:/usr/bin:/bin",  # sin ~/.local/bin: nunca las CLI reales
            }
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                errors, managed = self._run(dir_a, dir_b)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            leftovers = [pid for pid in managed if proc_alive(pid)]
            deadline = time.monotonic() + 3
            while leftovers and time.monotonic() < deadline:
                time.sleep(0.05)
                leftovers = [pid for pid in managed if proc_alive(pid)]
            if leftovers:
                errors.append(f"procesos huérfanos tras salir: {leftovers}")
            if len(managed) < 4:  # Claude, Codex, Antigravity (directo) y Antigravity (manual)
                errors.append(f"se esperaban al menos 4 procesos, hubo {len(managed)}")
            self.assertEqual(errors, [], "\n".join(errors))

    def _run(self, dir_a: Path, dir_b: Path) -> tuple[list[str], set[int]]:
        from ia_workspace.agents import tools as agent_tools
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind
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
                Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
                win, ctx = app._window, app.ctx
                # El selector "Asignar a" solo ofrece los agentes habilitados del proyecto
                # (a diferencia del menú de la terminal, que no depende de ``project.agents``).
                a = ctx.projects.register("Alfa", str(dir_a), SELECTABLE_AGENTS)
                b = ctx.projects.register("Beta", str(dir_b), ())  # sin agentes: a propósito

                def session(sid):
                    return ctx.sessions.get(sid)

                def text(sid) -> str:
                    return ctx.sessions.handle(sid).text()

                def pid(sid) -> int:
                    return ctx.sessions.handle(sid).pid

                win.open_project(a.id)
                view_a = win._workspaces[a.id]
                win.open_project(b.id)
                view_b = win._workspaces[b.id]

                # ---- 1. Borradores independientes por proyecto (persistencia existente) ---
                win.open_project(a.id)
                view_a._prompt.get_buffer().set_text("borrador de A, no tocar")
                win.open_project(b.id)
                check(view_b.state.prompt_draft == "", "el borrador de A se filtró a B")

                # ---- 2. Selección de agente limitada a los habilitados en el proyecto ----
                check(view_b._agent_kinds == [], "B no debería tener agentes (registrado sin ninguno)")
                check(not view_b._transfer.get_sensitive(), "B sin agentes no debería poder transferir")

                # ---- 3. Confirmación previa: abrir el diálogo no debe iniciar nada ----
                win.open_project(a.id)
                view_a._prompt.get_buffer().set_text("primer intento, se cancela")
                view_a._agent_picker.set_selected(0)  # Claude Code
                before = len(ctx.sessions.sessions(a.id))
                view_a.request_transfer()
                dialog = view_a._last_dialog
                check(dialog is not None, "no se abrió el diálogo de revisión")
                check(dialog.method == "direct", "Claude Code debería ofrecer transferencia directa")
                check("Transferir" == dialog.confirm_button.get_label(), "el botón no dice «Transferir»")
                dialog.close()  # cancelar: equivalente a pulsar «Cancelar»
                pump(timeout=0.1)
                check(len(ctx.sessions.sessions(a.id)) == before, "cancelar igualmente creó una sesión")
                check(view_a.state.prompt_draft == "primer intento, se cancela",
                      "cancelar no debería tocar el borrador")

                # ---- 4. Transferencia directa: Claude Code, con el prompt difícil ----
                view_a._prompt.get_buffer().set_text(TRICKY_PROMPT)
                view_a._agent_picker.set_selected(0)  # Claude Code
                view_a.request_transfer()
                dialog = view_a._last_dialog
                check(dialog is not None, "no se abrió el diálogo para Claude Code")
                dialog.confirm_button.emit("clicked")
                c1 = view_a.state.selected_session
                check(c1 is not None, "no se seleccionó la sesión de Claude tras transferir")
                check(pump(lambda: f"FAKE-CLAUDE-READY" in text(c1)), f"Claude no arrancó: {text(c1)[-300:]!r}")
                out = text(c1)
                received = parse_args(out)
                check("--" in received, f"Claude: falta el separador --: {received!r}")
                sep_idx = received.index("--")
                check(received[sep_idx + 1] == TRICKY_PROMPT if len(received) > sep_idx + 1 else False,
                      f"Claude no recibió el prompt intacto: {received[sep_idx + 1] if len(received) > sep_idx + 1 else None!r}")
                check(os.readlink(f"/proc/{pid(c1)}/cwd") == str(dir_a), "Claude no arrancó en la carpeta de A")
                check(view_a.state.prompt_draft == "", "el borrador no se vació tras transferir con éxito")
                check(view_a._prompt.get_buffer().get_text(
                    view_a._prompt.get_buffer().get_start_iter(), view_a._prompt.get_buffer().get_end_iter(), False
                ) == "", "el editor no se vació tras transferir con éxito")

                transfers_a = ctx.prompt_transfers.list(a.id)
                check(len(transfers_a) == 1, f"no se registró la transferencia de A: {transfers_a}")
                check(transfers_a[0].prompt == TRICKY_PROMPT, "el prompt guardado en SQLite no coincide")
                check(transfers_a[0].method == "direct" and transfers_a[0].status == "sent",
                      f"estado inesperado: {transfers_a[0].method}/{transfers_a[0].status}")
                check(transfers_a[0].session_id == c1, "la transferencia no referencia la sesión correcta")
                check(transfers_a[0].agent is AgentKind.CLAUDE_CODE, "agente incorrecto en el registro")

                # La fila de «Tareas» debe reflejar el estado en vivo y permitir volver a la sesión.
                view_a._ctx_toggle.set_active_name("tasks")
                rows = []
                child = view_a._tasks_list.get_first_child()
                while child is not None:
                    rows.append(child)
                    child = child.get_next_sibling()
                check(len(rows) == 1, f"la pestaña Tareas no muestra la transferencia: {len(rows)} filas")
                check(rows[0].get_activatable(), "la fila de tareas no permite volver a la sesión")

                # ---- 5. Transferencia directa: Codex, en el mismo proyecto, concurrente ----
                view_a._prompt.get_buffer().set_text("prompt para codex")
                view_a._agent_picker.set_selected(1)  # Codex
                view_a.request_transfer()
                view_a._last_dialog.confirm_button.emit("clicked")
                x1 = view_a.state.selected_session
                check(x1 is not None and x1 != c1, "Codex no abrió una sesión nueva")
                check(pump(lambda: "FAKE-CODEX-READY" in text(x1)), f"Codex no arrancó: {text(x1)[-300:]!r}")
                out = text(x1)
                check(argc(out) == 3, f"Codex: se esperaban 3 argumentos, ARGC={argc(out)}")
                received = parse_args(out)
                check(received == ["--no-daemon", "--", "prompt para codex"], f"argv de Codex: {received!r}")

                # Claude (c1) sigue vivo y respondiendo mientras Codex (x1) arrancaba.
                check(session(c1).state.is_active, "Claude se detuvo mientras Codex arrancaba")
                ctx.sessions.send_input(c1, "sigue aqui\n")
                check(pump(lambda: "CLAUDE> sigue aqui" in text(c1)), "Claude dejó de responder")

                # ---- 6. Transferencia cruzada: un tercer proyecto transfiere sin mezclarse con A --
                dir_g = dir_b.parent / "gamma"
                dir_g.mkdir()
                gamma = ctx.projects.register("Gamma", str(dir_g), (AgentKind.CODEX,))
                win.open_project(gamma.id)
                view_g = win._workspaces[gamma.id]
                view_g._prompt.get_buffer().set_text("prompt de Gamma, no debe mezclarse con A")
                view_g._agent_picker.set_selected(0)  # el único agente habilitado: Codex
                view_g.request_transfer()
                check(view_g._last_dialog is not None, "Gamma no pudo abrir el diálogo de transferencia")
                view_g._last_dialog.confirm_button.emit("clicked")
                gamma_session = view_g.state.selected_session
                check(pump(lambda: "FAKE-CODEX-READY" in text(gamma_session)), "Codex no arrancó en Gamma")
                check(session(gamma_session).spec.project_id == gamma.id, "la sesión de Gamma quedó mal asociada")
                check(os.readlink(f"/proc/{pid(gamma_session)}/cwd") == str(dir_g),
                      "Gamma no arrancó en su propia carpeta")
                check([t.project_id for t in ctx.prompt_transfers.list(gamma.id)] == [gamma.id],
                      "la transferencia de Gamma no quedó asociada a Gamma")
                check(ctx.prompt_transfers.list(a.id)[0].prompt != "prompt de Gamma, no debe mezclarse con A",
                      "la transferencia de Gamma apareció en la lista de A")
                managed.add(pid(gamma_session))
                ctx.sessions.stop(gamma_session)

                win.open_project(a.id)

                # ---- 7. Transferencia directa: Antigravity, verificando aislamiento de proyecto --
                view_a._prompt.get_buffer().set_text("prompt para agy")
                view_a._agent_picker.set_selected(2)  # Antigravity
                view_a.request_transfer()
                dialog = view_a._last_dialog
                check(dialog.method == "direct", "Antigravity debería ofrecer transferencia directa")
                dialog.confirm_button.emit("clicked")
                g1 = view_a.state.selected_session
                check(pump(lambda: "FAKE-AGY-READY" in text(g1)), f"Antigravity no arrancó: {text(g1)[-300:]!r}")
                out = text(g1)
                check(argc(out) == 2, f"Antigravity: se esperaban 2 argumentos, ARGC={argc(out)}")
                received = parse_args(out)
                check(received == ["--prompt-interactive", "prompt para agy"], f"argv de Antigravity: {received!r}")
                check(session(g1).spec.project_id == a.id, "la sesión de Antigravity quedó en el proyecto erróneo")
                check(ctx.prompt_transfers.list(b.id) == [], "una transferencia de A apareció en B")
                check(len(ctx.prompt_transfers.list(a.id)) == 3, "faltan transferencias registradas en A")

                # ---- 8. Alternativa manual cuando la herramienta no admite transferencia directa --
                bare = agent_tools.AgentTool(
                    kind=AgentKind.ANTIGRAVITY, executables=("agy",), install_hint="agy"
                )
                with mock.patch.dict(agent_tools.TOOLS, {AgentKind.ANTIGRAVITY: bare}):
                    check(not bare.supports_direct_prompt, "el doble de prueba no debería admitir transferencia directa")
                    view_a._prompt.get_buffer().set_text("prompt manual para agy")
                    view_a._agent_picker.set_selected(2)
                    view_a.request_transfer()
                    dialog = view_a._last_dialog
                    check(dialog.method == "manual", "debería ofrecerse la alternativa manual")
                    check(dialog.confirm_button.get_label() == "Copiar y abrir agente",
                          f"etiqueta inesperada: {dialog.confirm_button.get_label()}")
                    dialog.confirm_button.emit("clicked")
                    manual_session = view_a.state.selected_session
                    check(pump(lambda: "FAKE-AGY-READY" in text(manual_session)),
                          f"la sesión manual no arrancó: {text(manual_session)[-300:]!r}")
                    check(argc(text(manual_session)) == 0, "la alternativa manual no debería incrustar el prompt")
                    clip: dict = {}
                    view_a.get_clipboard().read_text_async(
                        None, lambda c, r: clip.setdefault("t", c.read_text_finish(r))
                    )
                    check(pump(lambda: "t" in clip), "no se pudo leer el portapapeles")
                    check(clip.get("t") == "prompt manual para agy", f"portapapeles: {clip.get('t')!r}")
                manual_transfer = ctx.prompt_transfers.list(a.id)[0]
                check(manual_transfer.method == "manual" and manual_transfer.status == "copied",
                      f"registro inesperado: {manual_transfer.method}/{manual_transfer.status}")

                # ---- 9. Error al transferir: el prompt no se pierde ----
                broken_bin = Path(os.environ["PATH"].split(":")[0]).parent / "broken-bin"
                broken_bin.mkdir(exist_ok=True)
                real_path = os.environ["PATH"]
                os.environ["PATH"] = f"{broken_bin}:/usr/bin:/bin"  # sin claude/codex/agy
                try:
                    view_a._prompt.get_buffer().set_text("este prompt no debe perderse")
                    view_a._agent_picker.set_selected(0)
                    view_a.request_transfer()
                    view_a._last_dialog.confirm_button.emit("clicked")
                    pump(timeout=0.2)
                finally:
                    os.environ["PATH"] = real_path
                check(view_a.state.prompt_draft == "este prompt no debe perderse",
                      f"el prompt se perdió tras el error: {view_a.state.prompt_draft!r}")
                check(view_a._prompt.get_buffer().get_text(
                    view_a._prompt.get_buffer().get_start_iter(), view_a._prompt.get_buffer().get_end_iter(), False
                ) == "este prompt no debe perderse", "el editor no conservó el prompt tras el error")
                error_transfers = [t for t in ctx.prompt_transfers.list(a.id) if t.status == "error"]
                check(len(error_transfers) == 1, f"no se registró el error: {error_transfers}")
                check(error_transfers[0].prompt == "este prompt no debe perderse", "el prompt no se guardó con el error")

                # ---- 10. Las sesiones iniciadas por transferencia siguen activas y concurrentes --
                for sid in (c1, x1, g1):
                    check(session(sid).state.is_active, f"la sesión {sid} se detuvo inesperadamente")
                win.navigate("agents")
                win.minimize()
                pump(timeout=0.3)
                win.present()
                win.open_project(a.id)
                pump(timeout=0.2)
                for sid in (c1, x1, g1):
                    check(session(sid).state.is_active, f"minimizar/navegar detuvo la sesión {sid}")

                # ---- 11. Volver a la sesión desde la pestaña «Tareas» ----
                view_a._select_session(g1)  # cambia la pestaña activa a otra sesión
                check(view_a.state.selected_session == g1, "no se pudo cambiar a g1 antes de probar «Tareas»")
                child = view_a._tasks_list.get_first_child()
                target_row = None
                while child is not None:
                    if child.get_title().startswith("Refactoriza"):
                        target_row = child
                        break
                    child = child.get_next_sibling()
                check(target_row is not None, "no se encontró la fila de la transferencia a Claude en Tareas")
                if target_row is not None:
                    target_row.emit("activated")
                    check(view_a.state.selected_session == c1,
                          "la fila de Tareas no regresó a la sesión de Claude")

                # Procesos que deben finalizar con el cierre controlado (comprobado tras salir).
                for sid in (c1, x1, g1, manual_session):
                    live = ctx.sessions.handle(sid)
                    if live is not None and getattr(live, "pid", None):
                        managed.add(live.pid)
                    ctx.sessions.stop(sid)
            except Exception:  # noqa: BLE001
                errors.append(traceback.format_exc())
            app.quit()
            return GLib.SOURCE_REMOVE

        def timeout() -> bool:
            errors.append("tiempo agotado")
            app.quit()
            return GLib.SOURCE_REMOVE

        app.connect("activate", lambda *_: GLib.idle_add(scenario))
        watchdog = GLib.timeout_add_seconds(60, timeout)
        try:
            app.run([])
        finally:
            if "tiempo agotado" not in errors:
                GLib.source_remove(watchdog)
            sys.excepthook = previous_hook
        return errors, managed


if __name__ == "__main__":
    unittest.main()
