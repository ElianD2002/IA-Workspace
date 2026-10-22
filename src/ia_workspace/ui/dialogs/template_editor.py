"""Diálogo para crear y editar plantillas de prompts reutilizables (fase 6B.2)."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ia_workspace.context import AppContext
from ia_workspace.domain.models import Project, PromptTemplate, PromptTemplateScope


class TemplateEditorDialog(Adw.Dialog):
    """Permite crear o editar una plantilla de prompt con ámbito y soporte de variables."""

    def __init__(
        self,
        *,
        ctx: AppContext,
        template: PromptTemplate | None = None,
        project: Project | None = None,
        initial_content: str = "",
        on_saved: Callable[[PromptTemplate], None] | None = None,
    ) -> None:
        is_edit = template is not None
        title = "Editar plantilla" if is_edit else "Nueva plantilla de prompt"
        super().__init__(title=title, content_width=580, content_height=600)

        self.ctx = ctx
        self.template = template
        self.project = project
        self._on_saved = on_saved

        # Cabecera
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label="Guardar")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", lambda _b: self._save())
        header.pack_end(save_btn)

        page = Adw.PreferencesPage()

        # Grupo: Metadatos
        meta_group = Adw.PreferencesGroup(title="Información de la plantilla")

        # Nombre
        self.name_entry = Gtk.Entry(hexpand=True)
        self.name_entry.set_placeholder_text("Ej. Refactorización modular de componente")
        if template:
            self.name_entry.set_text(template.name)
        name_row = Adw.ActionRow(title="Nombre", subtitle="Identificador descriptivo de la plantilla")
        name_row.add_suffix(self.name_entry)
        meta_group.add(name_row)

        # Categoría
        self.category_entry = Gtk.Entry(hexpand=True)
        self.category_entry.set_placeholder_text("Ej. Refactorización, Pruebas, Documentación")
        default_cat = template.category if template else "General"
        self.category_entry.set_text(default_cat)
        cat_row = Adw.ActionRow(title="Categoría", subtitle="Etiqueta para agrupar y filtrar")
        cat_row.add_suffix(self.category_entry)
        meta_group.add(cat_row)

        # Descripción
        self.desc_entry = Gtk.Entry(hexpand=True)
        self.desc_entry.set_placeholder_text("Breve explicación del objetivo o uso recomendado")
        if template:
            self.desc_entry.set_text(template.description)
        desc_row = Adw.ActionRow(title="Descripción", subtitle="Opcional")
        desc_row.add_suffix(self.desc_entry)
        meta_group.add(desc_row)

        # Ámbito
        self.scope_toggle = Adw.ToggleGroup()
        self.scope_toggle.add(Adw.Toggle(name="global", label="Global (todos los proyectos)"))
        proj_label = f"Proyecto ({project.name})" if project else "Proyecto"
        self.scope_toggle.add(Adw.Toggle(name="project", label=proj_label))

        # Determinar ámbito inicial
        if template:
            active_scope = "project" if template.scope == PromptTemplateScope.PROJECT else "global"
        elif project:
            active_scope = "project"
        else:
            active_scope = "global"
            self.scope_toggle.set_sensitive(False)

        self.scope_toggle.set_active_name(active_scope)
        scope_row = Adw.ActionRow(
            title="Ámbito",
            subtitle="Las plantillas de proyecto están aisladas y no se comparten con otros proyectos",
        )
        scope_row.add_suffix(self.scope_toggle)
        meta_group.add(scope_row)

        page.add(meta_group)

        # Grupo: Contenido del prompt
        content_group = Adw.PreferencesGroup(
            title="Contenido del prompt",
            description="Puedes incluir variables dinámicas usando la sintaxis {{nombre_variable}}.",
        )

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.text_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
        )
        self.text_view.add_css_class("prompt-view")
        buf = self.text_view.get_buffer()
        initial_text = template.content if template else initial_content
        buf.set_text(initial_text)

        scroller = Gtk.ScrolledWindow(
            child=self.text_view,
            min_content_height=180,
            max_content_height=300,
        )
        scroller.add_css_class("task-prompt-box")
        content_box.append(scroller)

        self.error_label = Gtk.Label(xalign=0, wrap=True)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)
        content_box.append(self.error_label)

        content_row = Adw.PreferencesRow(child=content_box)
        content_group.add(content_row)
        page.add(content_group)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)
        main_box.append(page)
        self.set_child(main_box)

    def _save(self) -> None:
        name = self.name_entry.get_text().strip()
        if not name:
            self._show_error("El nombre de la plantilla es obligatorio.")
            return

        buf = self.text_view.get_buffer()
        start, end = buf.get_bounds()
        content = buf.get_text(start, end, True).strip()
        if not content:
            self._show_error("El contenido del prompt no puede estar vacío.")
            return

        category = self.category_entry.get_text().strip() or "General"
        description = self.desc_entry.get_text().strip()

        scope_name = self.scope_toggle.get_active_name() or "global"
        if scope_name == "project":
            scope = PromptTemplateScope.PROJECT
            proj_id = self.template.project_id if self.template and self.template.project_id else (self.project.id if self.project else None)
            if proj_id is None:
                self._show_error("Para crear una plantilla de proyecto se requiere un proyecto activo.")
                return
        else:
            scope = PromptTemplateScope.GLOBAL
            proj_id = None

        if self.template is not None:
            saved = self.ctx.prompt_templates.update(
                template_id=self.template.id,
                name=name,
                content=content,
                category=category,
                description=description,
                scope=scope,
                project_id=proj_id,
            )
        else:
            saved = self.ctx.prompt_templates.create(
                name=name,
                content=content,
                category=category,
                description=description,
                scope=scope,
                project_id=proj_id,
            )

        if saved is not None:
            if self._on_saved:
                self._on_saved(saved)
            if self.get_root() is not None:
                self.close()

    def _show_error(self, message: str) -> None:
        self.error_label.set_text(message)
        self.error_label.set_visible(True)
