"""Diálogos para vincular, renombrar y desvincular conversaciones de ChatGPT."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import ChatLink
from ia_workspace.services.chat_links import ChatLinkError, ChatLinkService


class LinkConversationDialog(Adw.AlertDialog):
    """Pegar el enlace de una conversación y, opcionalmente, un título.

    La validación es inmediata y local: no se abre el enlace ni se descarga nada.
    """

    def __init__(self, service: ChatLinkService, project_id: int, project_name: str,
                 on_saved: Callable[[ChatLink], None], on_error: Callable[[str], None]) -> None:
        super().__init__(
            heading="Vincular conversación",
            body=(
                f"Pega el enlace de una conversación de ChatGPT para tenerla a mano en «{project_name}». "
                "IA Workspace no lee su contenido: se abrirá en tu navegador con tu sesión habitual."
            ),
        )
        self._service = service
        self._project_id = project_id
        self._on_saved = on_saved
        self._on_error = on_error

        group = Adw.PreferencesGroup()
        self.url_row = Adw.EntryRow(title="Enlace de la conversación", input_purpose=Gtk.InputPurpose.URL)
        self.url_row.connect("changed", lambda _e: self._validate())
        group.add(self.url_row)
        self.title_row = Adw.EntryRow(title="Título (opcional)", activates_default=True)
        group.add(self.title_row)
        self.feedback = Gtk.Label(xalign=0, wrap=True, margin_top=8, margin_start=4)
        self.feedback.add_css_class("caption")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(self.feedback)
        self.set_extra_child(box)

        self.add_response("cancel", "Cancelar")
        self.add_response("save", "Guardar conversación")
        self.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("save")
        self.set_close_response("cancel")
        self.connect("response", self._on_response)
        self._validate()

    def _validate(self) -> bool:
        text = self.url_row.get_text().strip()
        for css in ("error", "success", "dim-label"):
            self.feedback.remove_css_class(css)
        if not text:
            self.feedback.set_label("Admite chatgpt.com/c/…, chatgpt.com/share/… y enlaces de chat.openai.com.")
            self.feedback.add_css_class("dim-label")
            self.set_response_enabled("save", False)
            return False
        try:
            parsed = self._service.check(self._project_id, text)
        except ChatLinkError as exc:
            self.feedback.set_label(str(exc))
            self.feedback.add_css_class("error")
            self.set_response_enabled("save", False)
            return False
        if parsed.kind.value == "shared":
            message = "Enlace compartido: es una copia pública de solo lectura, no tu conversación privada."
        else:
            message = "Conversación privada: solo se abrirá con tu cuenta de ChatGPT."
        self.feedback.set_label(message)
        self.feedback.add_css_class("success")
        self.set_response_enabled("save", True)
        return True

    def _on_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "save" or not self._validate():
            return
        try:
            link = self._service.link(self._project_id, self.url_row.get_text(), self.title_row.get_text())
        except ChatLinkError as exc:
            self._on_error(str(exc))
            return
        self._on_saved(link)


def build_rename_dialog(service: ChatLinkService, link: ChatLink,
                        on_error: Callable[[str], None]) -> Adw.AlertDialog:
    entry = Adw.EntryRow(title="Título", text=link.title, activates_default=True)
    group = Adw.PreferencesGroup()
    group.add(entry)
    dialog = Adw.AlertDialog(heading="Renombrar conversación", extra_child=group)
    dialog.add_response("cancel", "Cancelar")
    dialog.add_response("rename", "Renombrar")
    dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("rename")
    dialog.set_close_response("cancel")
    entry.connect("changed", lambda e: dialog.set_response_enabled("rename", bool(e.get_text().strip())))

    def on_response(_d: Adw.AlertDialog, response: str) -> None:
        if response == "rename":
            try:
                service.rename(link.id, entry.get_text())
            except ChatLinkError as exc:
                on_error(str(exc))

    dialog.connect("response", on_response)
    dialog.entry = entry  # accesible para pruebas
    return dialog


def build_unlink_dialog(service: ChatLinkService, link: ChatLink) -> Adw.AlertDialog:
    dialog = Adw.AlertDialog(
        heading=f"¿Desvincular «{link.title}»?",
        body="Se quitará de este proyecto en IA Workspace. La conversación no se borra de ChatGPT.",
    )
    dialog.set_body_use_markup(False)
    dialog.add_response("cancel", "Cancelar")
    dialog.add_response("unlink", "Desvincular")
    dialog.set_response_appearance("unlink", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: service.unlink(link.id) if r == "unlink" else None)
    return dialog
