"""Diálogo selector de la biblioteca de plantillas de prompts para el workspace (fase 6B.2)."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk, Pango

from ia_workspace.context import AppContext
from ia_workspace.domain.models import Project, PromptTemplate, PromptTemplateScope
from ia_workspace.ui import widgets as w
from ia_workspace.ui.dialogs.fill_variables import FillVariablesDialog
from ia_workspace.ui.dialogs.template_editor import TemplateEditorDialog


class PromptLibraryDialog(Adw.Dialog):
    """Permite buscar, previsualizar y seleccionar una plantilla para insertarla en el editor de prompts."""

    def __init__(
        self,
        *,
        ctx: AppContext,
        project: Project,
        on_select_prompt: Callable[[str], None],
    ) -> None:
        super().__init__(
            title="Biblioteca de prompts",
            content_width=660,
            content_height=620,
        )
        self.ctx = ctx
        self.project = project
        self._on_select_prompt = on_select_prompt

        self._templates: list[PromptTemplate] = []
        self._selected_template: PromptTemplate | None = None
        self._filter_category: str = "all"
        self._search_text: str = ""

        # Cabecera
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        close_btn = Gtk.Button(label="Cerrar")
        close_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(close_btn)

        new_btn = Gtk.Button(label="Nueva plantilla", icon_name="list-add-symbolic")
        new_btn.connect("clicked", lambda _b: self._open_new_template())
        header.pack_end(new_btn)

        # Contenedor dividido: Lista arriba / izquierda y Vista previa abajo / derecha
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10, margin_bottom=10, margin_start=14, margin_end=14)

        # Barra de filtros y búsqueda
        filter_bar = Gtk.Box(spacing=10)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar plantilla por nombre o contenido…", hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        filter_bar.append(self.search_entry)

        self.category_dropdown = Gtk.DropDown()
        self.category_dropdown.connect("notify::selected", self._on_category_changed)
        filter_bar.append(self.category_dropdown)

        content_box.append(filter_bar)

        # Lista de plantillas
        self.templates_list = w.boxed_list()
        list_scroll = Gtk.ScrolledWindow(
            child=self.templates_list,
            min_content_height=180,
            max_content_height=240,
            vexpand=False,
        )
        content_box.append(list_scroll)

        # Panel de vista previa y detalles
        preview_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        preview_frame.add_css_class("task-detail-section")

        preview_header = Gtk.Box(spacing=8)
        self.preview_title = Gtk.Label(label="Selecciona una plantilla para ver su contenido", xalign=0, hexpand=True)
        self.preview_title.add_css_class("heading")
        self.preview_scope_chip = w.chip("Global", "tag-chip")
        self.preview_scope_chip.set_visible(False)
        preview_header.append(self.preview_title)
        preview_header.append(self.preview_scope_chip)
        preview_frame.append(preview_header)

        self.preview_desc = Gtk.Label(xalign=0, wrap=True)
        self.preview_desc.add_css_class("dim-label")
        self.preview_desc.set_visible(False)
        preview_frame.append(self.preview_desc)

        # Vista de texto del contenido
        self.preview_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            editable=False,
            cursor_visible=False,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
        )
        self.preview_view.add_css_class("prompt-view")
        preview_scroller = Gtk.ScrolledWindow(
            child=self.preview_view,
            min_content_height=120,
            max_content_height=160,
            vexpand=True,
        )
        preview_scroller.add_css_class("task-prompt-box")
        preview_frame.append(preview_scroller)

        # Barra de variables detectadas y botón de acción
        bottom_bar = Gtk.Box(spacing=10, margin_top=4)
        self.vars_label = Gtk.Label(xalign=0, hexpand=True)
        self.vars_label.add_css_class("dim-label")
        bottom_bar.append(self.vars_label)

        self.use_btn = Gtk.Button(label="Insertar en el editor", icon_name="go-down-symbolic")
        self.use_btn.add_css_class("suggested-action")
        self.use_btn.set_sensitive(False)
        self.use_btn.connect("clicked", lambda _b: self._use_selected())
        bottom_bar.append(self.use_btn)

        preview_frame.append(bottom_bar)
        content_box.append(preview_frame)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)
        main_box.append(content_box)
        self.set_child(main_box)

        # Suscripción reactiva para auto-refresco
        self._unsub = self.ctx.prompt_templates.subscribe(self._reload_templates)
        self.connect("closed", lambda _d: self._cleanup())

        self._load_categories()
        self._reload_templates()

    def _cleanup(self) -> None:
        if hasattr(self, "_unsub") and self._unsub:
            self._unsub()
            self._unsub = None

    def _load_categories(self) -> None:
        categories = ["Todas las categorías"] + self.ctx.prompt_templates.categories(self.project.id)
        model = Gtk.StringList.new(categories)
        self.category_dropdown.set_model(model)
        self.category_dropdown.set_selected(0)

    def _on_category_changed(self, dropdown: Gtk.DropDown, _param: object) -> None:
        idx = dropdown.get_selected()
        model = dropdown.get_model()
        if model and idx >= 0:
            selected = model.get_string(idx)
            self._filter_category = "all" if idx == 0 else selected
            self._reload_templates()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip()
        self._reload_templates()

    def _reload_templates(self) -> None:
        cat = None if self._filter_category == "all" else self._filter_category
        self._templates = self.ctx.prompt_templates.list_for_project(
            self.project.id,
            category=cat,
            search=self._search_text,
        )
        self._render_templates_list()

    def _render_templates_list(self) -> None:
        w.clear_box(self.templates_list)

        if not self._templates:
            row = Adw.ActionRow(
                title="No se encontraron plantillas",
                subtitle="Crea una nueva plantilla o modifica los filtros de búsqueda.",
            )
            self.templates_list.append(row)
            self._select_template(None)
            return

        for t in self._templates:
            row = Adw.ActionRow(
                title=t.name,
                subtitle=f"{t.category} · {w.relative_time(t.updated_at or t.created_at)}",
                activatable=True,
            )
            # Chip de ámbito
            if t.scope == PromptTemplateScope.GLOBAL:
                scope_chip = w.chip("Global", "tag-chip")
            else:
                scope_chip = w.chip("Proyecto", "tag-chip")
            row.add_suffix(scope_chip)

            row.connect("activated", lambda _r, tmpl=t: self._select_template(tmpl))
            self.templates_list.append(row)

        # Seleccionar la primera por defecto si no hay selección
        if self._selected_template is None or self._selected_template.id not in [x.id for x in self._templates]:
            self._select_template(self._templates[0])
        else:
            # Re-seleccionar para actualizar
            current = next((x for x in self._templates if x.id == self._selected_template.id), None)
            self._select_template(current)

    def _select_template(self, template: PromptTemplate | None) -> None:
        self._selected_template = template
        if template is None:
            self.preview_title.set_text("Sin plantilla seleccionada")
            self.preview_desc.set_visible(False)
            self.preview_scope_chip.set_visible(False)
            self.preview_view.get_buffer().set_text("")
            self.vars_label.set_text("")
            self.use_btn.set_sensitive(False)
            return

        self.preview_title.set_text(template.name)
        if template.description:
            self.preview_desc.set_text(template.description)
            self.preview_desc.set_visible(True)
        else:
            self.preview_desc.set_visible(False)

        is_global = template.scope == PromptTemplateScope.GLOBAL
        self.preview_scope_chip.set_text("Global" if is_global else "Proyecto")
        self.preview_scope_chip.set_visible(True)

        self.preview_view.get_buffer().set_text(template.content)

        variables = template.variables()
        if variables:
            self.vars_label.set_text(f"Variables requeridas: {', '.join(f'{{{{{v}}}}}' for v in variables)}")
        else:
            self.vars_label.set_text("Plantilla sin variables (texto directo)")

        self.use_btn.set_sensitive(True)

    def _use_selected(self) -> None:
        if self._selected_template is None:
            return

        template = self._selected_template
        variables = template.variables()

        if variables:
            # Abrir diálogo para completar variables
            dialog = FillVariablesDialog(
                template=template,
                on_complete=self._complete_insertion,
            )
            dialog.present(self.get_root())
        else:
            self._complete_insertion(template.content)

    def _complete_insertion(self, content: str) -> None:
        self._on_select_prompt(content)
        if self.get_root() is not None:
            self.close()

    def _open_new_template(self) -> None:
        dialog = TemplateEditorDialog(
            ctx=self.ctx,
            project=self.project,
            on_saved=lambda tmpl: self._on_template_saved(tmpl),
        )
        dialog.present(self.get_root())

    def _on_template_saved(self, template: PromptTemplate) -> None:
        self._load_categories()
        self._reload_templates()
        self._select_template(template)
