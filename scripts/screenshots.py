"""Genera capturas PNG de las pantallas principales para revisar el diseño.

Uso (sin abrir ventanas en el escritorio):
    scripts/screenshots.sh [claro|oscuro]

Crea carpetas y datos de demostración solo dentro de ``var/`` del repositorio.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEMO = ROOT / "var" / "demo"
OUT = ROOT / "var" / "screenshots"

import gi  # noqa: E402

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402


def pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


def capture(widget: Gtk.Widget, name: str) -> None:
    pump(1.2)
    node = None
    deadline = time.monotonic() + 3
    while node is None and time.monotonic() < deadline:
        widget.queue_draw()
        pump(0.1)
        width, height = widget.get_width(), widget.get_height()
        paintable = Gtk.WidgetPaintable(widget=widget)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
    if node is None:
        print(f"[!] {name}: nada que dibujar")
        return
    texture = widget.get_native().get_renderer().render_texture(node, None)
    path = OUT / f"{name}.png"
    texture.save_to_png(str(path))
    print(f"[ok] {path.relative_to(ROOT)}")


def make_demo_folders() -> list[tuple[str, Path]]:
    shutil.rmtree(DEMO, ignore_errors=True)
    specs = [
        ("FleetControl", "fleet-control", "main", {"package.json": '{"dependencies":{"next":"1","prisma":"1"}}', "AGENTS.md": ""}),
        ("Sistema Pablo", "sistema-pablo", "feature/facturas", {"package.json": '{"dependencies":{"express":"1"}}', "CLAUDE.md": ""}),
        ("Inventario", "inventario", None, {"pyproject.toml": ""}),
    ]
    result = []
    for name, folder, branch, files in specs:
        path = DEMO / folder
        path.mkdir(parents=True)
        if branch:
            (path / ".git").mkdir()
            (path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        for file, content in files.items():
            (path / file).write_text(content)
        result.append((name, path))
    return result


def main() -> None:
    scheme = sys.argv[1] if len(sys.argv) > 1 else "oscuro"
    data = ROOT / "var" / "demo-data"
    shutil.rmtree(data, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    os.environ["IAW_DATA_DIR"] = str(data)
    os.environ["IAW_SIMULATED_AGENTS"] = "1"

    from ia_workspace.app import IAWorkspaceApplication
    from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind

    app = IAWorkspaceApplication()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)

    def scenario() -> bool:
        try:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_LIGHT if scheme == "claro" else Adw.ColorScheme.FORCE_DARK
            )
            # Broadway sin navegador no avanza las animaciones: se desactivan.
            Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
            win, ctx = app._window, app.ctx
            win.set_default_size(1366, 768)
            capture(win, f"{scheme}-00-bienvenida")
            ids = []
            for i, (name, path) in enumerate(make_demo_folders()):
                agents = SELECTABLE_AGENTS if i != 2 else (AgentKind.CLAUDE_CODE,)
                ids.append(ctx.projects.register(name, str(path), agents).id)
            win.open_project(ids[0])
            view = win._workspaces[ids[0]]
            view._prompt.get_buffer().set_text(
                "Agrega un filtro por estado de mantenimiento en la lista de vehículos."
            )
            view._start_session(with_prompt=True)
            win.open_project(ids[1])
            win._workspaces[ids[1]]._prompt.get_buffer().set_text("Revisa la migración de facturas.")
            win._workspaces[ids[1]]._start_session(with_prompt=True)
            pump(2.5)
            for screen in ("home", "projects", "agents", "memory"):
                win.navigate(screen)
                capture(win, f"{scheme}-{('home', 'projects', 'agents', 'memory').index(screen) + 1:02d}-{screen}")
            win.open_project(ids[0])
            capture(win, f"{scheme}-05-workspace")
            win._workspaces[ids[0]]._ctx_toggle.set_active_name("memory")
            win.open_project(ids[2])
            capture(win, f"{scheme}-06-workspace-vacio")
            from ia_workspace.ui.dialogs.add_project import AddProjectDialog

            dialog = AddProjectDialog(ctx, on_added=lambda _i: None)
            dialog.present(win)
            dialog.set_folder(str(DEMO / "fleet-control"))
            capture(win, f"{scheme}-07-agregar-proyecto")
            dialog.force_close()
            from ia_workspace.ui.dialogs.confirm_quit import build_confirm_quit_dialog

            confirm = build_confirm_quit_dialog(ctx, ctx.sessions.active_sessions())
            confirm.present(win)
            capture(win, f"{scheme}-08-confirmar-salida")
            confirm.force_close()
            win.set_default_size(820, 700)
            pump(0.3)
            win.open_project(ids[0])
            capture(win, f"{scheme}-09-estrecho")
        finally:
            app._shutdown_sessions_and_quit()
        return GLib.SOURCE_REMOVE

    app.connect("activate", lambda *_: GLib.idle_add(scenario))
    app.run([])


if __name__ == "__main__":
    main()
