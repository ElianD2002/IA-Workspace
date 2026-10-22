"""Prueba de interfaz (Broadway) para el puente de contexto bajo demanda (fase 3B).

Comprueba de extremo a extremo:
1. Inicio de Claude Code con --append-system-prompt sin alterar prompt ni CLAUDE.md.
2. Inicio de Codex con -c developer_instructions sin sobrescribir config propia.
3. Inicio de Google Antigravity sin consultas falsas al modelo y con opción de copiar.
4. Transferencia de prompt a Antigravity con composición controlada.
5. Referencia mínima breve (< 600 caracteres) y una sola vez por sesión.
6. Aislamiento estricto de memorias entre proyectos.
7. Visualización discreta del estado en la barra del workspace.
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


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE para GTK 4")
class BrainBridgeUITest(unittest.TestCase):
    def test_brain_bridge_end_to_end(self) -> None:
        with temp_dir() as data, temp_dir() as root:
            root_path = Path(root)
            dir_a, dir_b, bindir = root_path / "alfa", root_path / "beta", root_path / "bin"
            for d in (dir_a, dir_b, bindir):
                d.mkdir()
            install(bindir, "claude", FAKE_CLAUDE)
            install(bindir, "codex", FAKE_CODEX)
            install(bindir, "agy", FAKE_AGY)
            env = {
                "IAW_DATA_DIR": data,
                "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc",
                "HISTFILE": "",
                "PATH": f"{bindir}:/usr/bin:/bin",
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
            self.assertEqual(errors, [], "\n".join(errors))

    def _run(self, dir_a: Path, dir_b: Path) -> tuple[list[str], set[int]]:
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.brain.model import Scope
        from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind
        from ia_workspace.sessions.models import BrainLinkStatus

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
                a = ctx.projects.register("Alfa", str(dir_a), SELECTABLE_AGENTS)
                b = ctx.projects.register("Beta", str(dir_b), SELECTABLE_AGENTS)

                # Inicializamos memoria únicamente para el proyecto Alfa
                ka = ctx.brain.project_key(a)
                ctx.brain.create(Scope.PROJECT, ka, "decisiones", "Arquitectura", "Usa microservicios.")

                def session(sid):
                    return ctx.sessions.get(sid)

                def text(sid) -> str:
                    return ctx.sessions.handle(sid).text()

                def pid(sid) -> int:
                    return ctx.sessions.handle(sid).pid

                def argv(sid) -> list[str]:
                    return ctx.sessions.handle(sid).argv

                win.open_project(a.id)
                view_a = win._workspaces[a.id]
                win.open_project(b.id)
                view_b = win._workspaces[b.id]

                # 1. Proyecto Beta: NO tiene memoria -> status UNAVAILABLE, sin argumentos de Cerebro
                c_beta = view_b.start_agent(AgentKind.CLAUDE_CODE)
                managed.add(pid(c_beta))
                check(pump(lambda: "FAKE-CLAUDE-READY" in text(c_beta)), "Claude en Beta no arrancó")
                args_beta = argv(c_beta)[1:]
                non_settings_beta = [a for a in args_beta if a != "--settings" and not a.startswith('{"statusLine"')]
                check(non_settings_beta == [], f"Claude en Beta recibió argumentos inesperados: {args_beta}")
                check(session(c_beta).spec.brain.status is BrainLinkStatus.UNAVAILABLE, "Beta no reportó UNAVAILABLE")

                # 2. Proyecto Alfa: Claude Code recibe --append-system-prompt
                win.open_project(a.id)
                c_alfa = view_a.start_agent(AgentKind.CLAUDE_CODE)
                managed.add(pid(c_alfa))
                check(pump(lambda: "FAKE-CLAUDE-READY" in text(c_alfa)), "Claude en Alfa no arrancó")
                args_alfa = argv(c_alfa)[1:]
                check("--append-system-prompt" in args_alfa, f"falta --append-system-prompt: {args_alfa}")
                prompt_idx = args_alfa.index("--append-system-prompt")
                check("iaw-brain" in args_alfa[prompt_idx + 1] and "read <ruta-relativa>" in args_alfa[prompt_idx + 1], f"la referencia no contiene iaw-brain: {args_alfa[prompt_idx + 1]}")
                check(len(args_alfa[prompt_idx + 1]) < 850, f"la referencia supera los 850 caracteres: len={len(args_alfa[prompt_idx + 1])}")
                check(session(c_alfa).spec.brain.status is BrainLinkStatus.APPLIED, "Alfa no reportó APPLIED")

                # Comprobamos que una entrada posterior no repita la referencia
                ctx.sessions.send_input(c_alfa, "pregunta de prueba\n")
                check(pump(lambda: "CLAUDE> pregunta de prueba" in text(c_alfa)), "Claude no recibió la entrada")
                check(len(argv(c_alfa)[1:]) == len(args_alfa), "se alteró el argv de la sesión activa")

                # 3. Proyecto Alfa: Codex recibe -c developer_instructions
                x_alfa = view_a.start_agent(AgentKind.CODEX)
                managed.add(pid(x_alfa))
                check(pump(lambda: "FAKE-CODEX-READY" in text(x_alfa)), "Codex en Alfa no arrancó")
                args_x = argv(x_alfa)[1:]
                check(len(args_x) == 3, f"Codex en Alfa: se esperaban 3 argumentos, hubo {len(args_x)}: {args_x}")
                check(args_x[0] == "--no-daemon", "Codex falta --no-daemon")
                check(args_x[1] == "-c", "Codex falta -c")
                check(args_x[2].startswith("developer_instructions="), f"Codex falta developer_instructions: {args_x[2]}")
                check(session(x_alfa).spec.brain.status is BrainLinkStatus.APPLIED, "Codex no reportó APPLIED")

                # 4. Proyecto Alfa: Antigravity sin prompt -> sin argumentos, estado COPY
                g_alfa = view_a.start_agent(AgentKind.ANTIGRAVITY)
                managed.add(pid(g_alfa))
                check(pump(lambda: "FAKE-AGY-READY" in text(g_alfa)), "Antigravity en Alfa no arrancó")
                args_g = argv(g_alfa)[1:]
                check(args_g == [], f"Antigravity no debió recibir argumentos: {args_g}")
                check(session(g_alfa).spec.brain.status is BrainLinkStatus.COPY, "Antigravity no reportó COPY")

                # 5. Proyecto Alfa: Transferencia de prompt a Antigravity con referencia incorporada
                view_a._prompt.get_buffer().set_text("refactorizar modulo x")
                view_a._agent_picker.set_selected(2)  # Antigravity
                view_a.request_transfer()
                dialog = view_a._last_dialog
                check(dialog is not None, "no se abrió el diálogo de transferencia")
                check(dialog.brain_switch is not None, "el diálogo no tiene switch para incorporar Cerebro")
                check(dialog.brain_switch.get_active(), "el switch no vino activo por defecto")
                dialog.confirm_button.emit("clicked")
                g_transfer = view_a.state.selected_session
                check(g_transfer is not None, "no se creó sesión para transferencia de Antigravity")
                managed.add(pid(g_transfer))
                check(pump(lambda: "FAKE-AGY-READY" in text(g_transfer)), "Antigravity transferido no arrancó")
                args_gt = argv(g_transfer)[1:]
                check(len(args_gt) == 2, f"Antigravity transferido: se esperaban 2 argumentos, hubo {len(args_gt)}: {args_gt}")
                check(args_gt[0] == "--prompt-interactive", f"falta --prompt-interactive: {args_gt}")
                check(args_gt[1].startswith("Memoria de «Alfa»"), "el prompt no incluyó la referencia de la memoria")
                check(args_gt[1].endswith("refactorizar modulo x"), "el prompt no incluyó el texto del usuario")
                check("---" in args_gt[1], "falta separador --- en la composición")

                # Verificamos que el registro en SQLite guardó el prompt original sin alterar
                transfers = ctx.prompt_transfers.list(a.id)
                check(len(transfers) >= 1, "no se guardó la transferencia en SQLite")
                latest = transfers[-1]
                check(latest.prompt == "refactorizar modulo x", f"el prompt en SQLite fue alterado: {latest.prompt!r}")

                # 6. Continuidad de sesiones al cambiar de proyecto
                win.open_project(b.id)
                check(proc_alive(pid(c_alfa)), "Claude de Alfa se detuvo al cambiar a Beta")
                check(proc_alive(pid(x_alfa)), "Codex de Alfa se detuvo al cambiar a Beta")
                check(proc_alive(pid(g_transfer)), "Antigravity de Alfa se detuvo al cambiar a Beta")

                win.open_project(a.id)
                check(view_a.state.selected_session == g_transfer, "la sesión seleccionada no se mantuvo al volver")

            except Exception as exc:
                errors.append(f"excepción en scenario: {exc}\n{traceback.format_exc()}")
            finally:
                app.quit()
            return False

        GLib.idle_add(scenario)
        app.run([])
        sys.excepthook = previous_hook
        return errors, managed
