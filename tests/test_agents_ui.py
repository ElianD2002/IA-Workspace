"""Agentes en terminales VTE reales, con CLI falsas (fase 2B.1).

Los ejecutables ``claude``, ``codex`` y ``agy`` de esta prueba son scripts que imitan
las interfaces oficiales (sesión interactiva, comandos de estado e inicio de sesión)
sin contactar con ningún servicio. El ``PATH`` excluye las herramientas reales, de
modo que es imposible consumir sesiones de IA.
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
PRIVATE = ("persona@example.invalid", "FAKE-ORG-SECRET-123")

# CLI interactiva falsa: se identifica, muestra su directorio y argumentos, y hace eco.
INTERACTIVE = r'''
echo "FAKE-{name} cwd=$PWD args=[$*]"
while IFS= read -r line; do echo "{name}> $line"; done
'''

FAKE_CLAUDE = "#!/bin/bash\n" + r'''
if [ "$1" = auth ] && [ "$2" = status ]; then
  echo '{"loggedIn": true, "authMethod": "claude.ai", "email": "persona@example.invalid", "orgName": "FAKE-ORG-SECRET-123"}'
  exit 0
fi
if [ "$1" = auth ] && [ "$2" = login ]; then echo FAKE-LOGIN-CLAUDE; exit 0; fi
''' + INTERACTIVE.format(name="CLAUDE")

FAKE_CODEX = "#!/bin/bash\n" + r'''
STATE="$(dirname "$0")/../codex-logged-in"
if [ "$1" = login ] && [ "$2" = status ]; then
  if [ -f "$STATE" ]; then echo "Logged in using ChatGPT"; exit 0; fi
  echo "Not logged in"; exit 1
fi
if [ "$1" = login ]; then echo FAKE-LOGIN-CODEX; touch "$STATE"; sleep 0.3; exit 0; fi
# Igual que la instalación real de este equipo: sin el paquete del daemon, la TUI falla
# salvo que se use --no-daemon.
case " $* " in *" --no-daemon "*) ;; *)
  echo "Error: this CLI has no complete local package; install a packaged Codex CLI or use the standalone installer"
  echo "To work without the background server, rerun the same command with --no-daemon"
  exit 1;;
esac
''' + INTERACTIVE.format(name="CODEX")

FAKE_AGY = "#!/bin/bash\n" + INTERACTIVE.format(name="AGY")


def pump(until=lambda: False, timeout: float = 5.0) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


def proc_alive(pid: int) -> bool:
    from ia_workspace.terminal.process import read_proc

    info = read_proc(pid)
    return info is not None and info.alive


def cmdline(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")


def install(bindir: Path, name: str, script: str) -> None:
    path = bindir / name
    path.write_text(script)
    path.chmod(0o755)


@unittest.skipUnless(HAS_DISPLAY and HAS_VTE, "requiere display y VTE para GTK 4")
class AgentSessionsTest(unittest.TestCase):
    def test_agents_end_to_end(self) -> None:
        with temp_dir() as data, temp_dir() as root:
            root_path = Path(root)
            dir_a, dir_b, bindir = root_path / "proyecto alfa", root_path / "proyecto-beta", root_path / "bin"
            for d in (dir_a, dir_b, bindir):
                d.mkdir()
            install(bindir, "claude", FAKE_CLAUDE)
            install(bindir, "codex", FAKE_CODEX)
            env = {
                "IAW_DATA_DIR": data,
                "IAW_TERMINAL_SHELL": "/bin/bash --noprofile --norc",
                "HISTFILE": "",
                "PATH": f"{bindir}:/usr/bin:/bin",  # sin ~/.local/bin: nunca las CLI reales
            }
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                errors, managed = self._run(dir_a, dir_b, bindir)
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
            if len(managed) < 3:  # Claude, Codex y Antigravity (un proceso cada uno)
                errors.append(f"se esperaban al menos 3 procesos de agentes, hubo {len(managed)}")

            # Ningún dato de la cuenta llega a SQLite ni a otros archivos de datos.
            for path in Path(data).rglob("*"):
                if path.is_file():
                    content = path.read_bytes()
                    for secret in PRIVATE:
                        if secret.encode() in content:
                            errors.append(f"{secret} apareció en {path.name}")
            self.assertEqual(errors, [], "\n".join(errors))

    def _run(self, dir_a: Path, dir_b: Path, bindir: Path) -> tuple[list[str], set[int]]:
        from ia_workspace.agents.auth import AuthState
        from ia_workspace.app import IAWorkspaceApplication
        from ia_workspace.domain.models import AgentKind
        from ia_workspace.sessions.models import SessionSpec, SessionState

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
                catalog = ctx.agents
                a = ctx.projects.register("Alfa", str(dir_a), ())
                b = ctx.projects.register("Beta", str(dir_b), ())

                def session(sid):
                    return ctx.sessions.get(sid)

                def text(sid) -> str:
                    return ctx.sessions.handle(sid).text()

                def pid(sid) -> int:
                    return ctx.sessions.handle(sid).pid

                def settled() -> bool:
                    return not any(s.checking for s in catalog.statuses())

                # 1. Detección: Claude y Codex (falsos) instalados, Antigravity ausente.
                check(catalog.status(AgentKind.CLAUDE_CODE).executable == str(bindir / "claude"),
                      f"claude detectado en {catalog.status(AgentKind.CLAUDE_CODE).executable}")
                check(catalog.status(AgentKind.CODEX).installed, "codex no detectado")
                check(catalog.status(AgentKind.ANTIGRAVITY).auth is AuthState.NOT_INSTALLED,
                      "Antigravity debería figurar como no instalado")

                win.open_project(a.id)
                view_a = win._workspaces[a.id]
                check(not view_a._agent_actions["start-antigravity"].get_enabled(),
                      "el menú permite iniciar Antigravity sin estar instalado")

                # Estado de sesión, bajo demanda, con los comandos oficiales (falsos).
                view_a._check_agents()
                check(pump(settled), "la comprobación de sesión no terminó")
                check(catalog.status(AgentKind.CLAUDE_CODE).auth is AuthState.LOGGED_IN, "Claude: sesión")
                check(catalog.status(AgentKind.CODEX).auth is AuthState.LOGGED_OUT, "Codex: sin sesión")
                check(view_a._agent_actions["login-codex"].get_enabled(), "no se ofrece iniciar sesión en Codex")
                labels = [view_a._agent_menu.get_item_link(i, "section").get_item_attribute_value(j, "label", None)
                          for i in range(view_a._agent_menu.get_n_items())
                          for j in range(view_a._agent_menu.get_item_link(i, "section").get_n_items())]
                labels = [v.get_string() for v in labels if v is not None]
                check("Claude Code · Sesión iniciada" in labels, f"menú: {labels}")
                check("Google Antigravity · No instalado" in labels, f"menú: {labels}")

                # 2. Claude Code en A, en su carpeta real y sin opciones de permisos.
                c1 = view_a.start_agent(AgentKind.CLAUDE_CODE)
                check(c1 is not None, "no se inició Claude Code")
                check(pump(lambda: f"FAKE-CLAUDE cwd={dir_a} args=[]" in text(c1)),
                      f"Claude no arrancó en la carpeta de A: {text(c1)[-200:]!r}")
                check(os.readlink(f"/proc/{pid(c1)}/cwd") == str(dir_a), "cwd real de Claude incorrecto")
                check(not any("dangerously" in a or "approve" in a for a in cmdline(pid(c1))),
                      f"argumentos inseguros: {cmdline(pid(c1))}")
                check(session(c1).spec.title == "Claude Code 1", f"título: {session(c1).spec.title}")
                ctx.sessions.send_input(c1, "hola agente\n")
                check(pump(lambda: "CLAUDE> hola agente" in text(c1)), "Claude no recibe entrada")

                # 3. Codex en B sin sesión: se ofrece el flujo oficial de inicio de sesión.
                win.open_project(b.id)
                view_b = win._workspaces[b.id]
                check(view_b.start_agent(AgentKind.CODEX) is None, "Codex sin sesión arrancó sin avisar")
                dialog = getattr(view_b, "_last_dialog", None)
                check(dialog is not None, "no se mostró el aviso de inicio de sesión")
                if dialog is not None:
                    dialog.emit("response", "login")
                    dialog.force_close()
                login = view_b.state.selected_session
                check(login is not None and session(login).spec.mode == "login", "no se abrió el inicio de sesión")
                check(pump(lambda: session(login).state is SessionState.DONE, 5), "el inicio de sesión no terminó")
                check("FAKE-LOGIN-CODEX" in text(login), "no se ejecutó `codex login`")
                check(pump(lambda: catalog.status(AgentKind.CODEX).auth is AuthState.LOGGED_IN, 5),
                      "el estado de Codex no se actualizó tras iniciar sesión")
                x1 = view_b.start_agent(AgentKind.CODEX)
                check(x1 is not None, "Codex no se inició tras iniciar sesión")
                check(pump(lambda: f"FAKE-CODEX cwd={dir_b} args=[--no-daemon]" in text(x1)),
                      f"Codex no arrancó en B: {text(x1)[-300:]!r}")
                check(session(x1).state.is_active, f"Codex terminó: {session(x1).detail}")

                # 4. Antigravity ausente: no se crea sesión; por la API, error claro.
                before = len(ctx.sessions.sessions())
                check(view_a.start_agent(AgentKind.ANTIGRAVITY) is None, "se inició Antigravity sin instalar")
                check(len(ctx.sessions.sessions()) == before, "se creó una sesión para una herramienta ausente")
                err = ctx.sessions.start(SessionSpec(project_id=a.id, kind=AgentKind.ANTIGRAVITY, cwd=str(dir_a)))
                check(err.state is SessionState.ERROR and "no está instalado" in err.detail, f"error: {err.detail}")
                ctx.sessions.remove(err.id)

                # Se instala «agy» y se vuelve a comprobar: ya se puede usar.
                install(bindir, "agy", FAKE_AGY)
                view_a._check_agents(force=True)
                check(pump(settled), "la nueva comprobación no terminó")
                check(catalog.status(AgentKind.ANTIGRAVITY).installed, "no se detectó agy tras instalarlo")
                check(catalog.status(AgentKind.ANTIGRAVITY).auth is AuthState.UNKNOWN, "agy: estado esperado UNKNOWN")
                win.open_project(a.id)
                g1 = view_a.start_agent(AgentKind.ANTIGRAVITY)
                check(g1 is not None and pump(lambda: f"FAKE-AGY cwd={dir_a}" in text(g1)), "Antigravity no arrancó")

                # 5. Cambiar de proyecto y de sección no recrea ni detiene sesiones.
                pids = {sid: pid(sid) for sid in (c1, x1, g1)}
                widgets = {sid: ctx.sessions.handle(sid).widget() for sid in pids}
                win.open_project(b.id)
                win.navigate("agents")
                win.minimize()
                pump(timeout=0.5)
                win.present()
                win.open_project(a.id)
                pump(timeout=0.3)
                for sid, original in pids.items():
                    check(pid(sid) == original and proc_alive(original), f"{sid} se reinició o se detuvo")
                    check(ctx.sessions.handle(sid).widget() is widgets[sid], f"{sid}: terminal recreada")
                check(os.readlink(f"/proc/{pids[x1]}/cwd") == str(dir_b), "los directorios se mezclaron")
                ctx.sessions.send_input(c1, "sigo aqui\n")
                check(pump(lambda: "CLAUDE> sigo aqui" in text(c1)), "Claude no siguió respondiendo")
                check(len([s for s in ctx.sessions.sessions(a.id) if s.spec.kind is AgentKind.CLAUDE_CODE]) == 1,
                      "volver al proyecto creó otra sesión de Claude")

                # 6. Cerrar una sesión de agente pide confirmación y no afecta a las demás.
                view_a._select_session(g1)
                view_a._close_selected()
                view_a._last_dialog.emit("response", "confirm")
                view_a._last_dialog.force_close()
                check(pump(lambda: session(g1) is None, 8), "no se cerró la sesión de Antigravity")
                check(proc_alive(pids[c1]) and proc_alive(pids[x1]), "cerrar una sesión afectó a otras")

                for sid in (c1, x1):
                    managed.update(ctx.sessions.handle(sid).managed_pids())
                managed.add(pids[g1])

                # Salida completa: confirmación y cierre controlado.
                win.close()
                pump(timeout=0.3)
                check(win.get_visible(), "la ventana se cerró sin confirmar")
                app._on_quit_response(None, "quit")
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
