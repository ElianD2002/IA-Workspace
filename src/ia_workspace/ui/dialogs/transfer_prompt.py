"""Diálogo de revisión antes de transferir un prompt a un agente."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import AgentKind
from ia_workspace.ui import widgets as w


class TransferPromptDialog(Adw.Dialog):
    """Muestra proyecto, agente y el prompt completo antes de iniciar la sesión.

    ``on_confirm(method)`` solo se llama al confirmar. ``method`` es ``"direct"``
    cuando la herramienta admite un mecanismo oficial para recibir un prompt
    inicial, o ``"manual"`` cuando hay que copiarlo y pegarlo a mano una vez la
    sesión esté lista.
    """

    def __init__(
        self,
        *,
        project_name: str,
        project_path: str,
        kind: AgentKind,
        prompt: str,
        direct: bool,
        on_confirm: Callable[[str], None],
    ) -> None:
        super().__init__(title="Revisar y transferir", content_width=560, content_height=560)
        self.method = "direct" if direct else "manual"
        self._on_confirm = on_confirm

        cancel = Gtk.Button(label="Cancelar")
        cancel.connect("clicked", lambda _b: self.close())
        self.confirm_button = Gtk.Button(label="Transferir" if direct else "Copiar y abrir agente")
        self.confirm_button.add_css_class("suggested-action")
        self.confirm_button.connect("clicked", lambda _b: self._confirm())
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self.confirm_button)

        page = Adw.PreferencesPage()

        summary = Adw.PreferencesGroup()
        target = Adw.ActionRow(
            title=f"Transferir a {kind.label}",
            subtitle=f"{project_name} · se ejecuta en {w.short_path(project_path)}",
        )
        target.add_prefix(w.tool_chip(kind))
        summary.add(target)
        page.add(summary)

        note_text = (
            f"Se abrirá una sesión nueva de {kind.label} en la carpeta del proyecto con este prompt "
            "como instrucción inicial, mediante el mecanismo oficial de la herramienta. No se omite "
            "ninguna aprobación: el agente seguirá pidiendo confirmación igual que si escribieras tú "
            "el prompt."
            if direct
            else (
                f"{kind.label} todavía no tiene un mecanismo oficial para recibir aquí un prompt "
                "inicial: se copiará al portapapeles y se abrirá una sesión nueva. Pégalo (Ctrl+V) en "
                "cuanto la sesión esté lista."
            )
        )
        page.add(Adw.PreferencesGroup(description=note_text))

        prompt_group = Adw.PreferencesGroup(title="Prompt")
        prompt_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=10,
            right_margin=10,
        )
        prompt_view.add_css_class("prompt-view")
        prompt_view.get_buffer().set_text(prompt)
        scroller = Gtk.ScrolledWindow(child=prompt_view, min_content_height=220, max_content_height=320)
        scroller.add_css_class("card")
        prompt_group.add(scroller)
        page.add(prompt_group)

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.set_default_widget(self.confirm_button)

    def _confirm(self) -> None:
        self.close()
        self._on_confirm(self.method)
