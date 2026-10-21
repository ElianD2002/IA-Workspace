"""A. Inicio — resumen de todos los proyectos."""

from __future__ import annotations

from gi.repository import Adw, Gtk, Pango

from ia_workspace.domain.models import Project
from ia_workspace.services.detection import inspect_folder
from ia_workspace.sessions.models import SessionState
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp

MAX_CONTINUE_CARDS = 4


class HomePage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Inicio")
        new_prompt = Gtk.Button(tooltip_text="Nuevo prompt (Ctrl+N)")
        new_prompt.set_child(Adw.ButtonContent(icon_name="document-edit-symbolic", label="Nuevo prompt"))
        new_prompt.connect("clicked", lambda _b: self._new_prompt())
        self.header.pack_end(new_prompt)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_empty(), "empty")
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._stack.add_named(scrolled_clamp(self._body), "content")
        self.set_content(self._stack)

    def _build_empty(self) -> Gtk.Widget:
        button = Gtk.Button(label="Agregar proyecto", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda _b: self.nav.request_add_project())
        return Adw.StatusPage(
            icon_name="folder-symbolic",
            title="Bienvenido a IA Workspace",
            description=(
                "Registra una carpeta local para empezar. Cada proyecto tendrá su propio "
                "contexto, tareas, conversaciones, terminales y memoria."
            ),
            child=button,
        )

    def _new_prompt(self) -> None:
        target = self.ctx.workspace.active_project_id or self.ctx.workspace.last_project_id
        projects = self.ctx.projects.list()
        if target is None or not any(p.id == target for p in projects):
            target = projects[0].id if projects else None
        if target is None:
            self.nav.request_add_project()
        else:
            self.nav.open_project(target)

    # ---- render ---------------------------------------------------------------

    def refresh(self) -> None:
        projects = self.ctx.projects.list()
        if not projects:
            self._stack.set_visible_child_name("empty")
            return
        self._stack.set_visible_child_name("content")
        w.clear_box(self._body)
        self._render_continue(projects)
        self._render_attention(projects)
        self._render_columns(projects)
        self._render_activity(projects)

    def _render_continue(self, projects: list[Project]) -> None:
        recent = sorted(
            projects,
            key=lambda p: (p.last_opened_at is not None, p.last_opened_at or p.created_at),
            reverse=True,
        )[:MAX_CONTINUE_CARDS]
        self._body.append(
            w.group_header(
                "Continuar donde lo dejaste",
                "El estado de cada workspace se conserva al cambiar de proyecto",
            )
        )
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=2,
            min_children_per_line=1,
            column_spacing=14,
            row_spacing=14,
            homogeneous=True,
        )
        for project in recent:
            flow.append(self._continue_card(project))
        self._body.append(flow)

    def _continue_card(self, project: Project) -> Gtk.Widget:
        info = inspect_folder(project.path)
        active = self.ctx.sessions.active_sessions(project.id)
        working = any(s.state is SessionState.WORKING for s in active)
        waiting = any(s.state is SessionState.WAITING_APPROVAL for s in active)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("card")
        card.add_css_class("continue-card")

        top = Gtk.Box(spacing=12)
        top.append(w.project_badge(project, "md"))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=project.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("title-4")
        names.append(title)
        names.append(w.caption(info.branch or "Sin repositorio Git", mono=bool(info.branch)))
        top.append(names)
        if waiting:
            top.append(w.status_pill("Aprobación", "wait"))
        elif working:
            top.append(w.status_pill("Trabajando", "working", spinning=True))
        elif active:
            top.append(w.status_pill("Sesiones abiertas", "idle"))
        else:
            top.append(w.status_pill("En pausa", "idle"))
        card.append(top)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        detail.add_css_class("card-info")
        line1 = (
            f"{len(active)} sesión activa" if len(active) == 1 else f"{len(active)} sesiones activas"
        ) if active else "Sin sesiones activas"
        first = Gtk.Label(label=line1, xalign=0)
        first.add_css_class("heading")
        detail.append(first)
        detail.append(w.caption(f"{info.stack_label or 'Stack no detectado'} · abierto: {w.relative_time(project.last_opened_at).lower()}"))
        card.append(detail)

        bottom = Gtk.Box(spacing=8)
        for kind in project.agents:
            bottom.append(w.tool_chip(kind))
        bottom.append(Gtk.Box(hexpand=True))
        open_btn = Gtk.Button(label="Abrir workspace")
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", lambda _b, pid=project.id: self.nav.open_project(pid))
        bottom.append(open_btn)
        card.append(bottom)
        return card

    def _render_attention(self, projects: list[Project]) -> None:
        names = {p.id: p.name for p in projects}
        needs = [
            s
            for s in self.ctx.sessions.sessions()
            if s.state in (SessionState.WAITING_APPROVAL, SessionState.ERROR, SessionState.DISCONNECTED)
        ]
        count = f"{len(needs)} elemento{'s' if len(needs) != 1 else ''}" if needs else ""
        self._body.append(w.group_header("Requiere tu atención", count))
        box = w.boxed_list()
        for s in needs:
            row = Adw.ActionRow(
                title=f"{s.spec.kind.label}: {s.state.label.lower()}",
                subtitle=f"{names.get(s.project_id, '—')} · {s.detail or s.spec.title or 'Sesión'}",
            )
            row.add_prefix(w.session_pill(s.state, s.spec.kind))
            button = Gtk.Button(label="Abrir", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, pid=s.project_id: self.nav.open_project(pid))
            row.add_suffix(button)
            box.append(row)
        if not needs:
            box.append(w.info_row("Nada pendiente. Los agentes no necesitan nada de ti ahora.", "object-select-symbolic"))
        self._body.append(box)

    def _render_columns(self, projects: list[Project]) -> None:
        names = {p.id: p.name for p in projects}
        columns = Gtk.Box(spacing=14, homogeneous=True)

        running_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        agents_btn = Gtk.Button(label="Centro de agentes")
        agents_btn.add_css_class("flat")
        agents_btn.connect("clicked", lambda _b: self.nav.navigate("agents"))
        running_col.append(w.group_header("Sesiones en ejecución", action=agents_btn))
        running = w.boxed_list()
        active = self.ctx.sessions.active_sessions()
        for s in active:
            row = Adw.ActionRow(title=s.spec.title or s.spec.kind.label, subtitle=names.get(s.project_id, "—"))
            row.add_prefix(w.tool_chip(s.spec.kind))
            row.add_suffix(w.session_pill(s.state, s.spec.kind))
            row.set_activatable(True)
            row.connect("activated", lambda _r, pid=s.project_id: self.nav.open_project(pid))
            running.append(row)
        if not active:
            running.append(w.info_row("Ningún agente está trabajando ahora.", "media-playback-pause-symbolic"))
        running_col.append(running)
        columns.append(running_col)

        tasks_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tasks_btn = Gtk.Button(label="Ver todas")
        tasks_btn.add_css_class("flat")
        tasks_btn.connect("clicked", lambda _b: self.nav.navigate("tasks"))
        tasks_col.append(w.group_header("Tareas pendientes", action=tasks_btn))
        tasks = w.boxed_list()
        tasks.append(w.info_row("Sin tareas pendientes. Las tareas se crean al transferir un prompt.", "view-list-bullet-symbolic"))
        tasks_col.append(tasks)
        columns.append(tasks_col)

        self._body.append(columns)

    def _render_activity(self, projects: list[Project]) -> None:
        self._body.append(w.group_header("Actividad reciente"))
        events: list[tuple[object, str, str, str]] = []
        for p in projects:
            if p.created_at:
                events.append((p.created_at, "list-add-symbolic", "Proyecto registrado", p.name))
            if p.last_opened_at:
                events.append((p.last_opened_at, "folder-open-symbolic", "Workspace abierto", p.name))
        events.sort(key=lambda e: e[0], reverse=True)
        box = w.boxed_list()
        for moment, icon, text, pname in events[:6]:
            row = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
            when = w.caption(w.relative_time(moment), mono=True)  # type: ignore[arg-type]
            when.set_size_request(72, -1)
            row.append(when)
            image = Gtk.Image.new_from_icon_name(icon)
            image.add_css_class("dim-label")
            row.append(image)
            row.append(Gtk.Label(label=text, xalign=0, hexpand=True))
            row.append(w.caption(pname))
            box.append(Gtk.ListBoxRow(activatable=False, child=row))
        self._body.append(box)
