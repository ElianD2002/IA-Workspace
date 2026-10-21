"""Diálogo «Agregar proyecto»."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ia_workspace.context import AppContext
from ia_workspace.domain.models import SELECTABLE_AGENTS
from ia_workspace.services.detection import inspect_folder
from ia_workspace.services.integrations import detect_tools
from ia_workspace.services.projects import ProjectValidationError
from ia_workspace.ui import widgets as w


class AddProjectDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, on_added: Callable[[int], None]) -> None:
        super().__init__(title="Agregar proyecto", content_width=520, content_height=640)
        self.ctx = ctx
        self._on_added = on_added
        self._path = ""
        self._name_edited = False

        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda _b: self.close())
        self._add = Gtk.Button(label="Agregar", sensitive=False)
        self._add.add_css_class("suggested-action")
        self._add.connect("clicked", lambda _b: self._submit())
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self._add)

        page = Adw.PreferencesPage()

        folder_group = Adw.PreferencesGroup(title="Carpeta")
        self._folder_row = Adw.ActionRow(title="Ninguna carpeta elegida", subtitle="Elige la raíz del proyecto")
        self._folder_row.add_css_class("property")
        choose = Gtk.Button(label="Elegir carpeta…", valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda _b: self._choose_folder())
        self._folder_row.add_suffix(choose)
        folder_group.add(self._folder_row)
        page.add(folder_group)

        name_group = Adw.PreferencesGroup()
        self._name = Adw.EntryRow(title="Nombre", activates_default=True)
        self._name.connect("changed", self._on_name_changed)
        name_group.add(self._name)
        page.add(name_group)

        self._detected = Adw.PreferencesGroup(title="Detectado en la carpeta", visible=False)
        self._git_row = Adw.ActionRow(title="Repositorio Git")
        self._stack_row = Adw.ActionRow(title="Tecnologías")
        self._ctx_row = Adw.ActionRow(title="Archivos de contexto")
        for row in (self._git_row, self._stack_row, self._ctx_row):
            row.add_css_class("property")
            self._detected.add(row)
        page.add(self._detected)

        agents_group = Adw.PreferencesGroup(
            title="Agentes disponibles para este proyecto",
            description="Cada herramienta usa su propio inicio de sesión y tu suscripción actual.",
        )
        installed = {s.kind: s.installed for s in detect_tools()}
        self._agent_rows: dict = {}
        for kind in SELECTABLE_AGENTS:
            row = Adw.SwitchRow(
                title=kind.label,
                subtitle="Instalado" if installed.get(kind) else "No detectado en este equipo",
                active=True,
            )
            row.add_prefix(w.tool_chip(kind))
            self._agent_rows[kind] = row
            agents_group.add(row)
        page.add(agents_group)

        self._error = Gtk.Label(wrap=True, visible=False, xalign=0, margin_start=12, margin_end=12)
        self._error.add_css_class("error")
        error_group = Adw.PreferencesGroup()
        error_group.add(self._error)
        page.add(error_group)

        note_group = Adw.PreferencesGroup(
            description="Registrar un proyecto no copia, mueve ni modifica archivos de la carpeta."
        )
        page.add(note_group)

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.set_default_widget(self._add)

    # ---- carpeta ----------------------------------------------------------------

    def _choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Elegir la carpeta del proyecto", modal=True)
        default = Path.home() / "Proyectos"
        if default.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(default)))
        dialog.select_folder(self.get_root(), None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelado por el usuario
        path = folder.get_path() if folder is not None else None
        if path:
            self.set_folder(path)

    def set_folder(self, path: str) -> None:
        self._path = path
        self._folder_row.set_title(GLib.markup_escape_text(path))
        self._folder_row.set_subtitle("Carpeta elegida")
        if not self._name_edited or not self._name.get_text().strip():
            self._name.set_text(Path(path).name)
            self._name_edited = False
        info = inspect_folder(path)
        self._git_row.set_subtitle(f"Rama {info.branch}" if info.branch else ("Sí" if info.is_git else "No"))
        self._stack_row.set_subtitle(info.stack_label or "No detectado")
        self._ctx_row.set_subtitle(
            ", ".join(info.context_files) + " · se importará a la memoria en una próxima fase"
            if info.context_files
            else "Ninguno (AGENTS.md, CLAUDE.md, GEMINI.md)"
        )
        self._detected.set_visible(True)
        self._error.set_visible(False)
        self._update_sensitivity()

    def _on_name_changed(self, _entry: Adw.EntryRow) -> None:
        if self._path and self._name.get_text() != Path(self._path).name:
            self._name_edited = True
        self._update_sensitivity()

    def _update_sensitivity(self) -> None:
        self._add.set_sensitive(bool(self._path and self._name.get_text().strip()))

    # ---- registro ----------------------------------------------------------------

    def _submit(self) -> None:
        agents = tuple(kind for kind, row in self._agent_rows.items() if row.get_active())
        try:
            project = self.ctx.projects.register(self._name.get_text(), self._path, agents)
        except ProjectValidationError as exc:
            self._error.set_label(str(exc))
            self._error.set_visible(True)
            return
        self.close()
        self._on_added(project.id)
