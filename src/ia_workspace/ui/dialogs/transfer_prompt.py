"""Diálogo de revisión antes de transferir un prompt a un agente."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gdk, Gtk

from ia_workspace.brain.bridge import compose_prompt_with_reference
from ia_workspace.domain.models import AgentKind
from ia_workspace.sessions.models import BrainLink, BrainLinkStatus
from ia_workspace.ui import widgets as w


class TransferPromptDialog(Adw.Dialog):
    """Muestra proyecto, agente y el prompt completo antes de iniciar la sesión.

    ``on_confirm(method, include_brain)`` se llama al confirmar. ``method`` es ``"direct"``
    cuando la herramienta admite un mecanismo oficial para recibir un prompt
    inicial, o ``"manual"`` cuando hay que copiarlo y pegarlo a mano una vez la
    sesión esté lista. ``include_brain`` indica si se incorpora la referencia al inicio.
    """

    def __init__(
        self,
        *,
        project_name: str,
        project_path: str,
        kind: AgentKind,
        prompt: str,
        direct: bool,
        on_confirm: Callable[..., None],
        brain_link: BrainLink | None = None,
    ) -> None:
        super().__init__(title="Revisar y transferir", content_width=560, content_height=560)
        self.method = "direct" if direct else "manual"
        self._on_confirm = on_confirm
        self.brain_link = brain_link
        self._original_prompt = prompt
        self.brain_switch: Adw.SwitchRow | None = None

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
                "inicial: se copiará al portapapeles y se abrirá una sesión nueva. Pégalo (Ctrl+Shift+V o menú "
                "contextual) en cuanto la sesión esté lista."
            )
        )
        page.add(Adw.PreferencesGroup(description=note_text))

        if brain_link is not None:
            brain_group = Adw.PreferencesGroup(title="Cerebro compartido")
            if brain_link.reference:
                if brain_link.status is BrainLinkStatus.APPLIED and brain_link.via_launch:
                    row = Adw.ActionRow(
                        title="Memoria vinculada automáticamente",
                        subtitle=brain_link.detail,
                    )
                    row.add_prefix(Gtk.Image.new_from_icon_name("accessories-dictionary-symbolic"))
                    copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copiar referencia al portapapeles")
                    copy_btn.add_css_class("flat")
                    copy_btn.connect("clicked", lambda _b: self._copy_ref(brain_link.reference))
                    row.add_suffix(copy_btn)
                    brain_group.add(row)
                else:
                    switch = Adw.SwitchRow(
                        title="Incorporar referencia al inicio del prompt",
                        subtitle="Google Antigravity no admite instrucciones de sistema por CLI: añade el índice y búsqueda al prompt.",
                    )
                    switch.set_active(True)
                    self.brain_switch = switch
                    switch.connect("notify::active", lambda *_: self._sync_prompt_view())
                    brain_group.add(switch)

                    copy_row = Adw.ActionRow(
                        title="Referencia de consulta",
                        subtitle="Disponible para copiar manualmente.",
                    )
                    copy_row.add_prefix(Gtk.Image.new_from_icon_name("accessories-dictionary-symbolic"))
                    copy_btn = Gtk.Button(label="Copiar", icon_name="edit-copy-symbolic", tooltip_text="Copiar referencia")
                    copy_btn.add_css_class("flat")
                    copy_btn.connect("clicked", lambda _b: self._copy_ref(brain_link.reference))
                    copy_row.add_suffix(copy_btn)
                    brain_group.add(copy_row)
            elif brain_link.status is BrainLinkStatus.UNAVAILABLE:
                row = Adw.ActionRow(
                    title="Memoria no disponible",
                    subtitle=brain_link.detail,
                )
                brain_group.add(row)
            page.add(brain_group)

        prompt_group = Adw.PreferencesGroup(title="Prompt")
        self._prompt_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=10,
            right_margin=10,
        )
        self._prompt_view.add_css_class("prompt-view")
        scroller = Gtk.ScrolledWindow(child=self._prompt_view, min_content_height=220, max_content_height=320)
        scroller.add_css_class("card")
        prompt_group.add(scroller)
        page.add(prompt_group)

        self._sync_prompt_view()

        toolbar = Adw.ToolbarView(content=page)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        self.set_default_widget(self.confirm_button)

    @property
    def include_brain(self) -> bool:
        if self.brain_switch is not None:
            return self.brain_switch.get_active()
        return False

    def _sync_prompt_view(self) -> None:
        if self.include_brain and self.brain_link and self.brain_link.reference:
            composed = compose_prompt_with_reference(self._original_prompt, self.brain_link.reference)
            self._prompt_view.get_buffer().set_text(composed)
        else:
            self._prompt_view.get_buffer().set_text(self._original_prompt)

    def _copy_ref(self, ref: str | None) -> None:
        if not ref:
            return
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(ref)

    def _confirm(self) -> None:
        self.close()
        try:
            self._on_confirm(self.method, self.include_brain)
        except TypeError:
            self._on_confirm(self.method)
