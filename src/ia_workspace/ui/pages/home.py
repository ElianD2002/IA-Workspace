"""A. Inicio — dashboard operativo del workspace."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from gi.repository import Adw, GLib, Gtk, Pango

from ia_workspace.brain.proposals import ProposalStatus
from ia_workspace.domain.models import Project
from ia_workspace.services.detection import inspect_folder
from ia_workspace.sessions.models import SessionState
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp

MAX_CONTINUE_CARDS = 6
MAX_ACTIVITY_EVENTS = 8


class HomePage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Inicio")
        new_prompt = Gtk.Button(tooltip_text="Nuevo prompt (Ctrl+N)")
        new_prompt.set_child(Adw.ButtonContent(icon_name="document-edit-symbolic", label="Nuevo prompt"))
        new_prompt.connect("clicked", lambda _b: self._new_prompt())
        self.header.pack_end(new_prompt)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._build_empty(), "empty")
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._stack.add_named(scrolled_clamp(self._body), "content")
        self.set_content(self._stack)

        self._unsubscribers: list[Callable[[], None]] = []
        self._setup_listeners()

    def _setup_listeners(self) -> None:
        if hasattr(self.ctx, "prompt_transfers") and hasattr(self.ctx.prompt_transfers, "subscribe"):
            unsub = self.ctx.prompt_transfers.subscribe(lambda *_: GLib.idle_add(self.refresh))
            self._unsubscribers.append(unsub)
        if hasattr(self.ctx, "tasks") and hasattr(self.ctx.tasks, "subscribe"):
            unsub = self.ctx.tasks.subscribe(lambda *_: GLib.idle_add(self.refresh))
            self._unsubscribers.append(unsub)
        if hasattr(self.ctx, "brain") and hasattr(self.ctx.brain, "subscribe"):
            unsub = self.ctx.brain.subscribe(lambda *_: GLib.idle_add(self.refresh))
            self._unsubscribers.append(unsub)
        if hasattr(self.ctx, "quotas") and hasattr(self.ctx.quotas, "subscribe"):
            unsub = self.ctx.quotas.subscribe(lambda *_: GLib.idle_add(self.refresh))
            self._unsubscribers.append(unsub)

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

    # ---- render principal ----------------------------------------------------

    def refresh(self) -> None:
        projects = self.ctx.projects.list()
        if not projects:
            self._stack.set_visible_child_name("empty")
            return
        self._stack.set_visible_child_name("content")
        w.clear_box(self._body)

        self._render_summary(projects)
        self._render_attention(projects)
        self._render_continue(projects)
        self._render_sessions(projects)
        self._render_activity(projects)

    # ---- A. Resumen superior compacto -----------------------------------------

    def _render_summary(self, projects: list[Project]) -> None:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=3,
            min_children_per_line=1,
            column_spacing=12,
            row_spacing=12,
            homogeneous=True,
        )
        flow.set_margin_top(4)
        flow.set_margin_bottom(8)

        # 1. Proyectos registrados
        flow.append(
            self._kpi_card(
                value=len(projects),
                label="Proyectos registrados",
                icon_name="folder-symbolic",
                on_clicked=lambda: self.nav.navigate("projects"),
                tooltip="Ver todos los proyectos registrados",
            )
        )

        # 2. Sesiones activas de agentes
        active_sessions = self.ctx.sessions.active_sessions()
        flow.append(
            self._kpi_card(
                value=len(active_sessions),
                label="Sesiones activas",
                icon_name="utilities-terminal-symbolic",
                on_clicked=lambda: self.nav.navigate("agents"),
                tooltip="Ver sesiones de agentes y centro de ejecución",
            )
        )

        # 3. Propuestas de memoria pendientes
        pending_props = 0
        if self.ctx.brain is not None:
            pending_props = sum(self.ctx.brain.count_pending_proposals(p.id) for p in projects)
        flow.append(
            self._kpi_card(
                value=pending_props,
                label="Propuestas pendientes",
                icon_name="document-send-symbolic",
                on_clicked=lambda: self.nav.navigate("memory"),
                tooltip="Revisar propuestas de memoria en el Cerebro Compartido",
            )
        )

        self._body.append(flow)

    def _kpi_card(
        self,
        value: int,
        label: str,
        icon_name: str,
        on_clicked: Callable[[], None],
        tooltip: str = "",
    ) -> Gtk.Widget:
        btn = Gtk.Button(valign=Gtk.Align.CENTER)
        btn.add_css_class("flat")
        btn.add_css_class("card")
        btn.add_css_class("summary-kpi-card")
        if tooltip:
            btn.set_tooltip_text(tooltip)
        btn.connect("clicked", lambda _b: on_clicked())

        box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=14, margin_end=14)

        icon_box = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        icon_box.add_css_class("kpi-icon-box")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(20)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_hexpand(True)
        icon.set_vexpand(True)
        icon_box.append(icon)
        box.append(icon_box)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        val_lbl = Gtk.Label(label=str(value), xalign=0)
        val_lbl.add_css_class("title-2")
        val_lbl.add_css_class("kpi-value")
        texts.append(val_lbl)
        texts.append(w.caption(label))
        box.append(texts)

        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")
        arrow.set_valign(Gtk.Align.CENTER)
        box.append(arrow)

        btn.set_child(box)
        return btn

    # ---- B. Requiere tu atención ----------------------------------------------

    def _render_attention(self, projects: list[Project]) -> None:
        names = {p.id: p.name for p in projects}
        items: list[tuple[str, str, str, str, str, Callable[[], None]]] = []

        # 1. Propuestas de memoria pendientes por proyecto
        if self.ctx.brain is not None:
            for p in projects:
                for prop in self.ctx.brain.list_proposals(p.id, status=ProposalStatus.PENDING):
                    items.append((
                        f"Propuesta: {prop.title}",
                        f"Proyecto: {p.name} · Categoría: «{prop.category}» · {prop.agent_kind or 'Agente'}",
                        "Propuesta",
                        "wait",
                        "Revisar",
                        lambda: self.nav.navigate("memory"),
                    ))

        # 2. Sesiones con errores o esperando confirmación
        attention_states = (
            SessionState.WAITING_APPROVAL,
            SessionState.FAILED,
            SessionState.ERROR,
            SessionState.DISCONNECTED,
        )
        for s in self.ctx.sessions.sessions():
            if s.state in attention_states:
                pname = names.get(s.project_id, "—")
                is_wait = s.state is SessionState.WAITING_APPROVAL
                badge = "Aprobación" if is_wait else "Error"
                style = "wait" if is_wait else "error"
                items.append((
                    f"{s.spec.kind.label}: {s.state.label}",
                    f"Proyecto: {pname} · {s.detail or s.spec.title or 'Sesión de agente'}",
                    badge,
                    style,
                    "Abrir workspace",
                    lambda pid=s.project_id: self.nav.open_project(pid),
                ))

        # 3. Alertas de consumo de cuotas de suscripciones oficiales
        if self.ctx.quotas is not None:
            snapshots = self.ctx.quotas.get_all_snapshots()
            for snap in snapshots.values():
                for group in snap.groups:
                    for win in group.windows:
                        used = win.used_percentage
                        if used is not None and used >= 80.0:
                            is_crit = used >= 95.0
                            badge = "Límite próximo" if is_crit else "Consumo alto"
                            style = "error" if is_crit else "wait"
                            dur = f"{win.window_minutes}m" if win.window_minutes else win.name
                            items.append((
                                f"{snap.provider.label}: {used:.1f}% consumido ({dur})",
                                "Suscripción oficial · Se sugiere moderar el consumo para evitar bloqueos",
                                badge,
                                style,
                                "Ver cuotas",
                                lambda: self.nav.navigate("agents"),
                            ))

        count_badge = f"{len(items)} elemento{'s' if len(items) != 1 else ''}" if items else ""
        self._body.append(w.group_header("Requiere tu atención", count_badge))

        box = w.boxed_list()
        for title, subtitle, badge_text, badge_style, action_label, action_cb in items:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(w.status_pill(badge_text, badge_style))
            btn = Gtk.Button(label=action_label, valign=Gtk.Align.CENTER)
            btn.add_css_class("suggested-action" if badge_style == "error" else "flat")
            btn.connect("clicked", lambda _b, cb=action_cb: cb())
            row.add_suffix(btn)
            row.set_activatable(True)
            row.connect("activated", lambda _r, cb=action_cb: cb())
            box.append(row)

        if not items:
            box.append(
                w.info_row(
                    "Todo al día. No hay sesiones con error, propuestas pendientes ni alertas de suscripción.",
                    "emblem-ok-symbolic",
                )
            )
        self._body.append(box)

    # ---- C. Continuar trabajando ----------------------------------------------

    def _render_continue(self, projects: list[Project]) -> None:
        recent = sorted(
            projects,
            key=lambda p: (p.last_opened_at is not None, p.last_opened_at or p.created_at),
            reverse=True,
        )[:MAX_CONTINUE_CARDS]

        projects_btn = Gtk.Button(label="Ver todos")
        projects_btn.add_css_class("flat")
        projects_btn.connect("clicked", lambda _b: self.nav.navigate("projects"))

        self._body.append(
            w.group_header(
                "Continuar trabajando",
                "Acceso directo a tus workspaces recientes",
                action=projects_btn,
            )
        )
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            max_children_per_line=2,
            min_children_per_line=1,
            column_spacing=12,
            row_spacing=12,
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
        has_error = any(s.state in (SessionState.FAILED, SessionState.ERROR) for s in active)
        pending_props = self.ctx.brain.count_pending_proposals(project.id) if self.ctx.brain is not None else 0

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("card")
        card.add_css_class("project-card")

        # Fila superior: badge, nombre + rama, estado general
        top = Gtk.Box(spacing=10)
        top.append(w.project_badge(project, "md"))

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        title = Gtk.Label(label=project.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("title-4")
        names.append(title)

        branch_text = f" {info.branch}" if info.branch else "Sin repositorio Git"
        names.append(w.caption(branch_text, mono=bool(info.branch)))
        top.append(names)

        if waiting:
            top.append(w.status_pill("Aprobación", "wait"))
        elif working:
            top.append(w.status_pill("Trabajando", "working", spinning=True))
        elif has_error:
            top.append(w.status_pill("Error", "error"))
        elif active:
            top.append(w.status_pill(f"{len(active)} activa{'s' if len(active) > 1 else ''}", "idle"))
        else:
            top.append(w.status_pill("En pausa", "idle"))
        card.append(top)

        # Fila intermedia: metadatos de actividad y propuestas
        meta_row = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        act_text = f"Actividad: {w.relative_time(project.last_opened_at or project.created_at).lower()}"
        meta_row.append(w.caption(act_text))
        if info.stack_label:
            meta_row.append(w.caption(f"· {info.stack_label}"))

        meta_row.append(Gtk.Box(hexpand=True))
        if pending_props > 0:
            prop_pill = w.status_pill(f"{pending_props} propuesta{'s' if pending_props > 1 else ''}", "wait")
            prop_pill.set_tooltip_text("Propuestas de memoria pendientes de revisión")
            meta_row.append(prop_pill)
        card.append(meta_row)

        # Fila inferior: agentes y botón de acción principal
        bottom = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        for kind in project.agents:
            bottom.append(w.tool_chip(kind))
        bottom.append(Gtk.Box(hexpand=True))

        open_btn = Gtk.Button(label="Abrir workspace")
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", lambda _b, pid=project.id: self.nav.open_project(pid))
        bottom.append(open_btn)
        card.append(bottom)

        return card

    # ---- D. Sesiones en ejecución ---------------------------------------------

    def _render_sessions(self, projects: list[Project]) -> None:
        names = {p.id: p.name for p in projects}
        active = self.ctx.sessions.active_sessions()

        agents_btn = Gtk.Button(label="Centro de agentes")
        agents_btn.add_css_class("flat")
        agents_btn.connect("clicked", lambda _b: self.nav.navigate("agents"))

        count_text = f"{len(active)} activa{'s' if len(active) != 1 else ''}" if active else ""
        self._body.append(w.group_header("Sesiones en ejecución", count_text, action=agents_btn))

        box = w.boxed_list()
        for s in active:
            pname = names.get(s.project_id, "—")
            sub = f"Proyecto: {pname}"
            if s.spec.cwd:
                sub += f" · {w.short_path(s.spec.cwd)}"
            row = Adw.ActionRow(title=s.spec.title or s.spec.kind.label, subtitle=sub)
            row.add_prefix(w.tool_chip(s.spec.kind))
            row.add_suffix(w.session_pill(s.state, w.is_live_terminal(self.ctx.sessions, s.id)))

            open_btn = Gtk.Button(label="Abrir workspace", valign=Gtk.Align.CENTER)
            open_btn.add_css_class("flat")
            open_btn.connect("clicked", lambda _b, pid=s.project_id: self.nav.open_project(pid))
            row.add_suffix(open_btn)

            row.set_activatable(True)
            row.connect("activated", lambda _r, pid=s.project_id: self.nav.open_project(pid))
            box.append(row)

        if not active:
            box.append(w.info_row("Ningún agente está trabajando en este momento.", "media-playback-pause-symbolic"))
        self._body.append(box)

    # ---- E. Actividad reciente ------------------------------------------------

    def _render_activity(self, projects: list[Project]) -> None:
        self._body.append(w.group_header("Actividad reciente", "Eventos reales registrados en el workspace"))

        events: list[tuple[datetime, str, str, str, int]] = []

        for p in projects:
            if p.created_at:
                events.append((p.created_at, "list-add-symbolic", "Proyecto registrado", p.name, p.id))
            if p.last_opened_at:
                events.append((p.last_opened_at, "folder-open-symbolic", "Workspace abierto", p.name, p.id))

            if self.ctx.prompt_transfers is not None:
                for t in self.ctx.prompt_transfers.list(p.id):
                    if t.created_at:
                        if t.status == "sent":
                            desc = f"Prompt transferido a {t.agent.label}"
                            icon = "document-send-symbolic"
                        elif t.status == "copied":
                            desc = f"Prompt copiado para {t.agent.label}"
                            icon = "edit-copy-symbolic"
                        else:
                            desc = f"Fallo al transferir prompt a {t.agent.label}"
                            icon = "dialog-error-symbolic"
                        events.append((t.created_at, icon, desc, p.name, p.id))

            if self.ctx.brain is not None:
                for pr in self.ctx.brain.list_proposals(p.id):
                    if pr.created_at:
                        events.append(
                            (
                                pr.created_at,
                                "user-bookmarks-symbolic",
                                f"Propuesta de memoria: {pr.title}",
                                p.name,
                                p.id,
                            )
                        )

            if self.ctx.chat_links is not None:
                for cl in self.ctx.chat_links.list(p.id):
                    if cl.created_at:
                        events.append((cl.created_at, "chat-symbolic", f"Chat vinculado: {cl.title}", p.name, p.id))

        def _dt_key(dt: datetime) -> datetime:
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

        events.sort(key=lambda e: _dt_key(e[0]), reverse=True)

        box = w.boxed_list()
        for moment, icon, desc, pname, pid in events[:MAX_ACTIVITY_EVENTS]:
            row_box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)

            when = w.caption(w.relative_time(moment), mono=True)
            when.set_size_request(80, -1)
            row_box.append(when)

            image = Gtk.Image.new_from_icon_name(icon)
            image.add_css_class("dim-label")
            row_box.append(image)

            desc_lbl = Gtk.Label(label=desc, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            row_box.append(desc_lbl)

            row_box.append(w.caption(pname))

            row = Gtk.ListBoxRow(activatable=True, child=row_box)
            row.connect("activate", lambda _r, target=pid: self.nav.open_project(target))
            box.append(row)

        if not events:
            box.append(w.info_row("Sin actividad registrada todavía.", "document-open-recent-symbolic"))
        self._body.append(box)
