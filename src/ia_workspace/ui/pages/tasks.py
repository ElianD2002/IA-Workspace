"""D. Tareas y prompts vinculados a proyectos y agentes (fase 6B.1) y Biblioteca de prompts (fase 6B.2).

Permite consultar, filtrar y gestionar el ciclo de vida de cada tarea generada
a partir de un prompt transferido, así como administrar la biblioteca de plantillas
de prompts reutilizables con variables {{variable}} y aislamiento por proyecto.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from ia_workspace.domain.models import (
    Project,
    PromptTemplate,
    PromptTemplateScope,
    Task,
    TaskState,
)
from ia_workspace.ui import widgets as w
from ia_workspace.ui.dialogs.template_editor import TemplateEditorDialog
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp


class TasksPage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Tareas y prompts")

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualizar vista")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        self.header.pack_end(refresh_btn)

        # Selector de sección principal: Tareas vs Biblioteca de prompts
        self._active_section = "tasks"
        self._section_toggle = Adw.ToggleGroup()
        self._section_toggle.add(Adw.Toggle(name="tasks", label="Tareas"))
        self._section_toggle.add(Adw.Toggle(name="templates", label="Biblioteca de prompts"))
        self._section_toggle.set_active_name("tasks")
        self._section_toggle.connect("notify::active-name", self._on_main_section_changed)
        self.header.set_title_widget(self._section_toggle)

        # ---------------- SECCIÓN 1: TAREAS ----------------
        self._selected_project_id: int | None = None
        self._filter_state: str = "all"
        self._search_text: str = ""

        # Contenedor principal de tareas con clamp
        tasks_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=14)

        # Barra de controles y filtros de tareas
        controls = Gtk.Box(spacing=12)

        # 1. Selector de proyecto
        self._project_dropdown = Gtk.DropDown(hexpand=False)
        self._project_dropdown.set_tooltip_text("Filtrar por proyecto")
        self._project_dropdown.connect("notify::selected", self._on_project_selected)
        controls.append(self._project_dropdown)

        # 2. Selector de filtro por estado
        self._state_toggle = Adw.ToggleGroup()
        self._state_toggle.add(Adw.Toggle(name="all", label="Todas"))
        self._state_toggle.add(Adw.Toggle(name="in_progress", label="En progreso"))
        self._state_toggle.add(Adw.Toggle(name="pending_review", label="Revisión"))
        self._state_toggle.add(Adw.Toggle(name="completed", label="Completadas"))
        self._state_toggle.add(Adw.Toggle(name="other", label="Otras"))
        self._state_toggle.set_active_name("all")
        self._state_toggle.connect("notify::active-name", self._on_state_toggle_changed)
        controls.append(self._state_toggle)

        # 3. Buscador de tareas
        self._search_entry = Gtk.SearchEntry(
            placeholder_text="Buscar en tareas o prompts…", hexpand=True
        )
        self._search_entry.connect("search-changed", self._on_search_changed)
        controls.append(self._search_entry)

        tasks_body.append(controls)

        # Lista de tareas
        self._tasks_list = w.boxed_list()
        tasks_body.append(self._tasks_list)

        # Stack de tareas para listas y estados vacíos
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self._stack.add_named(scrolled_clamp(tasks_body), "list")

        # Estados vacíos de tareas
        empty_project_btn = Gtk.Button(label="Agregar proyecto", action_name="win.add-project", halign=Gtk.Align.CENTER)
        empty_project_btn.add_css_class("suggested-action")
        empty_project_btn.add_css_class("pill")
        self._empty_no_projects = Adw.StatusPage(
            icon_name="folder-symbolic",
            title="Sin proyectos registrados",
            description="Registra un proyecto para comenzar a transferir prompts y gestionar tareas.",
            child=empty_project_btn,
        )
        self._stack.add_named(self._empty_no_projects, "empty_no_projects")

        self._empty_no_tasks = Adw.StatusPage(
            icon_name="view-list-bullet-symbolic",
            title="Sin tareas registradas",
            description="Transfiere un prompt hacia un agente desde el espacio de trabajo para crear una tarea.",
        )
        self._stack.add_named(self._empty_no_tasks, "empty_no_tasks")

        clear_filter_btn = Gtk.Button(label="Ver todas las tareas", halign=Gtk.Align.CENTER)
        clear_filter_btn.add_css_class("pill")
        clear_filter_btn.connect("clicked", lambda _b: self._clear_filters())
        self._empty_no_match = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Sin coincidencias",
            description="No se encontraron tareas con los filtros o el texto especificado.",
            child=clear_filter_btn,
        )
        self._stack.add_named(self._empty_no_match, "empty_no_match")

        # ---------------- SECCIÓN 2: BIBLIOTECA DE PROMPTS ----------------
        self._tmpl_project_id: int | None = None
        self._tmpl_filter_scope: str = "all"  # "all" | "global" | "project"
        self._tmpl_filter_category: str = "all"
        self._tmpl_search_text: str = ""

        tmpl_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=14)

        # Barra de controles de biblioteca
        tmpl_controls = Gtk.Box(spacing=12)

        # Selector de ámbito / proyecto para plantillas
        self._tmpl_project_dropdown = Gtk.DropDown(hexpand=False)
        self._tmpl_project_dropdown.set_tooltip_text("Filtrar plantillas por proyecto o ámbito")
        self._tmpl_project_dropdown.connect("notify::selected", self._on_tmpl_project_selected)
        tmpl_controls.append(self._tmpl_project_dropdown)

        # Selector de categoría
        self._tmpl_category_dropdown = Gtk.DropDown(hexpand=False)
        self._tmpl_category_dropdown.set_tooltip_text("Filtrar por categoría")
        self._tmpl_category_dropdown.connect("notify::selected", self._on_tmpl_category_selected)
        tmpl_controls.append(self._tmpl_category_dropdown)

        # Buscador de plantillas
        self._tmpl_search_entry = Gtk.SearchEntry(
            placeholder_text="Buscar plantilla por nombre o contenido…", hexpand=True
        )
        self._tmpl_search_entry.connect("search-changed", self._on_tmpl_search_changed)
        tmpl_controls.append(self._tmpl_search_entry)

        # Botón crear plantilla
        new_tmpl_btn = Gtk.Button(label="Nueva plantilla", icon_name="list-add-symbolic")
        new_tmpl_btn.add_css_class("suggested-action")
        new_tmpl_btn.connect("clicked", lambda _b: self._open_new_template_dialog())
        tmpl_controls.append(new_tmpl_btn)

        tmpl_body.append(tmpl_controls)

        # Lista de plantillas
        self._templates_list = w.boxed_list()
        tmpl_body.append(self._templates_list)

        # Stack de biblioteca
        self._tmpl_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self._tmpl_stack.add_named(scrolled_clamp(tmpl_body), "list")

        # Estados vacíos de biblioteca
        create_first_tmpl_btn = Gtk.Button(label="Crear primera plantilla", halign=Gtk.Align.CENTER)
        create_first_tmpl_btn.add_css_class("suggested-action")
        create_first_tmpl_btn.add_css_class("pill")
        create_first_tmpl_btn.connect("clicked", lambda _b: self._open_new_template_dialog())
        self._tmpl_empty_no_templates = Adw.StatusPage(
            icon_name="document-new-symbolic",
            title="Sin plantillas en la biblioteca",
            description="Crea plantillas reutilizables con variables {{variable}} para acelerar tu trabajo con agentes.",
            child=create_first_tmpl_btn,
        )
        self._tmpl_stack.add_named(self._tmpl_empty_no_templates, "empty_no_templates")

        tmpl_clear_btn = Gtk.Button(label="Ver todas las plantillas", halign=Gtk.Align.CENTER)
        tmpl_clear_btn.add_css_class("pill")
        tmpl_clear_btn.connect("clicked", lambda _b: self._clear_tmpl_filters())
        self._tmpl_empty_no_match = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Sin coincidencias",
            description="No se encontraron plantillas con los filtros o el texto especificado.",
            child=tmpl_clear_btn,
        )
        self._tmpl_stack.add_named(self._tmpl_empty_no_match, "empty_no_match")

        # Stack principal de navegación entre Tareas y Biblioteca
        self._main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=100)
        self._main_stack.add_named(self._stack, "tasks")
        self._main_stack.add_named(self._tmpl_stack, "templates")

        self.set_content(self._main_stack)

        # Suscripciones reactivas
        self._unsubscribers: list[Callable[[], None]] = []
        if hasattr(self.ctx, "projects") and hasattr(self.ctx.projects, "subscribe"):
            self._unsubscribers.append(self.ctx.projects.subscribe(lambda *_: GLib.idle_add(self._safe_refresh)))
        if hasattr(self.ctx, "tasks") and hasattr(self.ctx.tasks, "subscribe"):
            self._unsubscribers.append(self.ctx.tasks.subscribe(lambda *_: GLib.idle_add(self._safe_refresh)))
        if hasattr(self.ctx, "sessions") and hasattr(self.ctx.sessions, "subscribe"):
            self._unsubscribers.append(self.ctx.sessions.subscribe(lambda *_: GLib.idle_add(self._safe_render)))
        if hasattr(self.ctx, "prompt_templates") and hasattr(self.ctx.prompt_templates, "subscribe"):
            self._unsubscribers.append(self.ctx.prompt_templates.subscribe(lambda *_: GLib.idle_add(self._safe_render_templates)))

    def _on_main_section_changed(self, group: Adw.ToggleGroup, _pspec: object) -> None:
        active = group.get_active_name() or "tasks"
        self._active_section = active
        self._main_stack.set_visible_child_name(active)
        if active == "tasks":
            self._render()
        else:
            self._render_templates()

    def _safe_refresh(self) -> bool:
        if hasattr(self.ctx, "sessions") and getattr(self.ctx.sessions, "is_shutting_down", False):
            return GLib.SOURCE_REMOVE
        try:
            self.refresh()
        except (sqlite3.Error, Exception):
            pass
        return GLib.SOURCE_REMOVE

    def _safe_render(self) -> bool:
        if hasattr(self.ctx, "sessions") and getattr(self.ctx.sessions, "is_shutting_down", False):
            return GLib.SOURCE_REMOVE
        try:
            self._render()
        except (sqlite3.Error, Exception):
            pass
        return GLib.SOURCE_REMOVE

    def _safe_render_templates(self) -> bool:
        try:
            self._render_templates()
        except (sqlite3.Error, Exception):
            pass
        return GLib.SOURCE_REMOVE

    # ---- MÉTODOS DE TAREAS ----

    def _clear_filters(self) -> None:
        self._filter_state = "all"
        self._state_toggle.set_active_name("all")
        self._search_text = ""
        self._search_entry.set_text("")
        self._render()

    def _on_session_update(self, *_args: object) -> None:
        self._render()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().lower()
        self._render()

    def _on_state_toggle_changed(self, group: Adw.ToggleGroup, _pspec: object) -> None:
        name = group.get_active_name() or "all"
        if name != self._filter_state:
            self._filter_state = name
            self._render()

    def _on_project_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        idx = dropdown.get_selected()
        projects = self.ctx.projects.list()
        if idx == 0 or idx > len(projects):
            self._selected_project_id = None
        else:
            self._selected_project_id = projects[idx - 1].id
        self._render()

    def refresh(self) -> None:
        projects = self.ctx.projects.list()
        items = ["Todos los proyectos"] + [p.name for p in projects]
        model = Gtk.StringList.new(items)
        self._project_dropdown.set_model(model)

        if self._selected_project_id is not None:
            sel_idx = next((i + 1 for i, p in enumerate(projects) if p.id == self._selected_project_id), 0)
            self._project_dropdown.set_selected(sel_idx)
        else:
            self._project_dropdown.set_selected(0)

        self._refresh_template_filters()
        if self._active_section == "tasks":
            self._render()
        else:
            self._render_templates()

    def _render(self) -> None:
        if hasattr(self.ctx, "sessions") and getattr(self.ctx.sessions, "is_shutting_down", False):
            return
        try:
            projects = self.ctx.projects.list()
        except (sqlite3.Error, Exception):
            return

        if not projects:
            self._stack.set_visible_child_name("empty_no_projects")
            self.window_title.set_subtitle("Sin proyectos")
            return

        if not hasattr(self.ctx, "tasks") or self.ctx.tasks is None:
            self._stack.set_visible_child_name("empty_no_tasks")
            return

        try:
            all_tasks = self.ctx.tasks.list_all()
        except (sqlite3.Error, Exception):
            return
        if not all_tasks:
            self._stack.set_visible_child_name("empty_no_tasks")
            self.window_title.set_subtitle("0 tareas")
            return

        # Filtrar por proyecto
        tasks = all_tasks
        if self._selected_project_id is not None:
            tasks = [t for t in tasks if t.project_id == self._selected_project_id]

        # Filtrar por estado
        if self._filter_state == "in_progress":
            tasks = [t for t in tasks if t.status == TaskState.IN_PROGRESS]
        elif self._filter_state == "pending_review":
            tasks = [t for t in tasks if t.status == TaskState.PENDING_REVIEW]
        elif self._filter_state == "completed":
            tasks = [t for t in tasks if t.status == TaskState.COMPLETED]
        elif self._filter_state == "other":
            tasks = [t for t in tasks if t.status in (TaskState.PENDING, TaskState.FAILED, TaskState.CANCELED)]

        # Filtrar por búsqueda
        if self._search_text:
            s = self._search_text
            tasks = [
                t for t in tasks
                if s in t.title.lower() or s in t.prompt.lower() or s in t.result_notes.lower()
            ]

        # Actualizar subtítulo con métricas
        total_count = len(all_tasks)
        in_prog = sum(1 for t in all_tasks if t.status == TaskState.IN_PROGRESS)
        pending_rev = sum(1 for t in all_tasks if t.status == TaskState.PENDING_REVIEW)
        sub_parts = [f"{total_count} tareas"]
        if in_prog:
            sub_parts.append(f"{in_prog} en curso")
        if pending_rev:
            sub_parts.append(f"{pending_rev} por revisar")
        self.window_title.set_subtitle(" · ".join(sub_parts))

        w.clear_box(self._tasks_list)

        if not tasks:
            self._stack.set_visible_child_name("empty_no_match")
            return

        proj_map = {p.id: p for p in projects}
        for task in tasks:
            project = proj_map.get(task.project_id)
            self._tasks_list.append(self._build_task_row(task, project))

        self._stack.set_visible_child_name("list")

    def _build_task_row(self, task: Task, project: Project | None) -> Gtk.Widget:
        expander = Adw.ExpanderRow()
        expander.set_title(task.title)

        proj_name = project.name if project else "Proyecto"
        subtitle = f"{proj_name} · {task.agent.label} · {w.relative_time(task.created_at)}"
        expander.set_subtitle(subtitle)

        expander.add_prefix(w.tool_chip(task.agent))
        pill = w.status_pill(task.status.label, task.status.style)
        expander.add_suffix(pill)

        detail_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=10,
            margin_bottom=14,
            margin_start=14,
            margin_end=14,
        )

        meta_box = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
        if project:
            meta_box.append(w.project_badge(project, size="medium"))
            p_lbl = Gtk.Label(label=project.name, xalign=0)
            p_lbl.add_css_class("heading")
            meta_box.append(p_lbl)

        meta_box.append(Gtk.Box(hexpand=True))

        ws_btn = Gtk.Button(label="Abrir espacio de trabajo", icon_name="folder-open-symbolic")
        ws_btn.add_css_class("flat")
        ws_btn.connect("clicked", lambda _b, pid=task.project_id: self.nav.open_project(pid))
        meta_box.append(ws_btn)

        detail_box.append(meta_box)

        session_info_box = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        session_info_box.add_css_class("task-detail-section")
        sess_icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        session_info_box.append(sess_icon)

        live_session = self.ctx.sessions.get(task.session_id) if task.session_id else None
        if live_session is not None:
            is_live_term = w.is_live_terminal(self.ctx.sessions, live_session.id)
            sess_desc = f"Sesión activa: {live_session.id} ({w.session_label(live_session.state, is_live_term)})"
            session_info_box.append(Gtk.Label(label=sess_desc, hexpand=True, xalign=0))
            term_btn = Gtk.Button(label="Ver terminal", icon_name="utilities-terminal-symbolic")
            term_btn.add_css_class("suggested-action")
            term_btn.connect("clicked", lambda _b, pid=task.project_id: self.nav.open_project(pid))
            session_info_box.append(term_btn)
        elif task.session_id:
            sess_desc = f"Sesión histórica vinculada: {task.session_id} (finalizada)"
            session_info_box.append(Gtk.Label(label=sess_desc, hexpand=True, xalign=0))
        else:
            session_info_box.append(Gtk.Label(label="Sin sesión asignada", hexpand=True, xalign=0))

        detail_box.append(session_info_box)

        prompt_header = Gtk.Box(spacing=8)
        prompt_header.append(Gtk.Label(label="Prompt original transferido:", xalign=0, css_classes=["heading"]))
        prompt_header.append(Gtk.Box(hexpand=True))

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copiar prompt al portapapeles")
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", lambda _b, p=task.prompt: self._copy_to_clipboard(p))
        prompt_header.append(copy_btn)
        detail_box.append(prompt_header)

        prompt_label = Gtk.Label(
            label=task.prompt or "(Sin contenido)",
            wrap=True,
            selectable=True,
            xalign=0,
            yalign=0,
        )
        prompt_label.add_css_class("task-prompt-box")
        detail_box.append(prompt_label)

        notes_header = Gtk.Box(spacing=8, margin_top=4)
        notes_header.append(Gtk.Label(label="Notas o resultado de la tarea:", xalign=0, css_classes=["heading"]))
        detail_box.append(notes_header)

        notes_box = Gtk.Box(spacing=8)
        notes_entry = Gtk.Entry(
            text=task.result_notes,
            placeholder_text="Registra aquí observaciones, resultados o confirmación…",
            hexpand=True,
        )
        save_notes_btn = Gtk.Button(label="Guardar notas", icon_name="document-save-symbolic")
        save_notes_btn.connect(
            "clicked",
            lambda _b, tid=task.id, ent=notes_entry: self._save_notes(tid, ent.get_text()),
        )
        notes_box.append(notes_entry)
        notes_box.append(save_notes_btn)
        detail_box.append(notes_box)

        actions_box = Gtk.Box(spacing=10, margin_top=6, valign=Gtk.Align.CENTER)

        if task.status != TaskState.COMPLETED:
            complete_btn = Gtk.Button(label="Marcar completada", icon_name="emblem-ok-symbolic")
            complete_btn.add_css_class("suggested-action")
            complete_btn.connect(
                "clicked",
                lambda _b, tid=task.id, ent=notes_entry: self._mark_task_completed(tid, ent.get_text()),
            )
            actions_box.append(complete_btn)

        state_label = Gtk.Label(label="Cambiar estado:")
        state_label.add_css_class("dim-label")
        actions_box.append(state_label)

        state_options = [
            TaskState.PENDING,
            TaskState.IN_PROGRESS,
            TaskState.PENDING_REVIEW,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        ]
        state_names = [st.label for st in state_options]
        state_dd = Gtk.DropDown.new_from_strings(state_names)
        current_idx = state_options.index(task.status) if task.status in state_options else 0
        state_dd.set_selected(current_idx)

        apply_state_btn = Gtk.Button(label="Aplicar")
        apply_state_btn.connect(
            "clicked",
            lambda _b, tid=task.id, dd=state_dd, opts=state_options: self._change_state(
                tid, opts[dd.get_selected()]
            ),
        )
        actions_box.append(state_dd)
        actions_box.append(apply_state_btn)

        detail_box.append(actions_box)

        history = self.ctx.tasks.list_history(task.id)
        if history:
            hist_expander = Gtk.Expander(label=f"Historial de cambios ({len(history)})", margin_top=6)
            hist_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6)
            for entry in history:
                from_lbl = TaskState(entry.from_status).label if entry.from_status else "Inicio"
                to_lbl = TaskState(entry.to_status).label if entry.to_status else entry.to_status
                note_str = f" — {entry.note}" if entry.note else ""
                time_str = w.relative_time(entry.created_at)
                line = f"• {from_lbl} → {to_lbl} ({time_str}){note_str}"
                h_lbl = Gtk.Label(label=line, xalign=0)
                h_lbl.add_css_class("task-history-row")
                hist_box.append(h_lbl)
            hist_expander.set_child(hist_box)
            detail_box.append(hist_expander)

        row_widget = Gtk.ListBoxRow(activatable=False, child=detail_box)
        expander.add_row(row_widget)
        return expander

    def _copy_to_clipboard(self, text: str) -> None:
        display = Gdk.Display.get_default()
        if display:
            clipboard = display.get_clipboard()
            clipboard.set(text)
            self.nav.toast("Prompt copiado al portapapeles")

    def _save_notes(self, task_id: int, notes: str) -> None:
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            self.ctx.tasks.update_notes(task_id, notes)
            self.nav.toast("Notas guardadas")
            self._render()

    def _mark_task_completed(self, task_id: int, notes: str) -> None:
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            self.ctx.tasks.mark_completed(task_id, result_notes=notes)
            self.nav.toast("Tarea marcada como completada")
            self._render()

    def _change_state(self, task_id: int, new_state: TaskState) -> None:
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            self.ctx.tasks.update_status(
                task_id, new_state, note="Estado modificado manualmente por el usuario"
            )
            self.nav.toast(f"Estado actualizado a «{new_state.label}»")
            self._render()

    # ---- MÉTODOS DE BIBLIOTECA DE PROMPTS ----

    def _refresh_template_filters(self) -> None:
        if not hasattr(self.ctx, "prompt_templates") or self.ctx.prompt_templates is None:
            return

        # Ámbitos / Proyectos
        projects = self.ctx.projects.list()
        scope_items = ["Todos los ámbitos", "Solo globales"] + [f"Proyecto: {p.name}" for p in projects]
        self._tmpl_project_dropdown.set_model(Gtk.StringList.new(scope_items))
        self._tmpl_project_dropdown.set_selected(0)

        # Categorías
        cats = ["Todas las categorías"] + self.ctx.prompt_templates.categories()
        self._tmpl_category_dropdown.set_model(Gtk.StringList.new(cats))
        self._tmpl_category_dropdown.set_selected(0)

    def _on_tmpl_project_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        idx = dropdown.get_selected()
        projects = self.ctx.projects.list()
        if idx == 0:
            self._tmpl_filter_scope = "all"
            self._tmpl_project_id = None
        elif idx == 1:
            self._tmpl_filter_scope = "global"
            self._tmpl_project_id = None
        else:
            proj_idx = idx - 2
            if 0 <= proj_idx < len(projects):
                self._tmpl_filter_scope = "project"
                self._tmpl_project_id = projects[proj_idx].id
        self._render_templates()

    def _on_tmpl_category_selected(self, dropdown: Gtk.DropDown, _pspec: object) -> None:
        idx = dropdown.get_selected()
        model = dropdown.get_model()
        if model and idx >= 0:
            selected = model.get_string(idx)
            self._tmpl_filter_category = "all" if idx == 0 else selected
            self._render_templates()

    def _on_tmpl_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._tmpl_search_text = entry.get_text().strip()
        self._render_templates()

    def _clear_tmpl_filters(self) -> None:
        self._tmpl_filter_scope = "all"
        self._tmpl_project_id = None
        self._tmpl_filter_category = "all"
        self._tmpl_search_text = ""
        self._tmpl_project_dropdown.set_selected(0)
        self._tmpl_category_dropdown.set_selected(0)
        self._tmpl_search_entry.set_text("")
        self._render_templates()

    def _render_templates(self) -> None:
        if not hasattr(self.ctx, "prompt_templates") or self.ctx.prompt_templates is None:
            self._tmpl_stack.set_visible_child_name("empty_no_templates")
            return

        cat = None if self._tmpl_filter_category == "all" else self._tmpl_filter_category

        if self._tmpl_filter_scope == "project" and self._tmpl_project_id is not None:
            templates = self.ctx.prompt_templates.list_for_project(
                project_id=self._tmpl_project_id,
                category=cat,
                search=self._tmpl_search_text,
            )
        else:
            templates = self.ctx.prompt_templates.list_all(
                category=cat,
                search=self._tmpl_search_text,
            )
            if self._tmpl_filter_scope == "global":
                templates = [t for t in templates if t.scope == PromptTemplateScope.GLOBAL]

        all_total = len(self.ctx.prompt_templates.list_all())
        if all_total == 0:
            self._tmpl_stack.set_visible_child_name("empty_no_templates")
            self.window_title.set_subtitle("0 plantillas")
            return

        if self._active_section == "templates":
            self.window_title.set_subtitle(f"{len(templates)} plantillas disponibles")

        w.clear_box(self._templates_list)

        if not templates:
            self._tmpl_stack.set_visible_child_name("empty_no_match")
            return

        projects = self.ctx.projects.list()
        proj_map = {p.id: p for p in projects}

        for tmpl in templates:
            self._templates_list.append(self._build_template_row(tmpl, proj_map.get(tmpl.project_id)))

        self._tmpl_stack.set_visible_child_name("list")

    def _build_template_row(self, tmpl: PromptTemplate, project: Project | None) -> Gtk.Widget:
        expander = Adw.ExpanderRow()
        expander.set_title(tmpl.name)

        time_str = w.relative_time(tmpl.updated_at or tmpl.created_at)
        desc_prefix = f"{tmpl.description} · " if tmpl.description else ""
        expander.set_subtitle(f"{desc_prefix}{tmpl.category} · {time_str}")

        # Chip de ámbito
        if tmpl.scope == PromptTemplateScope.GLOBAL:
            scope_badge = w.chip("Global", "tag-chip")
        else:
            p_name = project.name if project else "Proyecto"
            scope_badge = w.chip(f"Proyecto: {p_name}", "tag-chip")
        expander.add_suffix(scope_badge)

        # Contenido desplegado
        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=8,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        # Variables detectadas
        vars_found = tmpl.variables()
        if vars_found:
            vars_chip_box = Gtk.Box(spacing=6)
            vars_chip_box.append(Gtk.Label(label="Variables:", css_classes=["dim-label"]))
            for v in vars_found:
                vars_chip_box.append(w.chip(f"{{{{{v}}}}}", "tool-chip"))
            content_box.append(vars_chip_box)

        # Prompt
        p_label = Gtk.Label(
            label=tmpl.content,
            wrap=True,
            selectable=True,
            xalign=0,
            yalign=0,
        )
        p_label.add_css_class("task-prompt-box")
        content_box.append(p_label)

        # Acciones
        btn_bar = Gtk.Box(spacing=8, margin_top=4)

        edit_btn = Gtk.Button(label="Editar", icon_name="document-edit-symbolic")
        edit_btn.connect("clicked", lambda _b, t=tmpl: self._edit_template_dialog(t))
        btn_bar.append(edit_btn)

        copy_btn = Gtk.Button(label="Copiar prompt", icon_name="edit-copy-symbolic")
        copy_btn.connect("clicked", lambda _b, c=tmpl.content: self._copy_to_clipboard(c))
        btn_bar.append(copy_btn)

        if tmpl.project_id:
            open_proj_btn = Gtk.Button(label="Abrir en workspace", icon_name="folder-open-symbolic")
            open_proj_btn.add_css_class("flat")
            open_proj_btn.connect("clicked", lambda _b, pid=tmpl.project_id: self.nav.open_project(pid))
            btn_bar.append(open_proj_btn)

        btn_bar.append(Gtk.Box(hexpand=True))

        del_btn = Gtk.Button(label="Eliminar", icon_name="user-trash-symbolic")
        del_btn.add_css_class("destructive-action")
        del_btn.connect("clicked", lambda _b, t=tmpl: self._confirm_delete_template(t))
        btn_bar.append(del_btn)

        content_box.append(btn_bar)

        row_widget = Gtk.ListBoxRow(activatable=False, child=content_box)
        expander.add_row(row_widget)
        return expander

    def _open_new_template_dialog(self) -> None:
        dialog = TemplateEditorDialog(
            ctx=self.ctx,
            project=None,
            on_saved=lambda _t: self.nav.toast("Plantilla creada correctamente"),
        )
        dialog.present(self.get_root())

    def _edit_template_dialog(self, template: PromptTemplate) -> None:
        project = None
        if template.project_id:
            project = self.ctx.projects.get(template.project_id)
        dialog = TemplateEditorDialog(
            ctx=self.ctx,
            template=template,
            project=project,
            on_saved=lambda _t: self.nav.toast("Plantilla actualizada"),
        )
        dialog.present(self.get_root())

    def _confirm_delete_template(self, template: PromptTemplate) -> None:
        dialog = Adw.AlertDialog(
            heading=f"¿Eliminar plantilla «{template.name}»?",
            body="Esta acción no se puede deshacer.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dlg: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                self.ctx.prompt_templates.delete(template.id)
                self.nav.toast("Plantilla eliminada")

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())
