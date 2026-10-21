"""Vista ampliada de ChatGPT: la misma vista web del proyecto, en un espacio grande.

La vista web (y su sesión de ChatGPT) no se recrea: se traslada del panel al diálogo y
vuelve al panel al cerrarlo. El selector solo ofrece las conversaciones vinculadas al
proyecto actual.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import ChatLink


class ChatExpandedDialog(Adw.Dialog):
    def __init__(
        self,
        *,
        project_name: str,
        links: list[ChatLink],
        current_link_id: int | None,
        on_select: Callable[[int], None],
        on_reload: Callable[[], None],
        on_external: Callable[[], None],
        on_link_request: Callable[[], None],
    ) -> None:
        super().__init__(title=f"ChatGPT · {project_name}")
        self._links = links
        self._on_select = on_select
        self._syncing = False

        header = Adw.HeaderBar()
        self.selector = Gtk.DropDown(
            model=Gtk.StringList.new([link.title for link in links]),
            tooltip_text="Conversaciones vinculadas a este proyecto",
            enable_search=len(links) > 8,
        )
        self.selector.connect("notify::selected", self._on_selected)
        header.pack_start(self.selector)
        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)
        external = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Abrir en el navegador")
        external.connect("clicked", lambda _b: on_external())
        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar")
        reload.connect("clicked", lambda _b: on_reload())
        header.pack_end(external)
        header.pack_end(reload)
        self.external_button, self.reload_button = external, reload

        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda _b: on_reload())
        self.host = Adw.Bin(vexpand=True, hexpand=True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.banner)
        if links:
            box.append(self.host)
        else:
            self.selector.set_visible(False)
            external.set_visible(False)
            reload.set_visible(False)
            button = Gtk.Button(label="Vincular conversación", halign=Gtk.Align.CENTER)
            button.add_css_class("suggested-action")
            button.add_css_class("pill")
            button.connect("clicked", lambda _b: (self.close(), on_link_request()))
            box.append(Adw.StatusPage(
                icon_name="user-available-symbolic",
                title="Sin conversaciones vinculadas",
                description="Vincula una conversación de ChatGPT a este proyecto para abrirla aquí.",
                child=button,
                vexpand=True,
            ))
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.select_link(current_link_id)

    def select_link(self, link_id: int | None) -> None:
        index = next((i for i, link in enumerate(self._links) if link.id == link_id), None)
        if index is not None:
            self._syncing = True
            self.selector.set_selected(index)
            self._syncing = False

    def _on_selected(self, *_args: object) -> None:
        index = self.selector.get_selected()
        if not self._syncing and 0 <= index < len(self._links):
            self._on_select(self._links[index].id)
