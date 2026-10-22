"""E. Cerebro compartido — memoria en Markdown, por proyecto y global.

Sigue el prototipo: selector «Proyecto | Global» en la cabecera, categorías a la izquierda
y, a la derecha, el título de la categoría, el buscador, su descripción y una tarjeta por
documento. Las tarjetas muestran solo el resumen (o, al buscar, un fragmento); el
documento completo se lee al abrirlo.
"""

from __future__ import annotations

from gi.repository import Adw, Gio, GObject, Gtk, Pango

from ia_workspace.brain.cli import cli_command
from ia_workspace.brain.model import BrainError, DocumentMeta, Scope, categories_for, category
from ia_workspace.brain.proposals import ProposalOperation, ProposalStatus
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page


class MemoryPage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Cerebro compartido")
        self.brain = self.ctx.brain
        self._scope = Scope.PROJECT
        self._project = None
        self._project_ids: list[int] = []
        self._categories: dict[Scope, str] = {Scope.PROJECT: "estado", Scope.GLOBAL: "preferencias"}
        self._proposals_filter = "pending"
        self._query = ""
        self._syncing = False
        self._last_dialog = None

        # ---- cabecera ------------------------------------------------------------------
        self._project_model = Gtk.StringList()
        self.project_picker = Gtk.DropDown(model=self._project_model, tooltip_text="Proyecto", visible=False)
        self.project_picker.connect("notify::selected", self._on_project_picked)
        self.header.pack_start(self.project_picker)
        self.categories_button = Gtk.ToggleButton(icon_name="view-list-bullet-symbolic", visible=False,
                                                  tooltip_text="Categorías")
        self.header.pack_start(self.categories_button)

        self.scope_toggle = Adw.ToggleGroup()
        self.scope_toggle.add(Adw.Toggle(name="project", label="Proyecto", icon_name="folder-symbolic"))
        self.scope_toggle.add(Adw.Toggle(name="global", label="Global", icon_name="avatar-default-symbolic"))
        self.scope_toggle.connect("notify::active-name", self._on_scope_changed)
        self.header.set_title_widget(self.scope_toggle)
        new = Gtk.Button(tooltip_text="Nueva entrada")
        new.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Nueva entrada"))
        new.connect("clicked", lambda _b: self.request_new())
        self.header.pack_end(new)
        self.new_button = new

        # ---- banner: qué reciben los agentes -------------------------------------------
        self.banner = Adw.Banner(revealed=True)
        self.banner.connect("button-clicked", lambda _b: self.copy_agent_brief())

        # ---- categorías ------------------------------------------------------------------
        self._category_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._category_list.add_css_class("navigation-sidebar")
        self._category_list.connect("row-activated", self._on_category_activated)
        self._category_rows: dict[str, Gtk.ListBoxRow] = {}
        self._category_counts: dict[str, Gtk.Label] = {}
        self._proposals_row: Gtk.ListBoxRow | None = None
        nav_scroll = Gtk.ScrolledWindow(child=self._category_list, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                        vexpand=True, width_request=230)

        # ---- contenido -------------------------------------------------------------------
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=22, margin_bottom=32,
                       margin_start=24, margin_end=24)
        self.scope_label = w.caption("")
        body.append(self.scope_label)
        top = Gtk.Box(spacing=12)
        self.heading = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.heading.add_css_class("title-2")
        top.append(self.heading)
        self.search = Gtk.SearchEntry(placeholder_text="Buscar en la memoria", width_request=260)
        self.search.connect("search-changed", lambda e: self.set_query(e.get_text()))
        top.append(self.search)
        body.append(top)
        self.description = Gtk.Label(xalign=0, wrap=True)
        self.description.add_css_class("dim-label")
        body.append(self.description)
        self.cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8)
        body.append(self.cards)
        clamp = Adw.Clamp(maximum_size=900, child=body)
        content = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)

        self.split = Adw.OverlaySplitView(sidebar=nav_scroll, content=content, min_sidebar_width=200,
                                          max_sidebar_width=260)
        self.split.bind_property("show-sidebar", self.categories_button, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        bin_ = Adw.BreakpointBin(child=self.split, width_request=360, height_request=320)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 860sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.categories_button, "visible", True)
        bin_.add_breakpoint(narrow)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.append(self.banner)
        main.append(bin_)
        self._stack = Gtk.Stack()
        self._stack.add_named(main, "brain")
        self._stack.add_named(Adw.StatusPage(icon_name="user-bookmarks-symbolic", title="Memoria no disponible",
                                             description="No hay carpeta de datos para guardar la memoria."),
                              "unavailable")
        self.set_content(self._stack)
        if self.brain is not None:
            self.brain.subscribe(self._on_brain_changed)

    # ---- estado ---------------------------------------------------------------------

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def project_key(self) -> str:
        if self._scope is Scope.GLOBAL or self._project is None:
            return ""
        return self.brain.project_key(self._project)

    @property
    def category_key(self) -> str:
        return self._categories[self._scope]

    def refresh(self) -> None:
        if self.brain is None:
            self._stack.set_visible_child_name("unavailable")
            return
        projects = self.ctx.projects.list()
        preferred = self._project.id if self._project is not None else (
            self.ctx.workspace.active_project_id or self.ctx.workspace.last_project_id)
        by_id = {p.id: p for p in projects}
        self._project = by_id.get(preferred) or (projects[0] if projects else None)
        self._syncing = True
        try:
            self._project_ids = [p.id for p in projects]
            self._project_model.splice(0, self._project_model.get_n_items(), [p.name for p in projects])
            if self._project is not None:
                self.project_picker.set_selected(self._project_ids.index(self._project.id))
            project_toggle = self.scope_toggle.get_toggle_by_name("project")
            project_toggle.set_label(self._project.name if self._project else "Proyecto")
            project_toggle.set_enabled(self._project is not None)
            if self._project is None:
                self._scope = Scope.GLOBAL
            self.scope_toggle.set_active_name("project" if self._scope is Scope.PROJECT else "global")
        finally:
            self._syncing = False
        self._render()

    def _on_scope_changed(self, *_args: object) -> None:
        if not self._syncing:
            self.select_scope(Scope.GLOBAL if self.scope_toggle.get_active_name() == "global" else Scope.PROJECT)

    def _on_project_picked(self, *_args: object) -> None:
        index = self.project_picker.get_selected()
        if not self._syncing and 0 <= index < len(self._project_ids):
            self.select_project(self._project_ids[index])

    def select_scope(self, scope: Scope) -> None:
        if scope is Scope.PROJECT and self._project is None:
            scope = Scope.GLOBAL
        if scope is Scope.GLOBAL and self._categories.get(Scope.GLOBAL) == "__proposals__":
            self._categories[Scope.GLOBAL] = "preferencias"
        self._scope = scope
        self._syncing = True
        self.scope_toggle.set_active_name("project" if scope is Scope.PROJECT else "global")
        self._syncing = False
        self._render()

    def select_project(self, project_id: int) -> None:
        project = self.ctx.projects.get(project_id)
        if project is not None:
            self._project = project
            self.refresh()

    def select_category(self, key: str) -> None:
        if key == "__proposals__" or category(self._scope, key) is not None:
            self._categories[self._scope] = key
            self._render()

    def set_query(self, query: str) -> None:
        self._query = query.strip()
        if self.search.get_text() != query:
            self.search.set_text(query)
        self._render()

    def _on_brain_changed(self, scope: Scope, key: str) -> None:
        if scope is self._scope and (scope is Scope.GLOBAL or key == self.project_key):
            self._render()

    # ---- render ---------------------------------------------------------------------

    def _render(self) -> None:
        if self.brain is None:
            return
        self._stack.set_visible_child_name("brain")
        scope, key = self._scope, self.project_key
        self.brain.sync(scope, key)  # recoge ediciones hechas a mano en los archivos
        self.project_picker.set_visible(scope is Scope.PROJECT and len(self._project_ids) > 1)
        if scope is Scope.PROJECT:
            self.scope_label.set_label(f"Memoria del proyecto «{self._project.name}» · solo de este proyecto")
            self.banner.set_title("Los agentes solo reciben la ruta de INDEX.md y consultan esta memoria "
                                  "bajo demanda, sin cargarla entera.")
            self.banner.set_button_label("Copiar instrucción")
        else:
            self.scope_label.set_label("Memoria global · tus preferencias generales, comunes a todos los proyectos")
            self.banner.set_title("La memoria global solo guarda tus preferencias generales; "
                                  "no contiene nada de ningún proyecto.")
            self.banner.set_button_label(None)
        is_proposals = (scope is Scope.PROJECT and self.category_key == "__proposals__")
        self.search.set_visible(not is_proposals)
        self.new_button.set_visible(not is_proposals)
        self._render_categories(scope, key)
        w.clear_box(self.cards)
        if is_proposals:
            self._render_proposals(scope, key)
        elif self._query:
            self._render_search(scope, key)
        else:
            self._render_category(scope, key)

    def _render_categories(self, scope: Scope, key: str) -> None:
        counts = self.brain.counts(scope, key)
        w.clear_box(self._category_list)
        self._category_rows.clear()
        self._category_counts.clear()
        self._proposals_row = None
        for cat in categories_for(scope):
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image.new_from_icon_name(cat.icon))
            box.append(Gtk.Label(label=cat.label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            count = w.caption(str(counts.get(cat.key, 0)))
            box.append(count)
            row = Gtk.ListBoxRow(child=box)
            row.category_key = cat.key  # type: ignore[attr-defined]
            self._category_list.append(row)
            self._category_rows[cat.key] = row
            self._category_counts[cat.key] = count
        if scope is Scope.PROJECT and self._project is not None:
            pending_count = self.brain.count_pending_proposals(self._project.id)
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image.new_from_icon_name("document-send-symbolic"))
            box.append(Gtk.Label(label="Propuestas", xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            if pending_count > 0:
                count_w = w.status_pill(str(pending_count), "warning")
            else:
                count_w = w.caption("0")
            box.append(count_w)
            row = Gtk.ListBoxRow(child=box)
            row.category_key = "__proposals__"  # type: ignore[attr-defined]
            self._category_list.append(row)
            self._proposals_row = row
        if self.category_key == "__proposals__" and self._proposals_row is not None and not self._query:
            self._category_list.select_row(self._proposals_row)
        else:
            current = self._category_rows.get(self.category_key)
            if current is not None and not self._query:
                self._category_list.select_row(current)
            else:
                self._category_list.unselect_all()

    def _on_category_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._query:
            self.set_query("")
        self.select_category(row.category_key)  # type: ignore[attr-defined]
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _render_category(self, scope: Scope, key: str) -> None:
        cat = category(scope, self.category_key)
        if cat is None:
            return
        self.heading.set_label(cat.label)
        self.description.set_label(cat.description)
        docs = self.brain.list(scope, key, cat.key)
        for doc in docs:
            self.cards.append(self._card(doc, doc.summary))
        if not docs:
            add = Gtk.Button(label="Nueva entrada", halign=Gtk.Align.CENTER)
            add.add_css_class("pill")
            add.connect("clicked", lambda _b: self.request_new())
            status = Adw.StatusPage(icon_name=cat.icon, title="Sin documentos",
                                    description="Crea el primero; podrás dividir los temas en varios "
                                                "documentos breves.", child=add)
            status.add_css_class("compact")
            self.cards.append(status)

    def _render_search(self, scope: Scope, key: str) -> None:
        hits = self.brain.search(scope, key, self._query, limit=25)
        self.heading.set_label(f"Resultados de «{self._query}»")
        count = len(hits)
        self.description.set_label(
            f"{count} documento{'s' if count != 1 else ''} · se muestra un fragmento de cada uno; ábrelo para leerlo."
            if hits else "Ningún documento de esta memoria coincide con la búsqueda."
        )
        for hit in hits:
            self.cards.append(self._card(hit.meta, hit.excerpt, show_category=True))

    def _render_proposals(self, scope: Scope, key: str) -> None:
        if self._project is None:
            return
        self.heading.set_label("Propuestas de actualización")
        self.description.set_label(
            "Sugerencias de los agentes para actualizar o ampliar la memoria. "
            "Revisa, compara diferencias y aprueba o rechaza cada propuesta."
        )

        filter_bar = Gtk.Box(spacing=8, margin_bottom=6)
        filter_bar.add_css_class("linked")
        btn_pending = Gtk.ToggleButton(label="Pendientes", active=self._proposals_filter == "pending")
        btn_history = Gtk.ToggleButton(
            label="Historial", group=btn_pending, active=self._proposals_filter == "history"
        )

        def on_filter_changed(_b) -> None:
            new_filter = "history" if btn_history.get_active() else "pending"
            if new_filter != self._proposals_filter:
                self._proposals_filter = new_filter
                self._render()

        btn_pending.connect("toggled", on_filter_changed)
        btn_history.connect("toggled", on_filter_changed)
        filter_bar.append(btn_pending)
        filter_bar.append(btn_history)
        self.cards.append(filter_bar)

        all_proposals = self.brain.list_proposals(self._project.id)
        if self._proposals_filter == "pending":
            proposals = [p for p in all_proposals if p.status in (ProposalStatus.PENDING, ProposalStatus.CONFLICT)]
        else:
            proposals = [p for p in all_proposals if p.status in (ProposalStatus.APPROVED, ProposalStatus.REJECTED)]

        for prop in proposals:
            self.cards.append(self._proposal_card(prop))

        if not proposals:
            if self._proposals_filter == "pending":
                title = "Sin propuestas pendientes"
                desc = "Cuando un agente ejecute «iaw-brain propose», sus sugerencias aparecerán aquí para tu revisión."
            else:
                title = "Sin historial de propuestas"
                desc = "Las propuestas que apruebes o rechaces se conservarán aquí."
            status = Adw.StatusPage(icon_name="document-send-symbolic", title=title, description=desc)
            status.add_css_class("compact")
            self.cards.append(status)

    def _proposal_card(self, meta: ProposalMeta) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("brain-card")
        card.proposal_id = meta.id  # type: ignore[attr-defined]

        top = Gtk.Box(spacing=8)
        op_label = "Nuevo documento" if meta.operation is ProposalOperation.CREATE else "Actualización"
        top.append(w.status_pill(op_label, "idle"))

        status_style = "warning" if meta.status is ProposalStatus.PENDING else (
            "error" if meta.status is ProposalStatus.CONFLICT else (
                "success" if meta.status is ProposalStatus.APPROVED else "idle"
            )
        )
        top.append(w.status_pill(meta.status.label, status_style))

        from ia_workspace.ui.dialogs.proposal_review import _agent_display_name
        agent_name = _agent_display_name(meta.agent_kind)
        top.append(w.caption(f"Origen: {agent_name}"))

        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)

        if meta.status in (ProposalStatus.PENDING, ProposalStatus.CONFLICT):
            btn_review = Gtk.Button(label="Revisar")
            btn_review.connect("clicked", lambda _b: self.review_proposal(meta.id))
            top.append(btn_review)

            btn_approve = Gtk.Button(label="Aprobar")
            btn_approve.add_css_class("suggested-action")
            btn_approve.connect("clicked", lambda _b: self.approve_proposal(meta.id))
            top.append(btn_approve)

            btn_reject = Gtk.Button(label="Rechazar")
            btn_reject.add_css_class("destructive-action")
            btn_reject.connect("clicked", lambda _b: self.reject_proposal(meta.id))
            top.append(btn_reject)
        else:
            btn_review = Gtk.Button(label="Ver detalles")
            btn_review.connect("clicked", lambda _b: self.review_proposal(meta.id))
            top.append(btn_review)

        card.append(top)

        title = Gtk.Label(label=meta.title, xalign=0, wrap=True)
        title.add_css_class("heading")
        card.append(title)

        if meta.summary:
            summary = Gtk.Label(label=meta.summary, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            card.append(summary)

        details = [meta.relpath, w.relative_time(meta.created_at)]
        if meta.tags:
            details.append(", ".join(meta.tags))
        caption = w.caption(" · ".join(details))
        caption.set_wrap(True)
        caption.set_ellipsize(Pango.EllipsizeMode.NONE)
        card.append(caption)

        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.review_proposal(meta.id))
        card.add_controller(click)
        card.set_tooltip_text("Examinar propuesta y comparar cambios")
        return card

    def _card(self, meta: DocumentMeta, text: str, show_category: bool = False) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.add_css_class("brain-card")
        card.relpath = meta.relpath  # type: ignore[attr-defined]
        top = Gtk.Box(spacing=8)
        title = Gtk.Label(label=meta.title, xalign=0, hexpand=True, wrap=True)
        title.add_css_class("heading")
        top.append(title)
        if meta.module:
            top.append(w.status_pill(meta.module, "idle"))
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Editar", valign=Gtk.Align.START)
        edit.add_css_class("flat")
        edit.connect("clicked", lambda _b: self.request_edit(meta.relpath))
        top.append(edit)
        top.append(self._card_menu(meta))
        card.append(top)
        if text:
            excerpt = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            card.append(excerpt)
        details = [meta.relpath, w.relative_time(meta.updated)]
        if show_category and (cat := category(meta.scope, meta.category)) is not None:
            details.insert(0, cat.label)
        if meta.tags:
            details.append(", ".join(meta.tags))
        caption = w.caption(" · ".join(details))
        caption.set_wrap(True)
        caption.set_ellipsize(Pango.EllipsizeMode.NONE)
        card.append(caption)
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: self.open_document(meta.relpath))
        card.add_controller(click)
        card.set_tooltip_text("Abrir el documento")
        return card

    def _card_menu(self, meta: DocumentMeta) -> Gtk.MenuButton:
        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("open", lambda *_: self.open_document(meta.relpath)),
            ("copy", lambda *_: self._copy(meta.relpath, "Ruta copiada")),
            ("delete", lambda *_: self.request_delete(meta.relpath)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        menu = Gio.Menu()
        menu.append("Abrir", "doc.open")
        menu.append("Copiar ruta", "doc.copy")
        danger = Gio.Menu()
        danger.append("Eliminar…", "doc.delete")
        menu.append_section(None, danger)
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.START,
                                tooltip_text="Más opciones")
        button.add_css_class("flat")
        button.insert_action_group("doc", group)
        return button

    # ---- acciones -------------------------------------------------------------------

    def _scope_title(self) -> str:
        if self._scope is Scope.GLOBAL:
            return "Memoria global"
        return f"Memoria del proyecto «{self._project.name}»"

    def _copy(self, text: str, message: str) -> None:
        self.get_clipboard().set(text)
        self.nav.toast(message)

    def copy_agent_brief(self) -> str | None:
        if self._scope is not Scope.PROJECT or self._project is None:
            return None
        brief = self.brain.agent_brief(self._project, cli_command())
        self._copy(brief, "Instrucción para agentes copiada")
        return brief

    def open_document(self, relpath: str) -> None:
        from ia_workspace.ui.dialogs.brain_document import BrainViewerDialog

        try:
            doc = self.brain.read(self._scope, self.project_key, relpath)
        except BrainError as exc:
            self.nav.toast(str(exc))
            return
        dialog = BrainViewerDialog(title=doc.meta.title, body=doc.body, scope_label=self._scope_title(),
                                   location=doc.meta.relpath, problems=doc.problems,
                                   on_edit=lambda: self.request_edit(relpath))
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def request_new(self) -> None:
        from ia_workspace.ui.dialogs.brain_document import BrainEditorDialog

        if self.brain is None:
            return
        cat = category(self._scope, self.category_key)
        scope, key = self._scope, self.project_key

        def save(title: str, body: str, tags: str, module: str) -> None:
            self.brain.create(scope, key, cat.key, title, body, tags, module)
            self.nav.toast("Documento guardado")

        dialog = BrainEditorDialog(heading="Nueva entrada", scope_label=self._scope_title(), category=cat,
                                   on_save=save)
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def request_edit(self, relpath: str) -> None:
        from ia_workspace.ui.dialogs.brain_document import BrainEditorDialog

        scope, key = self._scope, self.project_key
        try:
            doc = self.brain.read(scope, key, relpath)
        except BrainError as exc:
            self.nav.toast(str(exc))
            return

        def save(title: str, body: str, tags: str, _module: str) -> None:
            self.brain.update(scope, key, relpath, title, body, tags)
            self.nav.toast("Documento guardado")

        dialog = BrainEditorDialog(heading="Editar documento", scope_label=self._scope_title(),
                                   category=category(scope, doc.meta.category), title=doc.meta.title,
                                   body=doc.body, tags=doc.meta.tags, module=doc.meta.module,
                                   module_editable=False, on_save=save)
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def request_delete(self, relpath: str) -> None:
        scope, key = self._scope, self.project_key
        meta = next((d for d in self.brain.list(scope, key) if d.relpath == relpath), None)
        if meta is None:
            return
        dialog = Adw.AlertDialog(
            heading=f"¿Eliminar «{meta.title}»?",
            body="Se moverá a la papelera de la memoria (carpeta .trash), de donde puedes recuperarlo. "
                 "Los agentes dejarán de encontrarlo.",
        )
        dialog.set_body_use_markup(False)
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                try:
                    self.brain.delete(scope, key, relpath)
                    self.nav.toast("Documento movido a la papelera de la memoria")
                except BrainError as exc:
                    self.nav.toast(str(exc))

        dialog.connect("response", on_response)
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def review_proposal(self, proposal_id: str) -> None:
        from ia_workspace.ui.dialogs.proposal_review import ProposalReviewDialog

        if self._project is None or self.brain is None:
            return
        try:
            content = self.brain.get_proposal(self._project, proposal_id)
        except BrainError as exc:
            self.nav.toast(str(exc))
            return

        orig_body = None
        if content.meta.operation is ProposalOperation.UPDATE:
            try:
                orig_doc = self.brain.read(Scope.PROJECT, content.meta.project_key, content.meta.relpath)
                orig_body = orig_doc.body
            except BrainError:
                orig_body = None

        def on_approve(pid: str) -> None:
            self.approve_proposal(pid)

        def on_reject(pid: str, note: str) -> None:
            self._do_reject_proposal(pid, note)

        dialog = ProposalReviewDialog(
            proposal=content,
            project_name=self._project.name,
            original_body=orig_body,
            on_approve=on_approve,
            on_reject=on_reject,
        )
        dialog.present(self.get_root())
        self._last_dialog = dialog

    def approve_proposal(self, proposal_id: str) -> None:
        if self._project is None or self.brain is None:
            return
        try:
            self.brain.approve_proposal(self._project, proposal_id)
            self.nav.toast("Propuesta aprobada e incorporada a la memoria")
        except BrainError as exc:
            self.nav.toast(str(exc))

    def reject_proposal(self, proposal_id: str) -> None:
        if self._project is None or self.brain is None:
            return
        alert = Adw.AlertDialog(
            heading="¿Rechazar propuesta?",
            body="La propuesta se marcará como rechazada y no se modificará la memoria del proyecto.",
        )
        alert.add_response("cancel", "Cancelar")
        alert.add_response("reject", "Rechazar")
        alert.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "reject":
                self._do_reject_proposal(proposal_id, "")

        alert.connect("response", on_response)
        alert.present(self.get_root())

    def _do_reject_proposal(self, proposal_id: str, note: str) -> None:
        if self._project is None or self.brain is None:
            return
        try:
            self.brain.reject_proposal(self._project, proposal_id, note=note)
            self.nav.toast("Propuesta rechazada")
        except BrainError as exc:
            self.nav.toast(str(exc))

