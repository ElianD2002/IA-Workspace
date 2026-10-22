"""Diálogo para revisar, comparar diferencias y aprobar o rechazar propuestas."""

from __future__ import annotations

import difflib
from collections.abc import Callable

from gi.repository import Adw, Gtk, Pango

from ia_workspace.brain.proposals import (
    ProposalContent,
    ProposalOperation,
    ProposalStatus,
)
from ia_workspace.ui import widgets as w
from ia_workspace.ui.markdown_view import render_markdown


def _agent_display_name(agent_kind: str) -> str:
    match agent_kind:
        case "claude-code":
            return "Claude Code"
        case "codex":
            return "OpenAI Codex"
        case "antigravity":
            return "Google Antigravity"
        case "cli":
            return "CLI de terminal (iaw-brain)"
        case _:
            return agent_kind or "Agente"


def _scroller(child: Gtk.Widget, min_height: int = 340) -> Gtk.ScrolledWindow:
    scroller = Gtk.ScrolledWindow(
        child=child,
        min_content_height=min_height,
        vexpand=True,
        hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
    )
    scroller.add_css_class("card")
    return scroller


class ProposalReviewDialog(Adw.Dialog):
    """Permite al usuario examinar una propuesta, ver diff si es actualización y aprobarla/rechazarla."""

    def __init__(
        self,
        *,
        proposal: ProposalContent,
        project_name: str,
        original_body: str | None = None,
        on_approve: Callable[[str], None] | None = None,
        on_reject: Callable[[str, str], None] | None = None,
    ) -> None:
        meta = proposal.meta
        is_update = meta.operation is ProposalOperation.UPDATE
        title_text = f"Revisar: {meta.title}"
        super().__init__(title=title_text, content_width=720, content_height=680)

        self.proposal = proposal
        self._on_approve = on_approve
        self._on_reject = on_reject

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        close_btn = Gtk.Button(label="Cerrar")
        close_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(close_btn)

        can_act = meta.status in (ProposalStatus.PENDING, ProposalStatus.CONFLICT)

        self.reject_button = Gtk.Button(label="Rechazar", visible=can_act)
        self.reject_button.add_css_class("destructive-action")
        self.reject_button.connect("clicked", lambda _b: self._prompt_reject())
        header.pack_end(self.reject_button)

        self.approve_button = Gtk.Button(label="Aprobar", visible=can_act)
        self.approve_button.add_css_class("suggested-action")
        self.approve_button.connect("clicked", lambda _b: self._do_approve())
        header.pack_end(self.approve_button)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_start=18,
            margin_end=18,
            margin_top=8,
            margin_bottom=18,
        )

        # Barra informativa
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        top_row = Gtk.Box(spacing=8)
        op_label = "Nuevo documento" if not is_update else "Actualización"
        top_row.append(w.status_pill(op_label, "idle"))
        status_style = "warning" if meta.status is ProposalStatus.PENDING else (
            "error" if meta.status is ProposalStatus.CONFLICT else "idle"
        )
        top_row.append(w.status_pill(meta.status.label, status_style))
        agent_name = _agent_display_name(meta.agent_kind)
        top_row.append(w.caption(f"Origen: {agent_name}"))
        info_box.append(top_row)

        details = [f"Proyecto: «{project_name}»", f"Destino: {meta.relpath}"]
        if meta.summary:
            details.append(f"Motivo: {meta.summary}")
        details_label = w.caption(" · ".join(details))
        details_label.set_wrap(True)
        details_label.set_ellipsize(Pango.EllipsizeMode.NONE)
        info_box.append(details_label)

        if meta.status is ProposalStatus.CONFLICT:
            banner = Adw.Banner(
                title="Conflicto detectado: el documento original cambió después de crearse esta propuesta. "
                      "Comprueba las diferencias antes de aprobar.",
                revealed=True,
            )
            info_box.append(banner)
        elif not can_act:
            state_msg = f"Esta propuesta está {meta.status.label.lower()}."
            if meta.review_note:
                state_msg += f" Nota: {meta.review_note}"
            info_box.append(Adw.Banner(title=state_msg, revealed=True))

        box.append(info_box)

        # Visor de contenido o pestañas para diff
        if is_update and original_body is not None:
            view_stack = Adw.ViewStack()

            # Pestaña 1: Diferencias (Diff)
            diff_text = self._compute_diff(original_body, proposal.body, meta.relpath)
            diff_view = Gtk.TextView(
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.NONE,
                monospace=True,
                top_margin=10,
                bottom_margin=10,
                left_margin=12,
                right_margin=12,
            )
            diff_view.get_buffer().set_text(diff_text)
            diff_page = view_stack.add_titled(
                _scroller(diff_view), "diff", "Diferencias"
            )
            diff_page.set_icon_name("edit-diff-symbolic" if Gtk.IconTheme.get_for_display(
                Gtk.Widget.get_display(diff_view) if hasattr(Gtk.Widget, "get_display") else None
            ).has_icon("edit-diff-symbolic") else "document-edit-symbolic")

            # Pestaña 2: Propuesta
            prop_view = Gtk.TextView(
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                top_margin=12,
                bottom_margin=12,
                left_margin=14,
                right_margin=14,
            )
            render_markdown(prop_view.get_buffer(), proposal.body)
            view_stack.add_titled(_scroller(prop_view), "proposed", "Propuesta")

            # Pestaña 3: Documento actual
            orig_view = Gtk.TextView(
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                top_margin=12,
                bottom_margin=12,
                left_margin=14,
                right_margin=14,
            )
            render_markdown(orig_view.get_buffer(), original_body)
            view_stack.add_titled(_scroller(orig_view), "original", "Documento actual")

            switcher = Adw.ViewSwitcher(
                stack=view_stack,
                policy=Adw.ViewSwitcherPolicy.WIDE,
            )
            header.set_title_widget(switcher)
            box.append(view_stack)
        else:
            # Nuevo documento
            prop_view = Gtk.TextView(
                editable=False,
                cursor_visible=False,
                wrap_mode=Gtk.WrapMode.WORD_CHAR,
                top_margin=12,
                bottom_margin=12,
                left_margin=14,
                right_margin=14,
            )
            render_markdown(prop_view.get_buffer(), proposal.body)
            box.append(_scroller(prop_view))

        toolbar = Adw.ToolbarView(content=box)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)

    @staticmethod
    def _compute_diff(original: str, proposed: str, relpath: str) -> str:
        orig_lines = original.splitlines(keepends=True)
        prop_lines = proposed.splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                orig_lines,
                prop_lines,
                fromfile=f"a/{relpath}",
                tofile=f"b/{relpath} (propuesto)",
                n=3,
            )
        )
        if not diff:
            return "No se encontraron diferencias textuales en el contenido."
        return "".join(diff)

    def _do_approve(self) -> None:
        if self._on_approve:
            self.close()
            self._on_approve(self.proposal.meta.id)

    def _prompt_reject(self) -> None:
        alert = Adw.AlertDialog(
            heading=f"¿Rechazar propuesta «{self.proposal.meta.title}»?",
            body="La propuesta se marcará como rechazada y no se modificará la memoria del proyecto.",
        )
        alert.add_response("cancel", "Cancelar")
        alert.add_response("reject", "Rechazar")
        alert.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "reject":
                self.close()
                if self._on_reject:
                    self._on_reject(self.proposal.meta.id, "")

        alert.connect("response", on_response)
        alert.present(self.get_root())
