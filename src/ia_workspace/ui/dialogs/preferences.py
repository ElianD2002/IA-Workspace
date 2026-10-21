"""Preferencias: apariencia, integraciones y ubicación de los datos."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ia_workspace.context import AppContext
from ia_workspace.services.integrations import detect_tools
from ia_workspace.ui import widgets as w

SCHEME_KEY = "appearance.scheme"
SCHEMES = (
    ("system", "Predeterminado del sistema", Adw.ColorScheme.DEFAULT),
    ("light", "Claro", Adw.ColorScheme.FORCE_LIGHT),
    ("dark", "Oscuro", Adw.ColorScheme.FORCE_DARK),
)


def apply_color_scheme(ctx: AppContext) -> None:
    name = ctx.settings.get(SCHEME_KEY, "system")
    scheme = next((s for key, _l, s in SCHEMES if key == name), Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(title="Preferencias", search_enabled=False)
        self.ctx = ctx
        self.add(self._general_page())
        self.add(self._integrations_page())

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        appearance = Adw.PreferencesGroup(title="Apariencia")
        model = Gtk.StringList.new([label for _k, label, _s in SCHEMES])
        current = self.ctx.settings.get(SCHEME_KEY, "system")
        row = Adw.ComboRow(title="Estilo", model=model)
        row.set_selected(next((i for i, (k, _l, _s) in enumerate(SCHEMES) if k == current), 0))
        row.connect("notify::selected", self._on_scheme)
        appearance.add(row)
        page.add(appearance)

        behaviour = Adw.PreferencesGroup(
            title="Sesiones",
            description=(
                "Los agentes siguen trabajando mientras IA Workspace esté abierta o minimizada. "
                "Al cerrar la aplicación por completo con agentes activos, se pide confirmación "
                "y se finalizan de forma ordenada."
            ),
        )
        page.add(behaviour)

        data = Adw.PreferencesGroup(title="Ubicación de los datos")
        db_path = str(self.ctx.paths.database) if self.ctx.paths else self.ctx.db.path
        db_row = Adw.ActionRow(title="Base de datos local", subtitle=db_path, subtitle_selectable=True)
        db_row.add_css_class("property")
        data.add(db_row)
        page.add(data)
        return page

    def _on_scheme(self, row: Adw.ComboRow, _pspec: object) -> None:
        key = SCHEMES[row.get_selected()][0]
        self.ctx.settings.set(SCHEME_KEY, key)
        apply_color_scheme(self.ctx)

    def _integrations_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Integraciones", icon_name="network-wired-symbolic")
        tools = Adw.PreferencesGroup(
            title="Herramientas oficiales",
            description=(
                "Cada herramienta usa su propio inicio de sesión y tu suscripción actual. "
                "IA Workspace no pide claves API, no guarda credenciales ni tokens y no genera "
                "facturación adicional."
            ),
        )
        for status in detect_tools():
            row = Adw.ActionRow(
                title=status.kind.label,
                subtitle=status.executable or "No encontrado en el PATH",
                subtitle_selectable=status.installed,
            )
            row.add_prefix(w.tool_chip(status.kind))
            row.add_suffix(w.status_pill("Instalado", "done") if status.installed else w.status_pill("No detectado", "off"))
            tools.add(row)
        chatgpt = Adw.ActionRow(title="ChatGPT", subtitle="Se usa en tu navegador con tu propia sesión")
        chatgpt.add_prefix(w.tool_chip("gpt", "GPT", "ChatGPT"))
        chatgpt.add_suffix(w.status_pill("Navegador", "idle"))
        tools.add(chatgpt)
        page.add(tools)
        return page
