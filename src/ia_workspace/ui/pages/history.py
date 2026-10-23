"""Pantalla de Historial y revisión de actividad (fase 6C.1).

Lista cronológica unificada de eventos reales de IA Workspace:
- Tareas creadas y actualizaciones de estado.
- Transferencias de prompts a agentes CLI.
- Propuestas del Cerebro Compartido.
- Conversaciones ChatGPT vinculadas al proyecto.

Aislamiento estricto por proyecto, agrupación por fecha, búsqueda en tiempo real,
fechas en America/Tegucigalpa y paginación con carga incremental.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ia_workspace.domain.models import HistoryEvent, HistoryEventKind, Project
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp

TZ_TEGUCIGALPA = ZoneInfo("America/Tegucigalpa")

_KIND_FILTER_OPTIONS: tuple[tuple[str, HistoryEventKind | None], ...] = (
    ("Todos los tipos", None),
    ("Tareas", HistoryEventKind.TASK),
    ("Transferencias de prompts", HistoryEventKind.PROMPT_TRANSFER),
    ("Cerebro y memoria", HistoryEventKind.BRAIN_PROPOSAL),
    ("Conversaciones ChatGPT", HistoryEventKind.CHAT_LINK),
)


def _to_local_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_TEGUCIGALPA)


def _date_group_title(event_date: datetime, now_local: datetime) -> str:
    today = now_local.date()
    ev_d = event_date.date()
    delta = (today - ev_d).days
    if delta == 0:
        return f"Hoy · {event_date.strftime('%d/%m/%Y')}"
    elif delta == 1:
        return f"Ayer · {event_date.strftime('%d/%m/%Y')}"
    return event_date.strftime("%d/%m/%Y")


class HistoryPage(Page):
    """Pantalla de actividad histórica consolidada."""

    TITLE = "Historial y revisión"
    ICON = "document-open-recent-symbolic"

    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, self.TITLE)
        self._selected_project_id: int | None = None
        self._user_chose_all: bool = False
        self._filter_kind: HistoryEventKind | None = None
        self._search_text: str = ""
        self._limit: int = 50

        # Contenedor raíz con scroll y clamp
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=14)

        # Barra superior de filtros
        controls = Gtk.Box(spacing=12)

        # 1. Selector de proyecto
        self._project_dropdown = Gtk.DropDown(hexpand=False)
        self._project_dropdown.set_tooltip_text("Filtrar por proyecto")
        self._project_dropdown.connect("notify::selected", self._on_project_selected)
        controls.append(self._project_dropdown)

        # 2. Selector de tipo de evento
        kind_model = Gtk.StringList.new([opt[0] for opt in _KIND_FILTER_OPTIONS])
        self._kind_dropdown = Gtk.DropDown(model=kind_model, hexpand=False)
        self._kind_dropdown.set_tooltip_text("Filtrar por tipo de actividad")
        self._kind_dropdown.connect("notify::selected", self._on_kind_selected)
        controls.append(self._kind_dropdown)

        # 3. Buscador en vivo
        self._search_entry = Gtk.SearchEntry(placeholder_text="Buscar en historial...", hexpand=True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        controls.append(self._search_entry)

        body.append(controls)

        # Contenedor dinámico de eventos agrupados
        self._events_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.append(self._events_container)

        # Stack para lista vs estados vacíos
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self._stack.add_named(scrolled_clamp(body), "list")

        # Estado vacío: sin proyectos registrados
        add_proj_btn = Gtk.Button(label="Agregar proyecto", action_name="win.add-project", halign=Gtk.Align.CENTER)
        add_proj_btn.add_css_class("suggested-action")
        add_proj_btn.add_css_class("pill")
        self._empty_no_projects = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="Sin proyectos registrados",
            description="Registra un proyecto para comenzar a registrar actividad y tareas.",
            child=add_proj_btn,
        )
        self._stack.add_named(self._empty_no_projects, "empty_no_projects")

        # Estado vacío: sin actividad registrada
        self._empty_no_events = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Sin actividad registrada",
            description="Aún no hay transferencias, tareas ni propuestas registradas para este proyecto.",
        )
        self._stack.add_named(self._empty_no_events, "empty_no_events")

        # Estado vacío: sin coincidencias con los filtros
        reset_filters_btn = Gtk.Button(label="Restablecer filtros", halign=Gtk.Align.CENTER)
        reset_filters_btn.add_css_class("pill")
        reset_filters_btn.connect("clicked", lambda _b: self._clear_filters())
        self._empty_no_match = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Sin coincidencias",
            description="No se encontraron eventos con los filtros o la búsqueda especificada.",
            child=reset_filters_btn,
        )
        self._stack.add_named(self._empty_no_match, "empty_no_match")

        self.set_content(self._stack)

        # Suscripciones reactivas
        self._unsubscribers: list[Callable[[], None]] = []
        if hasattr(self.ctx, "projects") and hasattr(self.ctx.projects, "subscribe"):
            self._unsubscribers.append(self.ctx.projects.subscribe(lambda *_: GLib.idle_add(self._safe_refresh)))
        if hasattr(self.ctx, "tasks") and hasattr(self.ctx.tasks, "subscribe"):
            self._unsubscribers.append(self.ctx.tasks.subscribe(lambda *_: GLib.idle_add(self._safe_render)))
        if hasattr(self.ctx, "history") and hasattr(self.ctx.history, "subscribe"):
            self._unsubscribers.append(self.ctx.history.subscribe(lambda: GLib.idle_add(self._safe_render)))

    def _safe_refresh(self) -> bool:
        try:
            self.refresh()
        except (sqlite3.Error, Exception):
            pass
        return GLib.SOURCE_REMOVE

    def _safe_render(self) -> bool:
        try:
            self._render()
        except (sqlite3.Error, Exception):
            pass
        return GLib.SOURCE_REMOVE

    def _clear_filters(self) -> None:
        self._filter_kind = None
        self._kind_dropdown.set_selected(0)
        self._search_text = ""
        self._search_entry.set_text("")
        self._limit = 50
        self._render()

    def _on_project_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        idx = dropdown.get_selected()
        try:
            projects = self.ctx.projects.list()
        except (sqlite3.Error, Exception):
            return

        if idx >= len(projects):
            # Selección explícita de "Todos los proyectos"
            self._user_chose_all = True
            self._selected_project_id = None
        else:
            self._user_chose_all = False
            self._selected_project_id = projects[idx].id
        self._limit = 50
        self._render()

    def _on_kind_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        idx = dropdown.get_selected()
        if 0 <= idx < len(_KIND_FILTER_OPTIONS):
            self._filter_kind = _KIND_FILTER_OPTIONS[idx][1]
        else:
            self._filter_kind = None
        self._limit = 50
        self._render()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip()
        self._limit = 50
        self._render()

    def refresh(self) -> None:
        """Actualiza el modelo de proyectos y carga el historial del proyecto activo."""
        try:
            projects = self.ctx.projects.list()
        except (sqlite3.Error, Exception):
            projects = []

        if not projects:
            self._selected_project_id = None
            self._stack.set_visible_child_name("empty_no_projects")
            self.window_title.set_subtitle("Sin proyectos")
            return

        # Construir opciones del desplegable: proyectos primero, "Todos los proyectos" al final
        items = [p.name for p in projects] + ["Todos los proyectos"]
        model = Gtk.StringList.new(items)
        self._project_dropdown.set_model(model)

        # Selección predeterminada: si el usuario no eligió explícitamente "Todos los proyectos",
        # seleccionar el proyecto activo, el último abierto, o el primero.
        if not self._user_chose_all:
            target_id = self.ctx.workspace.active_project_id or self.ctx.workspace.last_project_id
            if target_id is not None and any(p.id == target_id for p in projects):
                self._selected_project_id = target_id
            elif self._selected_project_id is None or not any(p.id == self._selected_project_id for p in projects):
                self._selected_project_id = projects[0].id

            # Sincronizar índice en el desplegable
            sel_idx = next((i for i, p in enumerate(projects) if p.id == self._selected_project_id), 0)
            self._project_dropdown.set_selected(sel_idx)
        else:
            self._selected_project_id = None
            self._project_dropdown.set_selected(len(projects))

        self._render()

    def _render(self) -> None:
        """Consulta y renderiza los eventos de historial disponibles."""
        try:
            projects = self.ctx.projects.list()
        except (sqlite3.Error, Exception):
            projects = []

        if not projects:
            self._stack.set_visible_child_name("empty_no_projects")
            self.window_title.set_subtitle("Sin proyectos")
            return

        # Limpiar contenedor dinámico
        while (child := self._events_container.get_first_child()) is not None:
            self._events_container.remove(child)

        # Consultar eventos del servicio
        try:
            events, has_more = self.ctx.history.list_events(
                project_id=self._selected_project_id,
                kind=self._filter_kind,
                search=self._search_text,
                limit=self._limit,
            )
        except (sqlite3.Error, Exception):
            events, has_more = [], False

        # Actualizar subtítulo de cabecera
        proj_name = "Todos los proyectos"
        if self._selected_project_id is not None:
            p_obj = next((p for p in projects if p.id == self._selected_project_id), None)
            proj_name = p_obj.name if p_obj else "Proyecto"
        self.window_title.set_subtitle(f"{proj_name} · {len(events)} actividad{'es' if len(events) != 1 else ''}")

        if not events:
            if self._search_text or self._filter_kind is not None:
                self._stack.set_visible_child_name("empty_no_match")
            else:
                self._stack.set_visible_child_name("empty_no_events")
            return

        self._stack.set_visible_child_name("list")

        # Agrupar cronológicamente por fecha en hora local de Tegucigalpa
        now_local = datetime.now(timezone.utc).astimezone(TZ_TEGUCIGALPA)
        groups: dict[str, list[HistoryEvent]] = {}
        for ev in events:
            ev_local = _to_local_datetime(ev.created_at)
            title = _date_group_title(ev_local, now_local)
            if title not in groups:
                groups[title] = []
            groups[title].append(ev)

        # Construir listas agrupadas
        for group_title, group_events in groups.items():
            header = w.group_header(group_title, f"{len(group_events)} evento{'s' if len(group_events) != 1 else ''}")
            self._events_container.append(header)

            listbox = w.boxed_list()
            for ev in group_events:
                row = self._create_event_row(ev)
                listbox.append(row)
            self._events_container.append(listbox)

        # Botón para cargar más eventos si existen más allá del límite actual
        if has_more:
            load_more_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=16)
            load_more_btn = Gtk.Button(label="Cargar más actividades")
            load_more_btn.add_css_class("pill")
            load_more_btn.connect("clicked", self._on_load_more_clicked)
            load_more_box.append(load_more_btn)
            self._events_container.append(load_more_box)

    def _create_event_row(self, event: HistoryEvent) -> Adw.ActionRow:
        """Crea una fila nativa Adw.ActionRow para un evento histórico."""
        ev_local = _to_local_datetime(event.created_at)

        # Subtítulo: si la vista es global, incluir nombre del proyecto para claridad
        if self._selected_project_id is None:
            subtitle = f"[{event.project_name}] {event.description}"
        else:
            subtitle = event.description

        row = Adw.ActionRow(title=event.title, subtitle=subtitle)

        # Prefijo: icono del tipo de evento + chip de herramienta del agente (si aplica)
        prefix_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(event.kind.icon_name)
        icon.set_pixel_size(16)
        prefix_box.append(icon)

        if event.agent_kind:
            if event.agent_kind == "gpt":
                prefix_box.append(w.tool_chip("gpt", mono="GPT", tooltip="ChatGPT"))
            else:
                prefix_box.append(w.tool_chip(event.agent_kind))
        row.add_prefix(prefix_box)

        # Sufijo: píldora de estado, hora local y botón de navegación
        suffix_box = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)

        if event.status_label:
            suffix_box.append(w.status_pill(event.status_label, event.status_style))

        time_lbl = Gtk.Label(label=ev_local.strftime("%H:%M"), valign=Gtk.Align.CENTER)
        time_lbl.add_css_class("dim-label")
        time_lbl.set_tooltip_text(ev_local.strftime("%d/%m/%Y %H:%M %Z"))
        suffix_box.append(time_lbl)

        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")
        suffix_box.append(arrow)

        row.add_suffix(suffix_box)
        row.set_activatable(True)
        row.connect("activated", lambda _r, ev=event: self._on_event_activated(ev))

        return row

    def _on_load_more_clicked(self, _btn: Gtk.Button) -> None:
        self._limit += 50
        self._render()

    def _on_event_activated(self, event: HistoryEvent) -> None:
        """Navega al recurso u origen del evento correspondiente."""
        if event.target_type == "workspace":
            self.nav.open_project(event.project_id)
        elif event.target_type == "tasks":
            self.nav.navigate("tasks")
        elif event.target_type == "memory":
            self.nav.navigate("memory")
        else:
            self.nav.open_project(event.project_id)
