"""Prueba de concepto aislada: ChatGPT en WebKitGTK 6.0, para comprobar el inicio de sesión.

Uso:
    python3 scripts/chatgpt_poc.py            # abre https://chatgpt.com
    python3 scripts/chatgpt_poc.py --clear    # borra los datos web de esta prueba y sale

Usa el mismo componente que IA Workspace (``ia_workspace.ui.chatgpt_browser``), pero con
datos propios en ``var/chatgpt-poc/``: no toca la sesión de la aplicación. Por la
terminal solo se muestra el esquema y el host de cada navegación (nunca la URL completa,
que en un inicio de sesión lleva códigos de un solo uso), para saber en qué paso se
detiene un flujo. No se leen cookies ni contenido, ni se inyecta JavaScript.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DATA_DIR = ROOT / "var" / "chatgpt-poc"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from ia_workspace.services.web_policy import redact  # noqa: E402
from ia_workspace.ui.chatgpt_browser import WEBKIT_AVAILABLE  # noqa: E402

START_URL = "https://chatgpt.com/"


def main(argv: list[str]) -> int:
    if "--clear" in argv:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        print(f"Datos web de la prueba borrados: {DATA_DIR.relative_to(ROOT)}")
        return 0
    if not WEBKIT_AVAILABLE:
        print("WebKitGTK 6.0 no está disponible: instala gir1.2-webkit-6.0", file=sys.stderr)
        return 1

    from ia_workspace.ui.chatgpt_browser import ChatBrowser, network_session, release_sessions

    app = Adw.Application(application_id="io.github.elianmartinez.IAWorkspace.ChatGPTPoC",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(_app: Adw.Application) -> None:
        win = Adw.ApplicationWindow(application=app, title="Prueba de ChatGPT · IA Workspace",
                                    default_width=1100, default_height=800)
        status = Gtk.Label(label="Cargando…")
        status.add_css_class("caption")
        banner = Adw.Banner(revealed=False)
        last_host = {"value": ""}

        def changed() -> None:
            uri = browser.view.get_uri()
            host = redact(uri)
            if host and host != last_host["value"]:
                print("navegación:", host, flush=True)
                last_host["value"] = host
            state = "Cargando…" if browser.loading else "Listo"
            status.set_label(f"{host or '—'} · {state}")
            message = browser.error or browser.notice
            banner.set_title(message or "")
            banner.set_button_label("Reintentar" if browser.error else None)
            banner.set_revealed(bool(message))
            back.set_sensitive(browser.view.can_go_back())
            forward.set_sensitive(browser.view.can_go_forward())

        def external(uri: str) -> None:
            Gtk.UriLauncher(uri=uri).launch(win, None, None, None)

        browser = ChatBrowser(network_session(DATA_DIR), on_changed=lambda: changed(), on_external=external,
                              allow_loopback_http=False)
        banner.connect("button-clicked", lambda _b: browser.reload())

        header = Adw.HeaderBar(title_widget=status)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Atrás", sensitive=False)
        back.connect("clicked", lambda _b: browser.view.go_back())
        forward = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Adelante", sensitive=False)
        forward.connect("clicked", lambda _b: browser.view.go_forward())
        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar")
        reload.connect("clicked", lambda _b: browser.reload())
        outside = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Abrir esta página en el navegador")
        outside.connect("clicked", lambda _b: external(browser.view.get_uri() or START_URL))
        for button in (back, forward, reload):
            header.pack_start(button)
        header.pack_end(outside)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(banner)
        box.append(browser.view)
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        win.set_content(toolbar)

        def closing(_w) -> bool:
            browser.shutdown()
            release_sessions()
            return False

        win.connect("close-request", closing)
        win.present()
        browser.open(0, START_URL)
        print(f"Datos web de esta prueba en {DATA_DIR.relative_to(ROOT)} (borrar: --clear)", flush=True)

    app.connect("activate", activate)
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
