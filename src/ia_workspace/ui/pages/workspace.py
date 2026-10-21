"""C. Workspace del proyecto.

Hay una ``WorkspaceView`` por proyecto abierto. Cada una posee sus widgets, su
``WorkspaceState`` (paneles visibles, pestaña de contexto, borrador del prompt,
tamaños) y su registro de salida de sesiones; nada se comparte entre proyectos.
Ocultar la vista al cambiar de proyecto no detiene las sesiones: estas viven en
el ``SessionManager`` de la aplicación y la vista solo las observa.
"""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ia_workspace.domain.models import AgentKind, Project
from ia_workspace.services.detection import inspect_folder
from ia_workspace.sessions.backend import ViewableHandle
from ia_workspace.sessions.manager import SessionError
from ia_workspace.sessions.models import SessionEvent, SessionInfo, SessionSpec, SessionState
from ia_workspace.ui import widgets as w
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
        link = Gtk.Button(icon_name="insert-link-symbolic", sensitive=False)
        link.set_tooltip_text("Vincular conversación — disponible en una próxima fase")
        link.add_css_class("flat")
        external = Gtk.Button(icon_name="send-to-symbolic", tooltip_text="Abrir ChatGPT en el navegador")
        external.add_css_class("flat")
        external.connect("clicked", lambda _b: self._open_chatgpt())
        pane.append(_pane_header(_pane_title("ChatGPT", w.tool_chip("gpt", "GPT", "ChatGPT")), link, external))

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        bind = Gtk.Button(label="Vincular conversación", sensitive=False)
        bind.add_css_class("suggested-action")
        bind.add_css_class("pill")
        bind.set_tooltip_text("Disponible en una próxima fase")
        buttons.append(bind)
        browser = Gtk.Button(label="Abrir ChatGPT en el navegador")
        browser.add_css_class("flat")
        browser.connect("clicked", lambda _b: self._open_chatgpt())
        buttons.append(browser)
        pane.append(
            _compact_status(
                "user-available-symbolic",
                "Sin conversaciones vinculadas",
                "Pega el enlace de una conversación de ChatGPT para tenerla a mano en este proyecto.",
                buttons,
            )
        )
        note = w.caption("IA Workspace no lee tu historial de ChatGPT. Solo mostrará las conversaciones que vincules.")
        note.set_wrap(True)
        note.set_ellipsize(Pango.EllipsizeMode.NONE)
        note.set_margin_start(14)
        note.set_margin_end(14)
        note.set_margin_bottom(14)
        pane.append(note)
        return pane

    def _open_chatgpt(self) -> None:
        # Se abre en el navegador del usuario: usa su propia sesión y suscripción.
        Gtk.UriLauncher(uri=CHATGPT_URL).launch(self.get_root(), None, None, None)

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
        self._transfer.connect("clicked", lambda _b: self._start_session(with_prompt=True))
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
        if not self.ctx.simulated_agents:
            self._transfer.set_sensitive(False)
            self._transfer.set_tooltip_text(
                "La transferencia a agentes se habilitará con las integraciones reales"
            )
            return
        ready = bool(self.state.prompt_draft.strip()) and self._selected_agent() is not None
        self._transfer.set_sensitive(ready)
        self._transfer.set_tooltip_text("Iniciar una sesión simulada con este prompt")

    def focus_prompt(self) -> None:
        if not self.state.show_prompt:
            self._panel_switches["show_prompt"].set_active(True)
        self._prompt.grab_focus()

    # ---- sesiones -------------------------------------------------------------------
    #
    # Las terminales (y sus procesos) pertenecen al SessionManager de la aplicación.
    # Esta vista solo aloja sus widgets en ``_output_stack``: ocultar el workspace,
    # cambiar de proyecto o de pestaña nunca destruye ni reinicia una terminal.

    def _build_sessions_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, height_request=180)
        self._tabs = Gtk.Box(spacing=4, hexpand=True)
        self._new_session = Gtk.Button(icon_name="list-add-symbolic")
        self._new_session.add_css_class("flat")
        self._new_session.connect("clicked", lambda _b: self.new_terminal())
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
        self._new_session.set_sensitive(available)
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
        return w.session_label(info.state, info.spec.kind)

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
            if session_id in self._pending_close:
                self._remove_session(session_id)
                return  # remove() emite «removed», que vuelve a dibujar
            if session_id not in self._terminal_hosts:
                buffer = self._buffer_for(session_id)
                detail = f" — {event.session.detail}" if event.session.detail else ""
                buffer.insert(buffer.get_end_iter(), f"[{event.session.state.label}{detail}]\n")
        self._render_sessions()

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
        self._ctx_stack.add_named(
            _compact_status("view-list-bullet-symbolic", "Sin tareas", "Transfiere un prompt para crear la primera."),
            "tasks",
        )

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
        return pane

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
