"""Infraestructura común de pantallas."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from gi.repository import Adw, Gtk

if TYPE_CHECKING:
    from ia_workspace.context import AppContext


class Navigator(Protocol):
    """Lo que una pantalla puede pedirle a la ventana principal."""

    ctx: AppContext

    def navigate(self, screen: str) -> None: ...

    def open_project(self, project_id: int) -> None: ...

    def request_add_project(self) -> None: ...

    def sidebar_toggle(self) -> Gtk.Widget: ...

    def toast(self, text: str) -> None: ...


class Page(Adw.Bin):
    """Pantalla con barra de encabezado propia, como en el prototipo."""

    def __init__(self, nav: Navigator, title: str, subtitle: str = "") -> None:
        super().__init__()
        self.nav = nav
        self.ctx = nav.ctx
        self.header = Adw.HeaderBar()
        self.header.pack_start(nav.sidebar_toggle())
        self.window_title = Adw.WindowTitle(title=title, subtitle=subtitle)
        self.header.set_title_widget(self.window_title)
        self.toolbar = Adw.ToolbarView()
        self.toolbar.add_top_bar(self.header)
        self.set_child(self.toolbar)

    def set_content(self, widget: Gtk.Widget) -> None:
        self.toolbar.set_content(widget)

    def refresh(self) -> None:
        """Vuelve a leer los datos. Se llama al mostrarse y cuando cambian."""


def scrolled_clamp(child: Gtk.Widget, maximum: int = 1100) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=maximum, tightening_threshold=maximum - 100, child=child)
    clamp.set_margin_top(10)
    clamp.set_margin_bottom(32)
    clamp.set_margin_start(24)
    clamp.set_margin_end(24)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
