"""B. Proyectos — registro de carpetas locales."""

from __future__ import annotations

from gi.repository import Adw, Gio, Gtk, Pango

from ia_workspace.domain.models import Project
from ia_workspace.services.detection import FolderInfo, inspect_folder
from ia_workspace.services.projects import ProjectValidationError
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp


class ProjectsPage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Proyectos")
        add = Gtk.Button(action_name="win.add-project", tooltip_text="Agregar proyecto (Ctrl+O)")
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Agregar proyecto"))
        add.add_css_class("suggested-action")
        self.header.pack_end(add)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18)
        tools = Gtk.Box(spacing=12)
        self._filter = Gtk.SearchEntry(
            placeholder_text="Filtrar por nombre, carpeta o tecnología", hexpand=True
        )
        self._filter.connect("search-changed", lambda _e: self._render())
        tools.append(self._filter)
        self._order = Adw.ToggleGroup()
        self._order.add(Adw.Toggle(name="activity", label="Actividad"))
        self._order.add(Adw.Toggle(name="name", label="Nombre"))
        self._order.set_active_name(self.ctx.settings.get("projects.order", "activity") or "activity")
        self._order.connect("notify::active-name", self._on_order_changed)
        tools.append(self._order)
        body.append(tools)

        self._list = w.boxed_list()
        body.append(self._list)

        note = w.caption(
            "Registrar un proyecto no copia, mueve ni modifica archivos: IA Workspace solo "
            "guarda la ruta y su estado en su propia base de datos local."
        )
        note.set_wrap(True)
        note.set_ellipsize(Pango.EllipsizeMode.NONE)
        body.append(note)

        self._stack = Gtk.Stack()
        self._stack.add_named(scrolled_clamp(body), "list")
        empty_button = Gtk.Button(label="Agregar proyecto", action_name="win.add-project", halign=Gtk.Align.CENTER)
        empty_button.add_css_class("suggested-action")
        empty_button.add_css_class("pill")
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="folder-symbolic",
                title="Sin proyectos registrados",
                description="Elige una carpeta local para empezar a trabajar con tus agentes.",
                child=empty_button,
            ),
            "empty",
        )
        self.set_content(self._stack)
        self._info: dict[str, FolderInfo] = {}

    def _on_order_changed(self, *_args: object) -> None:
        self.ctx.settings.set("projects.order", self._order.get_active_name())
        self._render()

    # ---- render -------------------------------------------------------------

    def refresh(self) -> None:
        projects = self.ctx.projects.list()
        count = len(projects)
        self.window_title.set_subtitle(f"{count} proyecto{'s' if count != 1 else ''}" if count else "")
        # La inspección de carpetas es de solo lectura y se repite en cada visita.
        self._info = {p.path: inspect_folder(p.path) for p in projects}
        self._render(projects)

    def _render(self, projects: list[Project] | None = None) -> None:
        projects = self.ctx.projects.list() if projects is None else projects
        if not projects:
            self._stack.set_visible_child_name("empty")
            return
        self._stack.set_visible_child_name("list")
        query = self._filter.get_text().strip().lower()
        if query:
            projects = [p for p in projects if query in self._haystack(p)]
        if self._order.get_active_name() == "name":
            projects.sort(key=lambda p: p.name.lower())
        else:
            projects.sort(key=lambda p: p.last_opened_at or p.created_at, reverse=True)
        w.clear_box(self._list)
        for project in projects:
            self._list.append(self._row(project))
        if not projects:
            self._list.append(w.info_row("Ningún proyecto coincide con el filtro.", "system-search-symbolic"))

    def _haystack(self, project: Project) -> str:
        info = self._info.get(project.path, FolderInfo())
        return " ".join((project.name, project.path, info.stack_label, info.branch or "")).lower()

    def _row(self, project: Project) -> Gtk.ListBoxRow:
        info = self._info.get(project.path) or inspect_folder(project.path)
        box = Gtk.Box(spacing=16, margin_top=12, margin_bottom=12, margin_start=14, margin_end=10)
        box.append(w.project_badge(project, "lg"))

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        title_line = Gtk.Box(spacing=8)
        title = Gtk.Label(label=project.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("heading")
        title_line.append(title)
        if project.last_opened_at is None:
            title_line.append(w.status_pill("Nuevo", "working"))
        names.append(title_line)
        names.append(w.caption(w.short_path(project.path), mono=True))
        details = [info.branch and f"⎇ {info.branch}", info.stack_label]
        names.append(w.caption(" · ".join(d for d in details if d) or "Sin Git · stack no detectado"))
        box.append(names)

        agents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        agents.append(w.caption("Agentes"))
        chips = Gtk.Box(spacing=4)
        for kind in project.agents:
            chips.append(w.tool_chip(kind))
        if not project.agents:
            chips.append(w.caption("Ninguno vinculado"))
        agents.append(chips)
        box.append(agents)

        box.append(self._stat("Tareas", "0"))
        box.append(self._stat("Actividad", w.relative_time(project.last_opened_at or project.created_at)))

        open_btn = Gtk.Button(label="Abrir", valign=Gtk.Align.CENTER)
        open_btn.connect("clicked", lambda _b: self.nav.open_project(project.id))
        box.append(open_btn)
        box.append(self._menu(project))
        return Gtk.ListBoxRow(child=box, activatable=False)

    @staticmethod
    def _stat(title: str, value: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        box.set_size_request(76, -1)
        box.append(w.caption(title))
        label = Gtk.Label(label=value, xalign=0)
        label.add_css_class("numeric")
        box.append(label)
        return box

    def _menu(self, project: Project) -> Gtk.MenuButton:
        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("rename", lambda *_: self._rename(project)),
            ("open-folder", lambda *_: self._open_folder(project)),
            ("remove", lambda *_: self._remove(project)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        menu = Gio.Menu()
        menu.append("Renombrar…", "project.rename")
        menu.append("Abrir carpeta", "project.open-folder")
        danger = Gio.Menu()
        danger.append("Quitar del registro…", "project.remove")
        menu.append_section(None, danger)
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER)
        button.add_css_class("flat")
        button.set_tooltip_text("Más opciones")
        button.insert_action_group("project", group)
        return button

    # ---- acciones -----------------------------------------------------------

    def _rename(self, project: Project) -> None:
        entry = Adw.EntryRow(title="Nombre", text=project.name, activates_default=True)
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog = Adw.AlertDialog(heading="Renombrar proyecto", extra_child=group)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("rename", "Renombrar")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "rename":
                self._apply_rename(project, entry)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _apply_rename(self, project: Project, entry: Adw.EntryRow) -> None:
        try:
            self.ctx.projects.rename(project.id, entry.get_text())
        except ProjectValidationError as exc:
            self.nav.toast(str(exc))

    def _open_folder(self, project: Project) -> None:
        launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(project.path))
        launcher.launch(self.get_root(), None, None, None)

    def _remove(self, project: Project) -> None:
        active = self.ctx.sessions.active_sessions(project.id)
        if active:
            self.nav.toast("Detén las sesiones activas de este proyecto antes de quitarlo.")
            return
        dialog = Adw.AlertDialog(
            heading=f"¿Quitar «{project.name}» del registro?",
            body=(
                "Se olvidará su estado en IA Workspace. La carpeta y sus archivos no se "
                "modifican ni se borran."
            ),
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("remove", "Quitar")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "remove":
                self.ctx.projects.unregister(project.id)
                self.nav.toast(f"«{project.name}» se quitó del registro")

        dialog.connect("response", on_response)
        dialog.present(self.get_root())
