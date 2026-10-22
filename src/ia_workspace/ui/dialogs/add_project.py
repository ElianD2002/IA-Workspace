"""Diálogo «Agregar proyecto»."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ia_workspace.brain.model import BrainError
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

        # Memoria de un proyecto quitado que tenía esta misma carpeta: la decisión es explícita.
        self._memory_group = Adw.PreferencesGroup(
            title="Memoria anterior de esta carpeta",
            description="Esta carpeta perteneció a un proyecto que se quitó del registro. Su memoria del "
            "Cerebro se conservó: elige qué hacer. Nada se recupera sin tu elección.",
            visible=False,
        )
        page.add(self._memory_group)
        self._memory_rows: list[Gtk.Widget] = []
        self.recover_checks: dict[int, Gtk.CheckButton] = {}
        self.fresh_check: Gtk.CheckButton | None = None
        self._candidates: dict = {}
        self.memory_warning = Gtk.Label(wrap=True, visible=False, xalign=0, margin_start=12, margin_end=12,
                                        margin_top=10, margin_bottom=10)
        self.memory_warning.add_css_class("warning")
        self.confirm_row = Adw.SwitchRow(
            title="Confirmo que es la misma carpeta", subtitle="Necesario para recuperar una memoria con avisos",
            visible=False,
        )
        self.confirm_row.connect("notify::active", lambda *_: self._update_sensitivity())
        self._confirm_group = Adw.PreferencesGroup(visible=False)
        self._confirm_group.add(Adw.PreferencesRow(activatable=False, child=self.memory_warning))
        self._confirm_group.add(self.confirm_row)
        page.add(self._confirm_group)

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
        self._show_recoverable(self.ctx.projects.normalize_path(path))
        self._update_sensitivity()

    # ---- memoria anterior -----------------------------------------------------------

    def _show_recoverable(self, path: str) -> None:
        for row in self._memory_rows:
            self._memory_group.remove(row)
        self._memory_rows, self.recover_checks, self.fresh_check = [], {}, None
        brain = self.ctx.brain
        candidates = brain.recoverable_memories(path) if brain is not None else []
        self._candidates = {m.id: m for m in candidates}
        self.confirm_row.set_active(False)
        if not candidates:
            self._memory_group.set_visible(False)
            self._confirm_group.set_visible(False)
            return
        group_leader = None
        for memory in candidates:
            retired = memory.retired
            date = retired.retired_at.astimezone().strftime("%d/%m/%Y") if retired.retired_at else "fecha desconocida"
            plural = "documento" if memory.documents == 1 else "documentos"
            title = "Recuperar memoria anterior" if len(candidates) == 1 else f"Recuperar la memoria de «{retired.project_name}»"
            check = Gtk.CheckButton(group=group_leader, valign=Gtk.Align.CENTER)
            group_leader = group_leader or check
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(title),
                subtitle=GLib.markup_escape_text(
                    f"Proyecto «{retired.project_name}» · {memory.documents} {plural} · quitado el {date}"
                    + (" · requiere confirmación" if memory.warnings else "")
                ),
                activatable_widget=check,
                subtitle_lines=2,
            )
            row.add_prefix(check)
            check.connect("toggled", lambda *_: self._on_memory_choice())
            self.recover_checks[memory.id] = check
            self._memory_rows.append(row)
        self.fresh_check = Gtk.CheckButton(group=group_leader, valign=Gtk.Align.CENTER)
        self.fresh_check.connect("toggled", lambda *_: self._on_memory_choice())
        fresh = Adw.ActionRow(
            title="Comenzar con memoria nueva",
            subtitle="La memoria anterior se conserva intacta y podrá recuperarse más adelante.",
            activatable_widget=self.fresh_check,
        )
        fresh.add_prefix(self.fresh_check)
        self._memory_rows.append(fresh)
        for row in self._memory_rows:
            self._memory_group.add(row)
        self._on_memory_choice()
        self._memory_group.set_visible(True)

    def _chosen_memory(self):
        return next((self._candidates[i] for i, c in self.recover_checks.items() if c.get_active()), None)

    def _on_memory_choice(self) -> None:
        chosen = self._chosen_memory()
        warnings = chosen.warnings if chosen is not None else ()
        self.memory_warning.set_label("\n".join(warnings))
        self.memory_warning.set_visible(bool(warnings))
        self.confirm_row.set_visible(bool(warnings))
        self._confirm_group.set_visible(bool(warnings))
        if not warnings:
            self.confirm_row.set_active(False)
        self._update_sensitivity()

    def _on_name_changed(self, _entry: Adw.EntryRow) -> None:
        if self._path and self._name.get_text() != Path(self._path).name:
            self._name_edited = True
        self._update_sensitivity()

    def _memory_decided(self) -> bool:
        if not self._candidates:
            return True
        chosen = self._chosen_memory()
        if chosen is None:
            return self.fresh_check is not None and self.fresh_check.get_active()
        return not chosen.warnings or self.confirm_row.get_active()

    def _update_sensitivity(self) -> None:
        self._add.set_sensitive(bool(self._path and self._name.get_text().strip() and self._memory_decided()))

    # ---- registro ----------------------------------------------------------------

    def _submit(self) -> None:
        if not self._add.get_sensitive():
            return
        agents = tuple(kind for kind, row in self._agent_rows.items() if row.get_active())
        chosen, prepare = self._chosen_memory(), None
        if chosen is not None and self.ctx.brain is not None:
            confirmed = self.confirm_row.get_active()
            brain = self.ctx.brain

            def prepare(project) -> None:
                brain.recover_memory(project, chosen.id, confirmed)

        try:
            project = self.ctx.projects.register(self._name.get_text(), self._path, agents, prepare=prepare)
        except (ProjectValidationError, BrainError) as exc:
            self._error.set_label(str(exc))
            self._error.set_visible(True)
            return
        self.close()
        self._on_added(project.id)
