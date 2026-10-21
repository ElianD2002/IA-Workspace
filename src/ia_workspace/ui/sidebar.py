"""Barra lateral: secciones principales, proyectos registrados e integraciones."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Adw, Gio, Gtk, Pango

from ia_workspace import APP_NAME
from ia_workspace.context import AppContext
from ia_workspace.domain.models import Project
from ia_workspace.services.integrations import detect_tools, summary
from ia_workspace.sessions.models import SessionState
from ia_workspace.ui import widgets as w


@dataclass(frozen=True)
class NavItem:
    screen: str
    label: str
    icon: str
    accel: str


# Mismo orden y atajos que el prototipo (Alt+1 … Alt+6).
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("home", "Inicio", "go-home-symbolic", "<Alt>1"),
    NavItem("projects", "Proyectos", "folder-symbolic", "<Alt>2"),
    NavItem("agents", "Agentes", "system-run-symbolic", "<Alt>3"),
    NavItem("tasks", "Tareas y prompts", "view-list-bullet-symbolic", "<Alt>4"),
    NavItem("memory", "Cerebro compartido", "user-bookmarks-symbolic", "<Alt>5"),
    NavItem("history", "Historial", "document-open-recent-symbolic", "<Alt>6"),
)

ATTENTION_STATES = (
    SessionState.WAITING_APPROVAL,
    SessionState.FAILED,
    SessionState.ERROR,
    SessionState.DISCONNECTED,
)


def _menu_model() -> Gio.Menu:
    menu = Gio.Menu()
    first = Gio.Menu()
    first.append("Agregar proyecto…", "win.add-project")
    menu.append_section(None, first)
    second = Gio.Menu()
    second.append("Preferencias", "app.preferences")
    second.append("Atajos de teclado", "app.shortcuts")
    second.append(f"Acerca de {APP_NAME}", "app.about")
    menu.append_section(None, second)
    third = Gio.Menu()
    third.append("Salir", "app.quit")
    menu.append_section(None, third)
    return menu


class Sidebar(Adw.Bin):
    def __init__(
        self,
        ctx: AppContext,
        on_navigate: Callable[[str], None],
        on_open_project: Callable[[int], None],
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self._on_navigate = on_navigate
        self._on_open_project = on_open_project
        self._syncing = False
        self._nav_rows: dict[str, Gtk.ListBoxRow] = {}
        self._project_rows: dict[int, Gtk.ListBoxRow] = {}
        self._project_status: dict[int, Gtk.Box] = {}
        self._selected_project: int | None = None
        self._selected_screen = "home"

        header = Adw.HeaderBar(show_title=True)
        header.set_title_widget(Adw.WindowTitle(title=APP_NAME))
        search = Gtk.Button(icon_name="system-search-symbolic", sensitive=False)
        search.set_tooltip_text("Buscar (Ctrl+K) — disponible en una próxima fase")
        header.pack_start(search)
        header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic",
                menu_model=_menu_model(),
                tooltip_text="Menú principal (F10)",
                primary=True,
            )
        )

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._nav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._nav_list.add_css_class("navigation-sidebar")
        for item in NAV_ITEMS:
            row = self._nav_row(item)
            self._nav_rows[item.screen] = row
            self._nav_list.append(row)
        self._nav_list.connect("row-activated", self._on_nav_activated)
        body.append(self._nav_list)

        section = Gtk.Box(spacing=6, margin_top=10, margin_start=18, margin_end=10)
        title = Gtk.Label(label="Proyectos", xalign=0, hexpand=True)
        title.add_css_class("sidebar-section")
        section.append(title)
        add = Gtk.Button(icon_name="list-add-symbolic", action_name="win.add-project")
        add.add_css_class("flat")
        add.add_css_class("circular")
        add.set_tooltip_text("Agregar proyecto (Ctrl+O)")
        section.append(add)
        body.append(section)

        self._project_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._project_list.add_css_class("navigation-sidebar")
        self._project_list.connect("row-activated", self._on_project_activated)
        body.append(self._project_list)

        self._no_projects = w.caption("Aún no hay proyectos registrados.")
        self._no_projects.set_margin_start(18)
        self._no_projects.set_margin_top(4)
        body.append(self._no_projects)

        scroller = Gtk.ScrolledWindow(
            child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True
        )

        toolbar = Adw.ToolbarView(content=scroller)
        toolbar.add_top_bar(header)
        toolbar.add_bottom_bar(self._integrations_row())
        self.set_child(toolbar)
        self.refresh_projects()

    # ---- construcción --------------------------------------------------------

    def _nav_row(self, item: NavItem) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Image.new_from_icon_name(item.icon))
        box.append(Gtk.Label(label=item.label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        if item.screen == "agents":
            self._attention_badge = Gtk.Label(visible=False)
            self._attention_badge.add_css_class("sidebar-badge")
            box.append(self._attention_badge)
        shortcut = item.accel.replace("<Alt>", "Alt+")
        row = Gtk.ListBoxRow(child=box, tooltip_text=f"{item.label} ({shortcut})")
        row.screen = item.screen  # type: ignore[attr-defined]
        return row

    def _integrations_row(self) -> Gtk.Widget:
        statuses = detect_tools()
        button = Gtk.Button(action_name="app.preferences")
        button.add_css_class("flat")
        button.add_css_class("sidebar-footer")
        box = Gtk.Box(spacing=12)
        box.append(Gtk.Image.new_from_icon_name("network-wired-symbolic"))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        labels.append(Gtk.Label(label="Integraciones", xalign=0))
        labels.append(w.caption(summary(statuses)))
        box.append(labels)
        all_ok = all(s.installed for s in statuses)
        box.append(w.status_dot("done" if all_ok else "wait"))
        button.set_child(box)
        button.set_tooltip_text("Herramientas oficiales detectadas en este equipo")
        return button

    def _project_row(self, project: Project) -> Gtk.ListBoxRow:
        box = Gtk.Box(spacing=12)
        box.append(w.project_badge(project))
        box.append(Gtk.Label(label=project.name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        status = Gtk.Box(valign=Gtk.Align.CENTER)
        box.append(status)
        self._project_status[project.id] = status
        row = Gtk.ListBoxRow(child=box, tooltip_text=project.path)
        row.project_id = project.id  # type: ignore[attr-defined]
        return row

    # ---- actualización -------------------------------------------------------

    def refresh_projects(self) -> None:
        w.clear_box(self._project_list)
        self._project_rows.clear()
        self._project_status.clear()
        projects = self.ctx.projects.list()
        for project in projects:
            row = self._project_row(project)
            self._project_rows[project.id] = row
            self._project_list.append(row)
        self._no_projects.set_visible(not projects)
        self.refresh_indicators()
        self._apply_selection()

    def refresh_indicators(self) -> None:
        """Estado agregado de las sesiones de cada proyecto y contador de atención."""
        sessions = self.ctx.sessions.sessions()
        for project_id, holder in self._project_status.items():
            w.clear_box(holder)
            mine = [s for s in sessions if s.project_id == project_id]
            states = {s.state for s in mine}
            if states & set(ATTENTION_STATES):
                failed = states & {SessionState.ERROR, SessionState.FAILED}
                style = "error" if failed else "wait"
                holder.append(w.status_dot(style, "Requiere tu atención"))
            elif SessionState.WORKING in states or SessionState.STARTING in states:
                spinner = Adw.Spinner(tooltip_text="Agente trabajando")
                spinner.set_size_request(14, 14)
                holder.append(spinner)
            elif any(s.state.is_active for s in mine):
                holder.append(w.status_dot("idle", "Sesión abierta"))
        attention = sum(s.state in ATTENTION_STATES for s in sessions)
        self._attention_badge.set_label(str(attention))
        self._attention_badge.set_visible(attention > 0)

    # ---- selección -----------------------------------------------------------

    def select_screen(self, screen: str, project_id: int | None = None) -> None:
        self._selected_project = project_id if screen == "workspace" else None
        self._selected_screen = screen
        self._apply_selection()

    def _apply_selection(self) -> None:
        self._syncing = True
        try:
            if self._selected_screen in self._nav_rows:
                self._nav_list.select_row(self._nav_rows[self._selected_screen])
            else:
                self._nav_list.unselect_all()
            row = self._project_rows.get(self._selected_project) if self._selected_project else None
            if row is not None:
                self._project_list.select_row(row)
            else:
                self._project_list.unselect_all()
        finally:
            self._syncing = False

    def _on_nav_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if not self._syncing:
            self._on_navigate(row.screen)  # type: ignore[attr-defined]

    def _on_project_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if not self._syncing:
            self._on_open_project(row.project_id)  # type: ignore[attr-defined]
