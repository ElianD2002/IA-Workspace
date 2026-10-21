"""C. Workspace del proyecto.

Hay una ``WorkspaceView`` por proyecto abierto. Cada una posee sus widgets, su
``WorkspaceState`` (paneles visibles, pestaña de contexto, borrador del prompt,
tamaños) y su registro de salida de sesiones; nada se comparte entre proyectos.
Ocultar la vista al cambiar de proyecto no detiene las sesiones: estas viven en
el ``SessionManager`` de la aplicación y la vista solo las observa.
"""

from __future__ import annotations

import os

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ia_workspace.agents.auth import AuthState
from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind, Project, PromptTransfer
from ia_workspace.services.chat_links import kind_of
from ia_workspace.services.chatgpt_links import parse_chat_link
from ia_workspace.services.detection import inspect_folder
from ia_workspace.services.web_policy import resolve_for_view, test_base_url
from ia_workspace.sessions.backend import ViewableHandle
from ia_workspace.sessions.manager import SessionError
from ia_workspace.sessions.models import SessionEvent, SessionInfo, SessionSpec, SessionState
from ia_workspace.ui import widgets as w
from ia_workspace.ui.chatgpt_browser import WEBKIT_AVAILABLE
from ia_workspace.ui.pages.base import Navigator, Page

CHATGPT_URL = "https://chatgpt.com/"
DEFAULT_CHAT_WIDTH = 280
DEFAULT_CONTEXT_WIDTH = 280
MIN_CENTER_WIDTH = 380
DEFAULT_PROMPT_HEIGHT = 200

PANELS = (
    ("show_chat", "ChatGPT", "user-available-symbolic"),
    ("show_prompt", "Prompt", "document-edit-symbolic"),
    ("show_context", "Tareas y memoria", "view-list-bullet-symbolic"),
)


