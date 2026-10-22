"""Diálogo nativo flotante para consultar detalles y gestionar tareas vinculadas (fase 6B.1)."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gdk, Gtk, Pango

from ia_workspace.context import AppContext
from ia_workspace.domain.models import Project, Task, TaskState
from ia_workspace.ui import widgets as w


class TaskDetailDialog(Adw.Dialog):
    """Permite examinar el prompt original, sesión asociada, notas, historial y transiciones de una tarea."""

    def __init__(
        self,
        *,
        task: Task,
        ctx: AppContext,
        project: Project | None = None,
        on_open_session: Callable[[str], None] | None = None,
        on_toast: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            title="Detalle de tarea",
            content_width=640,
            content_height=640,
        )
        self.task = task
        self.ctx = ctx
        self.project = project
        self._on_open_session = on_open_session
        self._on_toast = on_toast

        # Cabecera
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        close_btn = Gtk.Button(label="Cerrar")
        close_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(close_btn)

        self.complete_btn = Gtk.Button(label="Marcar completada", icon_name="emblem-ok-symbolic")
        self.complete_btn.add_css_class("suggested-action")
        self.complete_btn.connect("clicked", lambda _b: self._mark_completed())
        self.complete_btn.set_visible(self.task.status != TaskState.COMPLETED)
        header.pack_end(self.complete_btn)

        page = Adw.PreferencesPage()

        # 1. Resumen principal e identificación
        info_group = Adw.PreferencesGroup()
        proj_name = self.project.name if self.project else "Proyecto"
        sub = f"{proj_name} · {self.task.agent.label} · Creada {w.relative_time(self.task.created_at)}"

        summary_row = Adw.ActionRow(
            title=self.task.title,
            subtitle=sub,
            title_lines=0,
        )
        summary_row.add_prefix(w.tool_chip(self.task.agent))

        self._status_pill_holder = Gtk.Box(valign=Gtk.Align.CENTER)
        self._status_pill = w.status_pill(self.task.status.label, self.task.status.style)
        self._status_pill_holder.append(self._status_pill)
        summary_row.add_suffix(self._status_pill_holder)
        info_group.add(summary_row)
        page.add(info_group)

        # 2. Sesión asociada
        session_group = Adw.PreferencesGroup(title="Sesión asociada")
        live_session = self.ctx.sessions.get(self.task.session_id) if self.task.session_id else None

        if live_session is not None:
            is_live_term = w.is_live_terminal(self.ctx.sessions, live_session.id)
            status_text = w.session_label(live_session.state, is_live_term)
            sess_row = Adw.ActionRow(
                title=f"Sesión activa: {live_session.id}",
                subtitle=f"Estado del proceso: {status_text}",
            )
            sess_row.add_prefix(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
            sess_row.add_suffix(w.session_pill(live_session.state, is_live_term))

            open_btn = Gtk.Button(label="Ir a la terminal", icon_name="utilities-terminal-symbolic")
            open_btn.add_css_class("flat")
            open_btn.connect("clicked", lambda _b: self._open_terminal(live_session.id))
            sess_row.add_suffix(open_btn)
            session_group.add(sess_row)
        elif self.task.session_id:
            sess_row = Adw.ActionRow(
                title=f"Sesión histórica: {self.task.session_id}",
                subtitle="El proceso de esta terminal ha finalizado",
            )
            sess_row.add_prefix(Gtk.Image.new_from_icon_name("process-completed-symbolic"))
            sess_row.add_suffix(w.status_pill("Finalizada", "idle"))
            session_group.add(sess_row)
        else:
            sess_row = Adw.ActionRow(
                title="Sin sesión asignada",
                subtitle="Esta tarea no tiene una terminal asociada",
            )
            sess_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            session_group.add(sess_row)

        page.add(session_group)

        # 3. Prompt original transferido
        prompt_group = Adw.PreferencesGroup(title="Prompt original transferido")
        copy_row = Adw.ActionRow(
            title="Instrucción transferida al agente",
            subtitle="Texto completo del prompt enviado a la CLI",
        )
        copy_btn = Gtk.Button(label="Copiar", icon_name="edit-copy-symbolic", tooltip_text="Copiar prompt al portapapeles")
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", lambda _b: self._copy_prompt())
        copy_row.add_suffix(copy_btn)
        prompt_group.add(copy_row)

        prompt_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            monospace=True,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
        )
        prompt_view.add_css_class("prompt-view")
        prompt_view.get_buffer().set_text(self.task.prompt or "(Sin contenido de prompt)")

        scroller = Gtk.ScrolledWindow(
            child=prompt_view,
            min_content_height=100,
            max_content_height=200,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroller.add_css_class("card")
        prompt_group.add(scroller)
        page.add(prompt_group)

        # 4. Campo de notas o resultado
        notes_group = Adw.PreferencesGroup(title="Notas o resultado")
        notes_box = Gtk.Box(spacing=8)
        self._notes_entry = Gtk.Entry(
            text=self.task.result_notes,
            placeholder_text="Registra observaciones, resultado del trabajo o validaciones…",
            hexpand=True,
        )
        save_notes_btn = Gtk.Button(label="Guardar notas", icon_name="document-save-symbolic")
        save_notes_btn.connect("clicked", lambda _b: self._save_notes())
        notes_box.append(self._notes_entry)
        notes_box.append(save_notes_btn)
        notes_group.add(notes_box)
        page.add(notes_group)

        # 5. Gestión manual de estado
        state_group = Adw.PreferencesGroup(title="Gestión de estado")
        state_row = Adw.ActionRow(
            title="Estado de la tarea",
            subtitle="Modificar manualmente el estado de avance",
        )
        self._state_options = [
            TaskState.PENDING,
            TaskState.IN_PROGRESS,
            TaskState.PENDING_REVIEW,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELED,
        ]
        self._state_dd = Gtk.DropDown.new_from_strings([st.label for st in self._state_options])
        curr_idx = self._state_options.index(self.task.status) if self.task.status in self._state_options else 0
        self._state_dd.set_selected(curr_idx)
        state_row.add_suffix(self._state_dd)

        apply_btn = Gtk.Button(label="Aplicar")
        apply_btn.connect("clicked", lambda _b: self._apply_state())
        state_row.add_suffix(apply_btn)
        state_group.add(state_row)
        page.add(state_group)

        # 6. Historial de trazabilidad plegable
        self._history_group = Adw.PreferencesGroup(title="Trazabilidad")
        self._render_history_expander()
        page.add(self._history_group)

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

    def _render_history_expander(self) -> None:
        history = self.ctx.tasks.list_history(self.task.id) if hasattr(self.ctx, "tasks") and self.ctx.tasks else ()
        title = f"Historial de transiciones ({len(history)})"
        expander = Adw.ExpanderRow(
            title=title,
            subtitle="Registro cronológico inmutable de cambios de estado",
        )
        if history:
            for entry in history:
                from_lbl = TaskState(entry.from_status).label if entry.from_status else "Inicio"
                to_lbl = TaskState(entry.to_status).label if entry.to_status else entry.to_status
                note_str = f" · {entry.note}" if entry.note else ""
                time_str = w.relative_time(entry.created_at)
                row = Adw.ActionRow(
                    title=f"{from_lbl} → {to_lbl}",
                    subtitle=f"{time_str}{note_str}",
                )
                row.add_prefix(Gtk.Image.new_from_icon_name("pan-end-symbolic"))
                expander.add_row(row)
        else:
            empty_row = Adw.ActionRow(title="Sin cambios de estado adicionales")
            expander.add_row(empty_row)

        self._history_group.add(expander)

    def _toast(self, message: str) -> None:
        if self._on_toast:
            self._on_toast(message)

    def _copy_prompt(self) -> None:
        if not self.task.prompt:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(self.task.prompt)
            self._toast("Prompt copiado al portapapeles")

    def _save_notes(self) -> None:
        notes = self._notes_entry.get_text().strip()
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            updated = self.ctx.tasks.update_notes(self.task.id, notes)
            if updated is not None:
                self.task = updated
            self._toast("Notas guardadas")

    def _mark_completed(self) -> None:
        notes = self._notes_entry.get_text().strip()
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            updated = self.ctx.tasks.mark_completed(self.task.id, notes)
            if updated is not None:
                self.task = updated
                self._update_status_ui()
                self._toast("Tarea marcada como completada")

    def _apply_state(self) -> None:
        sel = self._state_dd.get_selected()
        new_state = self._state_options[sel]
        if new_state == self.task.status:
            return
        if hasattr(self.ctx, "tasks") and self.ctx.tasks is not None:
            updated = self.ctx.tasks.update_status(
                self.task.id,
                new_state,
                note="Cambio manual desde diálogo de detalle",
            )
            if updated is not None:
                self.task = updated
                self._update_status_ui()
                self._toast(f"Estado cambiado a {new_state.label}")

    def _update_status_ui(self) -> None:
        # Actualiza pill
        w.clear_box(self._status_pill_holder)
        self._status_pill = w.status_pill(self.task.status.label, self.task.status.style)
        self._status_pill_holder.append(self._status_pill)

        # Actualiza visibilidad de botón completar
        self.complete_btn.set_visible(self.task.status != TaskState.COMPLETED)

        # Actualiza selector desplegable
        curr_idx = self._state_options.index(self.task.status) if self.task.status in self._state_options else 0
        self._state_dd.set_selected(curr_idx)

    def _open_terminal(self, session_id: str) -> None:
        if self.get_root() is not None:
            self.close()
        if self._on_open_session:
            self._on_open_session(session_id)
