"""Diálogo para solicitar valores de variables en plantillas de prompts (fase 6B.2)."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import PromptTemplate, render_template_content


class FillVariablesDialog(Adw.Dialog):
    """Solicita valores para las variables {{variable}} y muestra el resultado en tiempo real."""

    def __init__(
        self,
        *,
        template: PromptTemplate,
        on_complete: Callable[[str], None],
    ) -> None:
        super().__init__(
            title=f"Completar variables: {template.name}",
            content_width=560,
            content_height=520,
        )
        self.template = template
        self.variables = template.variables()
        self._on_complete = on_complete
        self._entries: dict[str, Gtk.Entry] = {}

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        self.insert_btn = Gtk.Button(label="Insertar prompt")
        self.insert_btn.add_css_class("suggested-action")
        self.insert_btn.connect("clicked", lambda _b: self._apply())
        header.pack_end(self.insert_btn)

        page = Adw.PreferencesPage()

        # Grupo con las variables
        var_group = Adw.PreferencesGroup(
            title="Variables de la plantilla",
            description="Completa cada marcador para generar el prompt final.",
        )

        for var_name in self.variables:
            entry = Gtk.Entry(hexpand=True)
            entry.set_placeholder_text(f"Valor para {{{{{var_name}}}}}")
            entry.connect("changed", lambda _e: self._update_preview())
            self._entries[var_name] = entry

            row = Adw.ActionRow(title=f"{{{{{var_name}}}}}")
            row.add_suffix(entry)
            var_group.add(row)

        page.add(var_group)

        # Grupo: Vista previa del resultado
        preview_group = Adw.PreferencesGroup(title="Vista previa del resultado")
        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self.preview_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            editable=False,
            cursor_visible=False,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
        )
        self.preview_view.add_css_class("prompt-view")

        scroller = Gtk.ScrolledWindow(
            child=self.preview_view,
            min_content_height=140,
            max_content_height=200,
        )
        scroller.add_css_class("task-prompt-box")
        preview_box.append(scroller)

        self.error_label = Gtk.Label(xalign=0, wrap=True)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)
        preview_box.append(self.error_label)

        preview_row = Adw.PreferencesRow(child=preview_box)
        preview_group.add(preview_row)
        page.add(preview_group)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)
        main_box.append(page)
        self.set_child(main_box)

        self._update_preview()

    def _get_values(self) -> dict[str, str]:
        return {var: entry.get_text().strip() for var, entry in self._entries.items()}

    def _update_preview(self) -> None:
        values = self._get_values()
        rendered = render_template_content(self.template.content, values)
        buf = self.preview_view.get_buffer()
        buf.set_text(rendered)

        # Verificar si hay variables pendientes
        missing = [var for var, val in values.items() if not val]
        if missing:
            self.error_label.set_text(f"Variables pendientes: {', '.join(f'{{{{{m}}}}}' for m in missing)}")
            self.error_label.set_visible(True)
            self.insert_btn.set_sensitive(False)
        else:
            self.error_label.set_visible(False)
            self.insert_btn.set_sensitive(True)

    def _apply(self) -> None:
        values = self._get_values()
        missing = [var for var, val in values.items() if not val]
        if missing:
            self.error_label.set_text("Por favor completa todas las variables requeridas.")
            self.error_label.set_visible(True)
            return

        rendered = render_template_content(self.template.content, values)
        self._on_complete(rendered)
        if self.get_root() is not None:
            self.close()
