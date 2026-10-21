"""Proceso auxiliar de test_chatgpt_web_ui: usa ChatBrowser, imprime sus procesos de
WebKit y termina. La prueba comprueba después que ninguno sobrevive a este proceso.

Uso: webkit_exit_probe.py <url-local> <carpeta-de-datos>
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from ia_workspace.ui.chatgpt_browser import ChatBrowser, network_session, release_sessions  # noqa: E402


def children() -> list[int]:
    me, found = os.getpid(), []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            if int(stat[stat.rfind(")") + 2:].split()[1]) == me and "WebKit" in stat[: stat.rfind(")")]:
                found.append(int(entry.name))
    return found


def main() -> int:
    url, data_dir = sys.argv[1], Path(sys.argv[2])
    Gtk.init()
    browser = ChatBrowser(network_session(data_dir), on_changed=lambda: None, on_external=lambda _u: None,
                          allow_loopback_http=True)
    window = Gtk.Window(default_width=600, default_height=400)
    window.set_child(browser.view)
    window.present()
    browser.open(1, url)
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not browser.title:
        context.iteration(False)
        time.sleep(0.01)
    print("TITLE", browser.title, flush=True)
    print("PIDS", *children(), flush=True)
    browser.shutdown()
    release_sessions()
    window.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