def _pane_header(*children: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(spacing=6)
    box.add_css_class("pane-header")
    for child in children:
        box.append(child)
    return box


def _pane_title(text: str, prefix: Gtk.Widget | None = None) -> Gtk.Box:
    box = Gtk.Box(spacing=8, hexpand=True)
    if prefix is not None:
        box.append(prefix)
    label = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
    label.add_css_class("pane-title")
    box.append(label)
    return box


def _compact_status(icon: str, title: str, description: str, child: Gtk.Widget | None = None) -> Adw.StatusPage:
    page = Adw.StatusPage(icon_name=icon, title=title, description=description, vexpand=True)
    page.add_css_class("compact")
    if child is not None:
        page.set_child(child)
    return page


class WorkspaceView(Page):
    def __init__(self, nav: Navigator, project: Project) -> None:
        super().__init__(nav, project.name)
        self.project = project
        self.state = self.ctx.workspace.state_for(project.id)
        self._logs: dict[str, Gtk.TextBuffer] = {}
        self._tab_buttons: dict[str, Gtk.ToggleButton] = {}
        self._positions_restored = False

        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Abrir carpeta del proyecto")
        folder.connect("clicked", lambda _b: self._open_folder())
        self.header.pack_start(folder)
        # Con la ventana estrecha el panel ChatGPT se oculta: este botón abre su vista ampliada.
        self._chat_header_button = Gtk.Button(icon_name="user-available-symbolic", visible=False,
                                              tooltip_text="ChatGPT (vista ampliada)")
        self._chat_header_button.connect("clicked", lambda _b: self.open_chat_expanded())
        self.header.pack_start(self._chat_header_button)
        self._browser = None  # vista web de este proyecto (se crea al abrir una conversación)
        self._expanded = None  # diálogo de vista ampliada, si está abierto
        # Simétrico al botón de la barra lateral izquierda: extremo derecho de la cabecera.
        self._context_toggle = Gtk.ToggleButton(
            icon_name="sidebar-show-right-symbolic",
            active=self.state.show_context,
            tooltip_text="Panel de tareas y memoria (Mayús+F9)",
        )
        self._context_toggle.connect("toggled", lambda b: self.set_context_visible(b.get_active()))
        self.header.pack_end(self._context_toggle)
        self.header.pack_end(self._panels_button())

        self._outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=False)
        self._inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._center = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)

        self._chat_pane = self._build_chat_pane()
        self._prompt_pane = self._build_prompt_pane()
        self._sessions_pane = self._build_sessions_pane()
        self._context_pane = self._build_context_pane()

        self._center.set_start_child(self._prompt_pane)
        self._center.set_end_child(self._sessions_pane)
        self._center.set_resize_start_child(False)
        self._center.set_shrink_start_child(False)

        # Ningún panel se encoge por debajo de su mínimo: el centro nunca se recorta.
        self._inner.set_start_child(self._center)
        self._inner.set_end_child(self._context_pane)
        self._inner.set_resize_end_child(False)
        self._inner.set_shrink_start_child(False)
        self._inner.set_shrink_end_child(False)

        self._outer.set_start_child(self._chat_pane)
        self._outer.set_end_child(self._inner)
        self._outer.set_resize_start_child(False)
        self._outer.set_shrink_start_child(False)
        self._outer.set_shrink_end_child(False)

        self._center.set_size_request(MIN_CENTER_WIDTH, -1)

        # Como el prototipo: en ventanas estrechas los paneles laterales se ocultan
        # temporalmente (sin cambiar la preferencia guardada del proyecto).
        self._narrow: set[str] = set()
        bin_ = Adw.BreakpointBin(child=self._outer, width_request=360, height_request=320)
        for key, condition in (("show_chat", "max-width: 940sp"), ("show_context", "max-width: 680sp")):
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            breakpoint.connect("apply", self._on_narrow, key, True)
            breakpoint.connect("unapply", self._on_narrow, key, False)
            bin_.add_breakpoint(breakpoint)
        self.set_content(bin_)
        self.add_tick_callback(self._restore_positions)
        self.update_project(project)
        self._apply_panels()

    # ---- cabecera ---------------------------------------------------------------

    def _panels_button(self) -> Gtk.MenuButton:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        heading = w.caption("Paneles del workspace")
        heading.add_css_class("heading")
        heading.set_margin_start(6)
        box.append(heading)
        self._panel_switches: dict[str, Gtk.Switch] = {}
        for key, label, icon in PANELS:
            row = Gtk.Box(spacing=10, margin_start=6, margin_end=6)
            row.append(Gtk.Image.new_from_icon_name(icon))
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            switch = Gtk.Switch(active=getattr(self.state, key), valign=Gtk.Align.CENTER)
            switch.connect("notify::active", self._on_panel_switch, key)
            self._panel_switches[key] = switch
            row.append(switch)
            box.append(row)
        box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        reset = Gtk.Button(label="Restablecer distribución")
        reset.add_css_class("flat")
        reset.connect("clicked", lambda _b: self._reset_layout())
        box.append(reset)
        popover = Gtk.Popover(child=box)
        self._panels_popover = popover
        return Gtk.MenuButton(
            icon_name="view-dual-symbolic", popover=popover, tooltip_text="Paneles del workspace"
        )

    def set_context_visible(self, visible: bool) -> None:
        """Muestra u oculta el panel derecho (Tareas / Memoria) de este proyecto.

        Solo cambia la visibilidad: la pestaña elegida y el contenido se conservan. La
        preferencia se guarda por proyecto y se recupera al volver o al reiniciar.
        """
        if self.state.show_context != visible:
            self.state.show_context = visible
            self._changed()
        self._apply_panels()

    def toggle_context(self) -> None:
        if "show_context" not in self._narrow:
            self.set_context_visible(not self.state.show_context)

    def _on_panel_switch(self, switch: Gtk.Switch, _pspec: object, key: str) -> None:
        if getattr(self.state, key) != switch.get_active():
            setattr(self.state, key, switch.get_active())
            self._changed()
            self._apply_panels()

    def _on_narrow(self, _bp: Adw.Breakpoint, key: str, narrow: bool) -> None:
        if narrow:
            self._narrow.add(key)
        else:
            self._narrow.discard(key)
        self._apply_panels()

    def _apply_panels(self) -> None:
        self._chat_pane.set_visible(self.state.show_chat and "show_chat" not in self._narrow)
        self._prompt_pane.set_visible(self.state.show_prompt)
        self._context_pane.set_visible(self.state.show_context and "show_context" not in self._narrow)
        for key, switch in self._panel_switches.items():
            switch.set_sensitive(key not in self._narrow)
            switch.set_tooltip_text("Necesita una ventana más ancha" if key in self._narrow else None)
            if switch.get_active() != getattr(self.state, key):
                switch.set_active(getattr(self.state, key))
        if hasattr(self, "_context_toggle"):
            narrow = "show_context" in self._narrow
            if self._context_toggle.get_active() != self.state.show_context:
                self._context_toggle.set_active(self.state.show_context)
            self._context_toggle.set_sensitive(not narrow)
            self._context_toggle.set_tooltip_text(
                "Necesita una ventana más ancha" if narrow else "Panel de tareas y memoria (Mayús+F9)"
            )
        if hasattr(self, "_chat_header_button"):
            self._chat_header_button.set_visible(
                WEBKIT_AVAILABLE and self.state.show_chat and "show_chat" in self._narrow
            )

    def _reset_layout(self) -> None:
        for key, _label, _icon in PANELS:
            setattr(self.state, key, True)
            self._panel_switches[key].set_active(True)
        for key in ("chat_width", "context_width", "prompt_height"):
            self.state.extra.pop(key, None)
        self._changed()
        self._apply_panels()
        self._positions_restored = False
        self.add_tick_callback(self._restore_positions)
        self._panels_popover.popdown()

    # ---- panel ChatGPT --------------------------------------------------------------

    def _build_chat_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=260)
        pane.add_css_class("side-pane")
        link = Gtk.Button(icon_name="insert-link-symbolic", tooltip_text="Vincular conversación")
        link.add_css_class("flat")
        link.connect("clicked", lambda _b: self.request_link_conversation())
        external = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Abrir ChatGPT en el navegador")
        external.add_css_class("flat")
        external.connect("clicked", lambda _b: self._open_chatgpt())
        pane.append(_pane_header(_pane_title("ChatGPT", w.tool_chip("gpt", "GPT", "ChatGPT")), link, external))

        self._chat_stack = Gtk.Stack(vexpand=True)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        bind = Gtk.Button(label="Vincular conversación")
        bind.add_css_class("suggested-action")
        bind.add_css_class("pill")
        bind.connect("clicked", lambda _b: self.request_link_conversation())
        buttons.append(bind)
        browser = Gtk.Button(label="Abrir ChatGPT en el navegador")
        browser.add_css_class("flat")
        browser.connect("clicked", lambda _b: self._open_chatgpt())
        buttons.append(browser)
        self._chat_stack.add_named(
            _compact_status(
                "user-available-symbolic",
                "Sin conversaciones vinculadas",
                "Pega el enlace de una conversación de ChatGPT para tenerla a mano en este proyecto.",
                buttons,
            ),
            "empty",
        )

        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                          margin_top=12, margin_bottom=12)
        heading = w.caption("Conversaciones vinculadas")
        heading.add_css_class("heading")
        listing.append(heading)
        self._chat_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._chat_list.add_css_class("boxed-list")
        listing.append(self._chat_list)
        self._chat_stack.add_named(
            Gtk.ScrolledWindow(child=listing, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True), "list"
        )
        self._chat_stack.add_named(self._build_web_page(), "web")
        pane.append(self._chat_stack)

        note = w.caption("IA Workspace no lee tu historial de ChatGPT. Solo muestra las conversaciones que vinculas.")
        note.set_wrap(True)
        note.set_ellipsize(Pango.EllipsizeMode.NONE)
        note.set_margin_start(14)
        note.set_margin_end(14)
        note.set_margin_bottom(14)
        pane.append(note)

        self.ctx.chat_links.subscribe(self._on_links_changed)
        self._render_links()
        return pane

    def _open_chatgpt(self) -> None:
        # Se abre en el navegador del usuario: usa su propia sesión y suscripción.
        self._open_uri(CHATGPT_URL)

    def _open_uri(self, uri: str) -> None:
        Gtk.UriLauncher(uri=uri).launch(self.get_root(), None, None, None)

    # ---- conversaciones vinculadas ----------------------------------------------------

    def _on_links_changed(self, project_id: int) -> None:
        if project_id == self.project.id:  # nunca se mezclan conversaciones entre proyectos
            self._render_links()

    def _render_links(self) -> None:
        links = self.ctx.chat_links.list(self.project.id)
        browser = self._browser
        if browser is not None and browser.link_id is not None and all(l.id != browser.link_id for l in links):
            self._close_conversation()  # se desvinculó la conversación abierta
        w.clear_box(self._chat_list)
        for link in links:
            self._chat_list.append(self._link_row(link))
        if self._chat_stack.get_visible_child_name() != "web":
            self._chat_stack.set_visible_child_name("list" if links else "empty")

    def _link_row(self, link) -> Gtk.Widget:
        kind = kind_of(link)
        short = parse_chat_link(link.url).short
        when = w.relative_time(link.created_at)
        row = Adw.ActionRow(title=link.title, subtitle=f"{kind.short_label} · {short} · {when}",
                            use_markup=False, activatable=True, tooltip_text=f"Abrir en el navegador: {link.url}")
        row.link_id = link.id  # type: ignore[attr-defined]
        row.connect("activated", lambda _r: self._open_link(link.id))  # clic, Intro o activación
        if self._browser is not None and self._browser.link_id == link.id:
            row.add_suffix(w.status_pill("Abierta", "idle"))
        icon = Gtk.Image.new_from_icon_name("folder-publicshare-symbolic" if kind.value == "shared"
                                            else "user-available-symbolic")
        icon.add_css_class("dim-label")
        icon.set_tooltip_text(kind.label)
        row.add_prefix(icon)

        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("open", lambda *_: self._open_link_external(link.id)),
            ("copy", lambda *_: self._copy_link(link.id)),
            ("rename", lambda *_: self._rename_link(link.id)),
            ("unlink", lambda *_: self._unlink(link.id)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        menu = Gio.Menu()
        menu.append("Abrir en el navegador", "link.open")
        menu.append("Copiar enlace", "link.copy")
        menu.append("Renombrar…", "link.rename")
        danger = Gio.Menu()
        danger.append("Desvincular…", "link.unlink")
        menu.append_section(None, danger)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                              tooltip_text="Más opciones")
        more.add_css_class("flat")
        more.insert_action_group("link", group)
        row.add_suffix(more)
        return row

    def request_link_conversation(self) -> None:
        from ia_workspace.ui.dialogs.chat_link import LinkConversationDialog

        dialog = LinkConversationDialog(
            self.ctx.chat_links, self.project.id, self.project.name,
            on_saved=lambda link: self.nav.toast(f"«{link.title}» vinculada a {self.project.name}"),
            on_error=self.nav.toast,
        )
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def _open_link_external(self, link_id: int) -> None:
        link = self.ctx.chat_links.get(link_id)
        if link is not None and link.project_id == self.project.id:
            # Validado al guardarlo y otra vez aquí: solo se abren enlaces de chatgpt.com.
            self._open_uri(parse_chat_link(link.url).url)

    def _open_link(self, link_id: int) -> None:
        """Abre la conversación dentro de IA Workspace (o en el navegador, sin WebKitGTK)."""
        if WEBKIT_AVAILABLE:
            self.open_conversation(link_id)
        else:
            self._open_link_external(link_id)

    # ---- ChatGPT dentro de IA Workspace (WebKitGTK) ----------------------------------
    #
    # Cada proyecto tiene su propia vista web, que solo abre conversaciones vinculadas a
    # ese proyecto. La sesión de ChatGPT (cookies del sitio oficial) es compartida y vive
    # en el almacenamiento persistente de WebKitGTK; IA Workspace nunca la lee ni la copia.

    def _build_web_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Volver a la lista")
        back.add_css_class("flat")
        back.connect("clicked", lambda _b: self._show_chat_list())
        self._web_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self._web_title.add_css_class("pane-title")
        self._web_spinner = Adw.Spinner(visible=False)
        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar")
        reload.add_css_class("flat")
        reload.connect("clicked", lambda _b: self._web_reload())
        external = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Abrir en el navegador")
        external.add_css_class("flat")
        external.connect("clicked", lambda _b: self._web_external())
        expand = Gtk.Button(icon_name="view-fullscreen-symbolic", tooltip_text="Vista ampliada")
        expand.add_css_class("flat")
        expand.connect("clicked", lambda _b: self.open_chat_expanded())
        page.append(_pane_header(back, self._web_title, self._web_spinner, reload, external, expand))

        self._web_banner = Adw.Banner(revealed=False)
        self._web_banner.connect("button-clicked", lambda _b: self._web_reload())
        page.append(self._web_banner)
        self._web_stack = Gtk.Stack(vexpand=True)
        self._web_host = Adw.Bin(vexpand=True, hexpand=True)
        self._web_stack.add_named(self._web_host, "view")
        self._web_stack.add_named(
            _compact_status("view-fullscreen-symbolic", "Abierta en la vista ampliada",
                            "Cierra la vista ampliada para seguir aquí."),
            "expanded",
        )
        page.append(self._web_stack)
        return page

    def _web_data_dir(self):
        paths = self.ctx.paths
        return paths.data_dir / "webkit" if paths is not None else None

    def _ensure_browser(self):
        if self._browser is None:
            from ia_workspace.ui.chatgpt_browser import ChatBrowser, network_session

            self._browser = ChatBrowser(
                network_session(self._web_data_dir()),
                on_changed=self._update_web_ui,
                on_external=self._open_uri,
            )
            self._place_browser(self._web_host)
        return self._browser

    def _place_browser(self, host: Adw.Bin) -> None:
        """Mueve la vista web (sin recrearla ni recargarla) al contenedor indicado."""
        view = self._browser.view
        parent = view.get_parent()
        if parent is host:
            return
        if isinstance(parent, Adw.Bin):
            parent.set_child(None)
        host.set_child(view)

    def open_conversation(self, link_id: int) -> bool:
        """Abre una conversación vinculada a este proyecto en la vista web integrada."""
        link = self.ctx.chat_links.get(link_id)
        if link is None or link.project_id != self.project.id:
            return False  # nunca se abre una conversación de otro proyecto
        url = resolve_for_view(parse_chat_link(link.url).url, test_base_url(dict(os.environ)))
        self._ensure_browser().open(link.id, url)
        self._web_title.set_label(link.title)
        self._web_title.set_tooltip_text(link.title)
        if self._expanded is None:
            self._chat_stack.set_visible_child_name("web")
        else:
            self._expanded.select_link(link.id)
            self._expanded.set_title(f"ChatGPT · {link.title}")
        self._render_links()
        return True

    def _close_conversation(self) -> None:
        if self._browser is not None:
            self._browser.link_id = self._browser.url = None
            self._browser.view.load_uri("about:blank")
        if self._expanded is not None:
            self._expanded.force_close()
        self._show_chat_list()

    def _show_chat_list(self) -> None:
        has_links = bool(self.ctx.chat_links.list(self.project.id))
        self._chat_stack.set_visible_child_name("list" if has_links else "empty")

    def _current_link(self):
        if self._browser is None or self._browser.link_id is None:
            return None
        return self.ctx.chat_links.get(self._browser.link_id)

    def _web_reload(self) -> None:
        if self._browser is not None:
            self._browser.reload()

    def _web_external(self) -> None:
        link = self._current_link()
        if link is not None:
            self._open_link_external(link.id)

    def _update_web_ui(self) -> None:
        browser = self._browser
        if browser is None:
            return
        message = browser.error or browser.notice
        banners = [self._web_banner] + ([self._expanded.banner] if self._expanded is not None else [])
        for banner in banners:
            banner.set_title(message or "")
            banner.set_button_label("Reintentar" if browser.error else None)
            banner.set_revealed(bool(message))
        self._web_spinner.set_visible(browser.loading)
        if self._expanded is not None:
            self._expanded.spinner.set_visible(browser.loading)

    def open_chat_expanded(self) -> None:
        """Vista ampliada: la misma vista web en un diálogo grande (útil con ventanas pequeñas)."""
        if not WEBKIT_AVAILABLE or self._expanded is not None:
            return
        from ia_workspace.ui.dialogs.chatgpt_expanded import ChatExpandedDialog

        links = self.ctx.chat_links.list(self.project.id)
        current = self._current_link()
        dialog = ChatExpandedDialog(
            project_name=self.project.name,
            links=links,
            current_link_id=current.id if current is not None else None,
            on_select=self.open_conversation,
            on_reload=self._web_reload,
            on_external=self._web_external,
            on_link_request=self.request_link_conversation,
        )
        root = self.get_root()
        if root is not None:
            dialog.set_content_width(max(360, int(root.get_width() * 0.94)))
            dialog.set_content_height(max(360, int(root.get_height() * 0.92)))
        self._expanded = dialog
        dialog.connect("closed", self._on_expanded_closed)
        if links:
            if current is None:
                self.open_conversation(links[0].id)
            self._place_browser(dialog.host)
            self._web_stack.set_visible_child_name("expanded")
            if current is not None:
                dialog.set_title(f"ChatGPT · {current.title}")
        self._update_web_ui()
        dialog.present(root)
        self._last_dialog = dialog

    def _on_expanded_closed(self, _dialog: Adw.Dialog) -> None:
        self._expanded = None
        if self._browser is not None:
            self._place_browser(self._web_host)
        self._web_stack.set_visible_child_name("view")
        if self._current_link() is not None and self._chat_pane.get_visible():
            self._chat_stack.set_visible_child_name("web")
        self._update_web_ui()

    def _copy_link(self, link_id: int) -> None:
        link = self.ctx.chat_links.get(link_id)
        if link is not None:
            self.get_clipboard().set(link.url)
            self.nav.toast("Enlace copiado")

    def _rename_link(self, link_id: int) -> None:
        from ia_workspace.ui.dialogs.chat_link import build_rename_dialog

        link = self.ctx.chat_links.get(link_id)
        if link is not None:
            dialog = build_rename_dialog(self.ctx.chat_links, link, self.nav.toast)
            dialog.present(self.get_root())
            self._last_dialog = dialog

    def _unlink(self, link_id: int) -> None:
        from ia_workspace.ui.dialogs.chat_link import build_unlink_dialog

        link = self.ctx.chat_links.get(link_id)
        if link is not None:
            dialog = build_unlink_dialog(self.ctx.chat_links, link)
            dialog.present(self.get_root())
            self._last_dialog = dialog

    # ---- panel Prompt -----------------------------------------------------------------

    def _build_prompt_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, height_request=150)
        self._prompt_meta = w.caption("")
        library = Gtk.Button(label="Biblioteca", sensitive=False, tooltip_text="Disponible en una próxima fase")
        library.add_css_class("flat")
        pane.append(_pane_header(_pane_title("Prompt", Gtk.Image.new_from_icon_name("document-edit-symbolic")),
                                 self._prompt_meta, library))

        self._prompt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=10, bottom_margin=10,
                                    left_margin=12, right_margin=12)
        self._prompt.add_css_class("prompt-view")
        buffer = self._prompt.get_buffer()
        buffer.set_text(self.state.prompt_draft)
        buffer.connect("changed", self._on_prompt_changed)
        placeholder = Gtk.Label(
            label="Describe la tarea para el agente. Podrás partir de una respuesta de ChatGPT "
            "o de una plantilla de la Biblioteca.",
            xalign=0, yalign=0, wrap=True, can_target=False, margin_top=10, margin_start=14, margin_end=14,
        )
        placeholder.add_css_class("dim-label")
        self._placeholder = placeholder
        overlay = Gtk.Overlay(child=Gtk.ScrolledWindow(child=self._prompt, vexpand=True))
        overlay.add_overlay(placeholder)
        pane.append(overlay)

        bar = Gtk.Box(spacing=8, margin_start=10, margin_end=10, margin_top=6, margin_bottom=8)
        self._context_chips = Gtk.Box(spacing=6, hexpand=True)
        bar.append(self._context_chips)
        self._agent_model = Gtk.StringList()
        self._agent_kinds: list[AgentKind] = []
        self._agent_picker = Gtk.DropDown(model=self._agent_model, tooltip_text="Asignar a")
        self._agent_picker.connect("notify::selected", self._on_agent_selected)
        bar.append(self._agent_picker)
        self._transfer = Gtk.Button()
        self._transfer.set_child(Adw.ButtonContent(icon_name="mail-send-symbolic", label="Transferir…"))
        self._transfer.add_css_class("suggested-action")
        self._transfer.connect("clicked", lambda _b: self.request_transfer())
        bar.append(self._transfer)
        pane.append(bar)
        self._update_prompt_meta()
        return pane

    def _on_prompt_changed(self, buffer: Gtk.TextBuffer) -> None:
        self.state.prompt_draft = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self._changed()
        self._update_prompt_meta()

    def _update_prompt_meta(self) -> None:
        length = len(self.state.prompt_draft)
        self._placeholder.set_visible(length == 0)
        self._prompt_meta.set_label(f"{length} caracteres · borrador guardado" if length else "")
        self._update_transfer_sensitivity()

    def _on_agent_selected(self, *_args: object) -> None:
        index = self._agent_picker.get_selected()
        if 0 <= index < len(self._agent_kinds):
            self.state.extra["agent"] = self._agent_kinds[index].value
            self._changed()

    def _selected_agent(self) -> AgentKind | None:
        index = self._agent_picker.get_selected()
        return self._agent_kinds[index] if 0 <= index < len(self._agent_kinds) else None

    def _update_transfer_sensitivity(self) -> None:
        if not hasattr(self, "_transfer"):
            return
        ready = bool(self.state.prompt_draft.strip()) and self._selected_agent() is not None
        self._transfer.set_sensitive(ready)
        if self.ctx.simulated_agents:
            self._transfer.set_tooltip_text("Iniciar una sesión simulada con este prompt")
        else:
            self._transfer.set_tooltip_text("Revisar y transferir este prompt al agente elegido")

    def focus_prompt(self) -> None:
        if not self.state.show_prompt:
            self._panel_switches["show_prompt"].set_active(True)
        self._prompt.grab_focus()

    # ---- transferencia de prompts a agentes --------------------------------------------
    #
    # Siempre crea una sesión nueva con el prompt como instrucción inicial (mecanismo
    # oficial de cada CLI, ver agents/tools.py): nunca se escribe en la entrada de una
    # sesión ya en marcha, así que no hace falta comprobar si está «lista».

    def request_transfer(self) -> None:
        """Abre el diálogo de revisión antes de transferir el prompt al agente elegido."""
        kind = self._selected_agent()
        prompt = self.state.prompt_draft.strip()
        if kind is None:
            self.nav.toast("Este proyecto no tiene agentes habilitados.")
            return
        if not prompt:
            self.nav.toast("Escribe un prompt antes de transferir.")
            return
        from ia_workspace.ui.dialogs.transfer_prompt import TransferPromptDialog

        direct = self.ctx.agents.status(kind).tool.supports_direct_prompt
        dialog = TransferPromptDialog(
            project_name=self.project.name,
            project_path=self.project.path,
            kind=kind,
            prompt=prompt,
            direct=direct,
            on_confirm=lambda method: self._execute_transfer(kind, prompt, method),
        )
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def _execute_transfer(self, kind: AgentKind, prompt: str, method: str) -> None:
        self._agent_seq[kind] = self._agent_seq.get(kind, 0) + 1
        title = prompt.splitlines()[0][:60] if method == "direct" else f"{kind.label} {self._agent_seq[kind]}"
        spec = SessionSpec(
            project_id=self.project.id,
            kind=kind,
            cwd=self.project.path,
            title=title,
            initial_prompt=prompt if method == "direct" else None,
        )
        try:
            info = self.ctx.sessions.start(spec)
        except SessionError as exc:
            self.ctx.prompt_transfers.record(self.project.id, kind, prompt, None, method, "error", str(exc))
            self.nav.toast(str(exc))
            return

        if method == "direct" and self.ctx.simulated_agents and info.state.is_active:
            # El backend simulado no entiende el prompt incrustado en el argv de la CLI
            # (no lanza ninguna CLI): se lo escribimos a la sesión que acabamos de crear,
            # nunca a una ya existente.
            self.ctx.sessions.send_input(info.id, prompt)

        status = "error" if info.state is SessionState.ERROR else ("sent" if method == "direct" else "copied")
        self.ctx.prompt_transfers.record(
            self.project.id, kind, prompt, info.id, method, status, info.detail if status == "error" else ""
        )
        if method == "manual":
            self.get_clipboard().set(prompt)
            self.nav.toast(f"Prompt copiado. Pégalo en la sesión de {kind.label} en cuanto esté lista.")
        elif status == "error":
            self.nav.toast(f"No se pudo transferir el prompt: {info.detail}")
        if status != "error":
            self.state.prompt_draft = ""
            self._prompt.get_buffer().set_text("")
            self._changed()
        self._select_session(info.id)

    # ---- sesiones -------------------------------------------------------------------
    #
    # Las terminales (y sus procesos) pertenecen al SessionManager de la aplicación.
    # Esta vista solo aloja sus widgets en ``_output_stack``: ocultar el workspace,
    # cambiar de proyecto o de pestaña nunca destruye ni reinicia una terminal.

    def _build_sessions_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, height_request=180)
        self._tabs = Gtk.Box(spacing=4, hexpand=True)
        self._agent_menu = Gio.Menu()
        self._agent_popover = Gtk.PopoverMenu.new_from_model(self._agent_menu)
        self._agent_popover.connect("show", lambda _p: self._check_agents())
        self._new_session = Adw.SplitButton(icon_name="list-add-symbolic", popover=self._agent_popover,
                                            dropdown_tooltip="Iniciar un agente")
        self._new_session.add_css_class("flat")
        self._new_session.connect("clicked", lambda _b: self.new_terminal())
        self._setup_agent_actions()
        tabs_scroll = Gtk.ScrolledWindow(child=self._tabs, vscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True)
        pane.append(_pane_header(tabs_scroll, self._new_session))

        self._session_stack = Gtk.Stack()
        empty_button = Gtk.Button(label="Nueva terminal", halign=Gtk.Align.CENTER)
        empty_button.add_css_class("pill")
        empty_button.connect("clicked", lambda _b: self.new_terminal())
        self._empty_terminal_button = empty_button
        self._session_stack.add_named(
            _compact_status(
                "utilities-terminal-symbolic",
                "Sin sesiones en este proyecto",
                "Las terminales y agentes que inicies aquí trabajarán dentro de la carpeta del "
                "proyecto y seguirán activos aunque cambies de proyecto o minimices la ventana.",
                empty_button,
            ),
            "empty",
        )

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        status_bar = Gtk.Box(spacing=10, margin_start=12, margin_end=10, margin_top=8, margin_bottom=8)
        self._session_pill_holder = Gtk.Box(spacing=6)
        status_bar.append(self._session_pill_holder)
        self._session_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self._session_title.add_css_class("heading")
        status_bar.append(self._session_title)
        self._session_meta = w.caption("", mono=True)
        status_bar.append(self._session_meta)
        self._stop_button = Gtk.Button(label="Detener")
        self._stop_button.add_css_class("destructive-action")
        self._stop_button.connect("clicked", lambda _b: self._confirm_stop_selected())
        status_bar.append(self._stop_button)
        self._discard_button = Gtk.Button(label="Cerrar")
        self._discard_button.connect("clicked", lambda _b: self._close_selected())
        status_bar.append(self._discard_button)
        detail.append(status_bar)
        detail.append(Gtk.Separator())

        # Salida: el registro de texto (sesiones simuladas) o la terminal de cada sesión.
        self._output_stack = Gtk.Stack(vexpand=True)
        self._log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                      wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._log_view.add_css_class("terminal-log")
        self._log_scroll = Gtk.ScrolledWindow(child=self._log_view, vexpand=True)
        self._output_stack.add_named(self._log_scroll, "log")
        detail.append(self._output_stack)
        self._session_stack.add_named(detail, "session")
        pane.append(self._session_stack)

        self._terminal_hosts: dict[str, Gtk.Widget] = {}
        self._pending_close: set[str] = set()
        self._terminal_seq = 0
        available = self._terminals_available()
        empty_button.set_sensitive(available)
        self._new_session.set_tooltip_text(
            "Nueva terminal (Ctrl+Mayús+T)" if available else "Terminales no disponibles: falta gir1.2-vte-3.91"
        )
        return pane

    def _sessions(self) -> list[SessionInfo]:
        return self.ctx.sessions.sessions(self.project.id)

    def _terminals_available(self) -> bool:
        return self.ctx.sessions.backend_for(AgentKind.SHELL) is not None

    def new_terminal(self) -> str | None:
        """Abre una terminal real en la carpeta del proyecto. Devuelve el id de sesión."""
        if not self._terminals_available():
            self.nav.toast("Las terminales no están disponibles en este equipo.")
            return None
        self._terminal_seq += 1
        spec = SessionSpec(project_id=self.project.id, kind=AgentKind.SHELL, cwd=self.project.path,
                           title=f"Terminal {self._terminal_seq}")
        try:
            info = self.ctx.sessions.start(spec)
        except SessionError as exc:
            self.nav.toast(str(exc))
            return None
        self._select_session(info.id)
        return info.id

    # ---- agentes (Claude Code, Codex, Antigravity) -------------------------------------

    def _setup_agent_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        self._agent_actions: dict[str, Gio.SimpleAction] = {}

        def add(name: str, handler) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_: handler())
            group.add_action(action)
            self._agent_actions[name] = action

        add("new-terminal", self.new_terminal)
        add("check-agents", lambda: self._check_agents(force=True))
        for kind in SELECTABLE_AGENTS:
            add(f"start-{kind.value}", lambda k=kind: self.start_agent(k))
            add(f"login-{kind.value}", lambda k=kind: self._launch_agent(k, login=True))
        self.insert_action_group("ws", group)
        self._agent_seq: dict[AgentKind, int] = {}
        self.ctx.agents.subscribe(lambda _kind: self._update_agent_menu())
        self._update_agent_menu()

    def _update_agent_menu(self) -> None:
        menu = self._agent_menu
        menu.remove_all()
        first = Gio.Menu()
        first.append("Nueva terminal", "ws.new-terminal")
        menu.append_section(None, first)
        agents = Gio.Menu()
        logins = Gio.Menu()
        for status in self.ctx.agents.statuses():
            kind = status.kind
            agents.append(f"{kind.label} · {status.summary}", f"ws.start-{kind.value}")
            self._agent_actions[f"start-{kind.value}"].set_enabled(status.installed)
            needs_login = (
                status.installed and status.tool.supports_login and status.auth is AuthState.LOGGED_OUT
            )
            self._agent_actions[f"login-{kind.value}"].set_enabled(needs_login)
            if needs_login:
                logins.append(f"Iniciar sesión en {kind.label}…", f"ws.login-{kind.value}")
        menu.append_section("Agentes", agents)
        if logins.get_n_items():
            menu.append_section(None, logins)
        last = Gio.Menu()
        last.append("Comprobar de nuevo", "ws.check-agents")
        menu.append_section(None, last)

    def _check_agents(self, force: bool = False) -> None:
        """Comprueba instalación y sesión con los comandos oficiales (solo bajo demanda)."""
        self.ctx.agents.detect()
        for status in self.ctx.agents.statuses():
            if status.installed:
                if force:
                    self.ctx.agents.refresh(status.kind)
                else:
                    self.ctx.agents.ensure_checked(status.kind)

    def start_agent(self, kind: AgentKind) -> str | None:
        """Inicia la CLI oficial del agente en la carpeta del proyecto."""
        status = self.ctx.agents.status(kind)
        if not status.installed and not self.ctx.simulated_agents:
            self.nav.toast(f"{kind.label} no está instalado. {status.tool.install_hint}.")
            return None
        if status.auth is AuthState.LOGGED_OUT and status.tool.supports_login:
            dialog = Adw.AlertDialog(
                heading=f"{kind.label} requiere iniciar sesión",
                body=(
                    f"Inicia sesión con tu cuenta mediante el flujo oficial de {kind.label}; se abrirá "
                    "en esta terminal y, si hace falta, en tu navegador. IA Workspace no ve ni "
                    "guarda tus credenciales."
                ),
            )
            dialog.add_response("cancel", "Cancelar")
            dialog.add_response("open", "Abrir de todos modos")
            dialog.add_response("login", "Iniciar sesión")
            dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("login")
            dialog.set_close_response("cancel")

            def on_response(_d: Adw.AlertDialog, response: str) -> None:
                if response in ("open", "login"):
                    self._launch_agent(kind, login=response == "login")

            dialog.connect("response", on_response)
            dialog.present(self.get_root())
            self._last_dialog = dialog
            return None
        return self._launch_agent(kind)

    def _launch_agent(self, kind: AgentKind, *, login: bool = False) -> str | None:
        if login:
            title = f"Inicio de sesión · {kind.label}"
        else:
            self._agent_seq[kind] = self._agent_seq.get(kind, 0) + 1
            title = f"{kind.label} {self._agent_seq[kind]}"
        spec = SessionSpec(project_id=self.project.id, kind=kind, cwd=self.project.path, title=title,
                           mode="login" if login else "interactive")
        try:
            info = self.ctx.sessions.start(spec)
        except SessionError as exc:
            self.nav.toast(str(exc))
            return None
        self._select_session(info.id)
        return info.id

    def _start_session(self, *, with_prompt: bool) -> None:
        kind = self._selected_agent()
        if kind is None:
            self.nav.toast("Este proyecto no tiene agentes habilitados.")
            return
        prompt = self.state.prompt_draft.strip() if with_prompt else ""
        title = prompt.splitlines()[0][:60] if prompt else f"Sesión de {kind.label}"
        try:
            info = self.ctx.sessions.start(SessionSpec(project_id=self.project.id, kind=kind,
                                                       cwd=self.project.path, title=title))
        except SessionError as exc:
            self.nav.toast(str(exc))
            return
        if prompt:
            self._prompt.get_buffer().set_text("")
            if info.state.is_active:
                self.ctx.sessions.send_input(info.id, prompt)
        self._select_session(info.id)

    # ---- detener y cerrar ------------------------------------------------------------

    def _selected_info(self) -> SessionInfo | None:
        sid = self.state.selected_session
        return self.ctx.sessions.get(sid) if sid else None

    def _has_running_work(self, info: SessionInfo) -> bool:
        handle = self.ctx.sessions.handle(info.id)
        check = getattr(handle, "has_foreground_work", None)
        return bool(check()) if callable(check) else info.state.is_active

    def _confirm(self, heading: str, body: str, action: str, on_confirm) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("confirm", action)
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: on_confirm() if r == "confirm" else None)
        dialog.present(self.get_root())
        self._last_dialog = dialog  # accesible para pruebas

    def _confirm_stop_selected(self) -> None:
        info = self._selected_info()
        if info is None or not info.state.is_active:
            return
        name = info.spec.title or info.spec.kind.label
        self._confirm(
            f"¿Detener «{name}»?",
            "Se pedirá a todos sus procesos que terminen. La salida seguirá visible hasta que "
            "cierres la terminal. Las terminales de otros proyectos no se ven afectadas.",
            "Detener",
            lambda: self.ctx.sessions.stop(info.id),
        )

    def _close_selected(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        if not info.state.is_active:
            self._remove_session(info.id)
            return
        name = info.spec.title or info.spec.kind.label
        if self._has_running_work(info) or info.spec.kind is not AgentKind.SHELL:
            self._confirm(
                f"¿Cerrar «{name}»?",
                "Hay procesos en ejecución en esta terminal. Se detendrán y la terminal se cerrará.",
                "Cerrar terminal",
                lambda: self._stop_and_close(info.id),
            )
        else:
            self._stop_and_close(info.id)

    def _stop_and_close(self, session_id: str) -> None:
        self._pending_close.add(session_id)
        self.ctx.sessions.stop(session_id)
        info = self.ctx.sessions.get(session_id)
        if info is not None and info.state.is_terminal:
            self._remove_session(session_id)

    def _remove_session(self, session_id: str) -> None:
        self._pending_close.discard(session_id)
        try:
            self.ctx.sessions.remove(session_id)
        except SessionError as exc:
            self.nav.toast(str(exc))

    # Compatibilidad con la fase 1 (sesiones simuladas).
    def _stop_selected(self) -> None:
        if self.state.selected_session:
            self.ctx.sessions.stop(self.state.selected_session)

    def _discard_selected(self) -> None:
        if self.state.selected_session:
            self._remove_session(self.state.selected_session)

    # ---- render -----------------------------------------------------------------------

    def _select_session(self, session_id: str | None) -> None:
        if self.state.selected_session != session_id:
            self.state.selected_session = session_id
            self._changed()
        self._render_sessions()
        host = self._terminal_hosts.get(session_id) if session_id else None
        if host is not None and self.get_mapped():
            terminal = self._terminal_widget(session_id)
            if terminal is not None:
                terminal.grab_focus()

    def _terminal_widget(self, session_id: str) -> Gtk.Widget | None:
        handle = self.ctx.sessions.handle(session_id)
        return handle.widget() if isinstance(handle, ViewableHandle) else None

    def _ensure_host(self, info: SessionInfo) -> Gtk.Widget | None:
        """Aloja (una sola vez) el widget propio de la sesión dentro de esta vista."""
        if info.id in self._terminal_hosts:
            return self._terminal_hosts[info.id]
        terminal = self._terminal_widget(info.id)
        if terminal is None:
            return None
        host = Gtk.ScrolledWindow(child=terminal, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        host.add_css_class("terminal-host")
        self._output_stack.add_named(host, info.id)
        self._terminal_hosts[info.id] = host
        return host

    def _state_label(self, info: SessionInfo) -> str:
        return w.session_label(info.state, info.id in self._terminal_hosts)

    def _render_sessions(self) -> None:
        sessions = self._sessions()
        ids = {s.id for s in sessions}
        if self.state.selected_session not in ids:
            self.state.selected_session = sessions[-1].id if sessions else None

        w.clear_box(self._tabs)
        self._tab_buttons.clear()
        group: Gtk.ToggleButton | None = None
        for info in sessions:
            self._ensure_host(info)
            content = Gtk.Box(spacing=6)
            content.append(w.tool_chip(info.spec.kind))
            content.append(Gtk.Label(label=info.spec.title or info.spec.kind.label, max_width_chars=22,
                                     ellipsize=Pango.EllipsizeMode.END))
            content.append(w.status_dot(w.STATE_STYLE[info.state], self._state_label(info)))
            button = Gtk.ToggleButton(child=content, active=info.id == self.state.selected_session)
            button.add_css_class("flat")
            button.add_css_class("session-tab")
            if group is None:
                group = button
            else:
                button.set_group(group)
            button.connect("toggled", self._on_tab_toggled, info.id)
            self._tab_buttons[info.id] = button
            self._tabs.append(button)

        selected = self._selected_info()
        if selected is None:
            self._session_stack.set_visible_child_name("empty")
            return
        self._session_stack.set_visible_child_name("session")
        w.clear_box(self._session_pill_holder)
        self._session_pill_holder.append(
            w.status_pill(self._state_label(selected), w.STATE_STYLE[selected.state],
                          spinning=selected.state is SessionState.WORKING)
        )
        is_terminal = selected.id in self._terminal_hosts
        if self.ctx.simulated_agents and not is_terminal:
            sim = Gtk.Label(label="SIMULADA")
            sim.add_css_class("sim-label")
            self._session_pill_holder.append(sim)
        self._session_title.set_label(selected.spec.title or selected.spec.kind.label)
        if is_terminal:
            pid = getattr(self.ctx.sessions.handle(selected.id), "pid", None)
            where = w.short_path(selected.spec.cwd)
            if selected.state.is_terminal and selected.detail:
                self._session_meta.set_label(selected.detail)  # p. ej. «Finalizado con error · código 1»
            else:
                self._session_meta.set_label(f"{where} · PID {pid}" if pid else where)
            self._output_stack.set_visible_child_name(selected.id)
        else:
            started = selected.started_at.astimezone().strftime("%H:%M")
            self._session_meta.set_label(f"{selected.spec.kind.label} · desde {started}")
            self._log_view.set_buffer(self._buffer_for(selected.id))
            self._output_stack.set_visible_child_name("log")
        self._stop_button.set_visible(selected.state.is_active)
        self._stop_button.set_sensitive(selected.state is not SessionState.STOPPING)
        self._discard_button.set_label("Cerrar" if is_terminal else "Descartar")
        self._discard_button.set_visible(is_terminal or not selected.state.is_active)

    def _on_tab_toggled(self, button: Gtk.ToggleButton, session_id: str) -> None:
        if button.get_active() and session_id != self.state.selected_session:
            self._select_session(session_id)

    def _buffer_for(self, session_id: str) -> Gtk.TextBuffer:
        if session_id not in self._logs:
            self._logs[session_id] = Gtk.TextBuffer()
        return self._logs[session_id]

    def on_session_event(self, event: SessionEvent) -> None:
        """Llamado por la ventana para las sesiones de este proyecto, esté visible o no."""
        session_id = event.session.id
        if event.kind == "output":
            buffer = self._buffer_for(session_id)
            buffer.insert(buffer.get_end_iter(), event.text + "\n")
            if session_id == self.state.selected_session:
                GLib.idle_add(self._scroll_log_to_end)
            return
        if event.kind == "removed":
            self._logs.pop(session_id, None)
            self._pending_close.discard(session_id)
            host = self._terminal_hosts.pop(session_id, None)
            if host is not None:
                self._output_stack.remove(host)
        elif event.kind == "state" and event.session.state.is_terminal:
            if event.session.spec.mode == "login" and event.session.spec.kind in SELECTABLE_AGENTS:
                self.ctx.agents.refresh(event.session.spec.kind)
            if session_id in self._pending_close:
                self._remove_session(session_id)
                return  # remove() emite «removed», que vuelve a dibujar
            if session_id not in self._terminal_hosts:
                buffer = self._buffer_for(session_id)
                detail = f" — {event.session.detail}" if event.session.detail else ""
                buffer.insert(buffer.get_end_iter(), f"[{event.session.state.label}{detail}]\n")
        self._render_sessions()
        self._render_tasks()  # una fila de «Tareas» puede mostrar el estado en vivo de su sesión

    def _scroll_log_to_end(self) -> bool:
        adj = self._log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    # ---- panel de contexto ------------------------------------------------------------

    def _build_context_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=260)
        pane.add_css_class("side-pane")
        self._ctx_toggle = Adw.ToggleGroup(hexpand=True)
        self._ctx_toggle.add(Adw.Toggle(name="tasks", label="Tareas"))
        self._ctx_toggle.add(Adw.Toggle(name="memory", label="Memoria"))
        self._ctx_toggle.set_active_name(self.state.context_tab)
        self._ctx_toggle.connect("notify::active-name", self._on_context_tab)
        pane.append(_pane_header(self._ctx_toggle))

        self._ctx_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)

        self._tasks_stack = Gtk.Stack()
        tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12,
                            margin_top=12, margin_bottom=12)
        self._tasks_list = w.boxed_list()
        tasks_box.append(self._tasks_list)
        self._tasks_stack.add_named(
            _compact_status("view-list-bullet-symbolic", "Sin tareas", "Transfiere un prompt para crear la primera."),
            "empty",
        )
        self._tasks_stack.add_named(
            Gtk.ScrolledWindow(child=tasks_box, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True), "list"
        )
        self._ctx_stack.add_named(self._tasks_stack, "tasks")

        memory = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12, margin_end=12,
                         margin_top=12, margin_bottom=12)
        heading = w.caption("Archivos de contexto para agentes")
        heading.add_css_class("heading")
        memory.append(heading)
        self._context_files = w.boxed_list()
        memory.append(self._context_files)
        hint = w.caption(
            "La memoria propia del proyecto (estado actual, próximos pasos y reglas) llegará "
            "con el Cerebro compartido. Será independiente para cada proyecto."
        )
        hint.set_wrap(True)
        hint.set_ellipsize(Pango.EllipsizeMode.NONE)
        memory.append(hint)
        brain = Gtk.Button(label="Abrir Cerebro compartido", halign=Gtk.Align.START)
        brain.add_css_class("flat")
        brain.connect("clicked", lambda _b: self.nav.navigate("memory"))
        memory.append(brain)
        self._ctx_stack.add_named(Gtk.ScrolledWindow(child=memory, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                                     vexpand=True), "memory")
        self._ctx_stack.set_visible_child_name(self.state.context_tab)
        pane.append(self._ctx_stack)
        self.ctx.prompt_transfers.subscribe(self._on_transfers_changed)
        self._render_tasks()
        return pane

    def _on_transfers_changed(self, project_id: int) -> None:
        if project_id == self.project.id:  # nunca se mezclan tareas entre proyectos
            self._render_tasks()

    def _render_tasks(self) -> None:
        transfers = self.ctx.prompt_transfers.list(self.project.id)
        w.clear_box(self._tasks_list)
        for transfer in transfers:
            self._tasks_list.append(self._task_row(transfer))
        self._tasks_stack.set_visible_child_name("list" if transfers else "empty")

    def _task_row(self, transfer: PromptTransfer) -> Gtk.Widget:
        first_line = next((line.strip() for line in transfer.prompt.splitlines() if line.strip()), transfer.prompt)
        method_label = "Transferencia directa" if transfer.method == "direct" else "Copiado para pegar"
        live = self.ctx.sessions.get(transfer.session_id) if transfer.session_id else None
        row = Adw.ActionRow(
            title=first_line,
            subtitle=f"{transfer.agent.label} · {method_label} · {w.relative_time(transfer.created_at)}",
            activatable=live is not None,
        )
        row.add_prefix(w.tool_chip(transfer.agent))
        if live is not None:
            live_terminal = w.is_live_terminal(self.ctx.sessions, live.id)
            row.add_suffix(w.session_pill(live.state, live_terminal))
            row.set_tooltip_text("Ir a esta sesión")
            row.connect("activated", lambda _r, sid=transfer.session_id: self._select_session(sid))
        else:
            styles = {"sent": "idle", "copied": "idle", "error": "error"}
            labels = {"sent": "Enviado", "copied": "Copiado", "error": "Error al transferir"}
            row.add_suffix(w.status_pill(labels[transfer.status], styles[transfer.status]))
            if transfer.status == "error" and transfer.detail:
                row.set_tooltip_text(transfer.detail)
        return row

    def _on_context_tab(self, *_args: object) -> None:
        name = self._ctx_toggle.get_active_name()
        if name in ("tasks", "memory") and name != self.state.context_tab:
            self.state.context_tab = name
            self._changed()
        self._ctx_stack.set_visible_child_name(self.state.context_tab)

    # ---- datos del proyecto -------------------------------------------------------

    def update_project(self, project: Project) -> None:
        self.project = project
        self.window_title.set_title(project.name)
        self._agent_kinds = list(project.agents)
        saved = self.state.extra.get("agent")
        self._agent_model.splice(0, self._agent_model.get_n_items(), [k.label for k in self._agent_kinds])
        index = next((i for i, k in enumerate(self._agent_kinds) if k.value == saved), 0)
        if self._agent_kinds:
            self._agent_picker.set_selected(index)
        self._agent_picker.set_sensitive(bool(self._agent_kinds))
        self._update_transfer_sensitivity()

    def refresh(self) -> None:
        info = inspect_folder(self.project.path)
        branch = info.branch or "sin Git"
        self.window_title.set_subtitle(f"{w.short_path(self.project.path)} · {branch}")

        w.clear_box(self._context_chips)
        for name in info.context_files:
            chip = Gtk.Label(label=name)
            chip.add_css_class("context-chip")
            chip.set_tooltip_text("Archivo de contexto detectado en la carpeta")
            self._context_chips.append(chip)

        w.clear_box(self._context_files)
        for name in info.context_files:
            row = Adw.ActionRow(title=name, subtitle="Detectado en la carpeta del proyecto")
            row.add_prefix(Gtk.Image.new_from_icon_name("text-x-generic-symbolic"))
            self._context_files.append(row)
        if not info.context_files:
            self._context_files.append(w.info_row("No hay AGENTS.md, CLAUDE.md ni GEMINI.md en la carpeta."))
        self._render_sessions()
        self._render_tasks()

    # ---- estado ---------------------------------------------------------------------

    def _changed(self) -> None:
        self.ctx.workspace.mark_dirty(self.project.id)

    def _restore_positions(self, _widget: Gtk.Widget, _clock: object) -> bool:
        width = self._outer.get_width()
        if width <= 0:
            return GLib.SOURCE_CONTINUE
        extra = self.state.extra
        self._outer.set_position(int(extra.get("chat_width", DEFAULT_CHAT_WIDTH)))
        inner_width = width - (self._outer.get_position() if self._chat_pane.get_visible() else 0)
        ctx_width = int(extra.get("context_width", DEFAULT_CONTEXT_WIDTH))
        self._inner.set_position(max(MIN_CENTER_WIDTH, inner_width - ctx_width))
        self._center.set_position(int(extra.get("prompt_height", DEFAULT_PROMPT_HEIGHT)))
        self._positions_restored = True
        return GLib.SOURCE_REMOVE

    def save_state(self) -> None:
        """Copia los tamaños de los paneles al estado (solo si la vista llegó a mostrarse)."""
        if not self._positions_restored:
            return
        extra = self.state.extra
        before = dict(extra)
        if self._chat_pane.get_visible():
            extra["chat_width"] = self._outer.get_position()
        if self._context_pane.get_visible() and self._inner.get_width() > 0:
            extra["context_width"] = self._inner.get_width() - self._inner.get_position()
        if self._prompt_pane.get_visible():
            extra["prompt_height"] = self._center.get_position()
        if extra != before:
            self._changed()

    def _open_folder(self) -> None:
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(self.project.path))
        launcher.launch(self.get_root(), None, None, None)
