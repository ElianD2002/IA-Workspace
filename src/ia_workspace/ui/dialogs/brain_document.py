"""Diálogos del Cerebro compartido: ver y editar un documento Markdown."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk, Pango

from ia_workspace.brain.model import BrainError, Category
from ia_workspace.ui import widgets as w
from ia_workspace.ui.markdown_view import render_markdown


def _scroller(child: Gtk.Widget, min_height: int) -> Gtk.ScrolledWindow:
    scroller = Gtk.ScrolledWindow(child=child, min_content_height=min_height, vexpand=True,
                                  hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.add_css_class("card")
    return scroller


class BrainViewerDialog(Adw.Dialog):
    """Muestra un documento ya renderizado. ``scope_label`` deja claro de qué memoria es."""

    def __init__(self, *, title: str, body: str, scope_label: str, location: str, problems: tuple[str, ...],
                 on_edit: Callable[[], None]) -> None:
        super().__init__(title=title, content_width=640, content_height=620)
        header = Adw.HeaderBar()
        edit = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Editar")
        edit.connect("clicked", lambda _b: (self.close(), on_edit()))
        header.pack_end(edit)
        self.edit_button = edit

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=18, margin_end=18,
                      margin_top=6, margin_bottom=18)
        meta = w.caption(f"{scope_label} · {location}", mono=False)
        meta.set_wrap(True)
        meta.set_ellipsize(Pango.EllipsizeMode.NONE)
        box.append(meta)
        if problems:
            notice = Adw.Banner(title="Avisos del archivo: " + "; ".join(problems), revealed=True)
            box.append(notice)
        self.view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=14, right_margin=14)
        render_markdown(self.view.get_buffer(), body)
        box.append(_scroller(self.view, 360))
        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

    def rendered_text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


class BrainEditorDialog(Adw.Dialog):
    """Crear o editar un documento. ``on_save`` puede lanzar ``BrainError``: se muestra aquí."""

    def __init__(self, *, heading: str, scope_label: str, category: Category, title: str = "", body: str = "",
                 tags: tuple[str, ...] = (), module: str = "", module_editable: bool = True,
                 on_save: Callable[[str, str, str, str], None]) -> None:
        super().__init__(title=heading, content_width=680, content_height=680)
        self._on_save = on_save
        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda _b: self.close())
        self.save_button = Gtk.Button(label="Guardar")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda _b: self.save())
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self.save_button)

        page = Adw.PreferencesPage()
        info = Adw.PreferencesGroup(description=f"{scope_label} · {category.label}. {category.description}")
        page.add(info)
        fields = Adw.PreferencesGroup()
        self.title_row = Adw.EntryRow(title="Título", text=title)
        self.title_row.connect("changed", lambda _e: self._update())
        fields.add(self.title_row)
        self.module_row = Adw.EntryRow(title="Módulo", text=module, visible=category.modular,
                                       sensitive=module_editable)
        fields.add(self.module_row)
        self.tags_row = Adw.EntryRow(title="Etiquetas (separadas por comas)", text=", ".join(tags))
        fields.add(self.tags_row)
        page.add(fields)

        content = Adw.PreferencesGroup(
            title="Contenido (Markdown)",
            description="Breve y concreto: los agentes leerán este documento solo cuando lo necesiten. "
                        "No incluyas contraseñas, claves ni tokens.",
        )
        self.body_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, top_margin=10,
                                      bottom_margin=10, left_margin=12, right_margin=12)
        self.body_view.get_buffer().set_text(body)
        content.add(_scroller(self.body_view, 280))
        page.add(content)

        self.error = Gtk.Label(wrap=True, visible=False, xalign=0, margin_start=12, margin_end=12)
        self.error.add_css_class("error")
        errors = Adw.PreferencesGroup()
        errors.add(self.error)
        page.add(errors)

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self._update()

    def body(self) -> str:
        buffer = self.body_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _update(self) -> None:
        self.save_button.set_sensitive(bool(self.title_row.get_text().strip()))

    def save(self) -> bool:
        try:
            self._on_save(self.title_row.get_text(), self.body(), self.tags_row.get_text(), self.module_row.get_text())
        except BrainError as exc:  # el texto se conserva: el usuario puede corregir y reintentar
            self.error.set_label(str(exc))
            self.error.set_visible(True)
            return False
        self.close()
        return True
