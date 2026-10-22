"""Preferencias: apariencia, integraciones y ubicación de los datos."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ia_workspace.agents.tools import describe_install
from ia_workspace.context import AppContext
from ia_workspace.ui import widgets as w

SCHEME_KEY = "appearance.scheme"
# (clave guardada, título, descripción, esquema de Libadwaita). Las claves son las de
# siempre ("system", "light", "dark"), así que la elección anterior se conserva.
SCHEMES = (
    ("system", "Automático (sistema)", "Sigue el modo claro u oscuro de GNOME", Adw.ColorScheme.DEFAULT),
    ("light", "Catppuccin Latte", "Apariencia clara", Adw.ColorScheme.FORCE_LIGHT),
    ("dark", "Catppuccin Mocha", "Apariencia oscura", Adw.ColorScheme.FORCE_DARK),
)


def apply_color_scheme(ctx: AppContext) -> None:
    """Aplica la apariencia guardada solo a IA Workspace (``Adw.StyleManager``; GNOME no
    cambia). Latte/Mocha se aplican a la interfaz y a las terminales al notificar
    ``notify::dark`` (``ui/theme.py`` y ``terminal/theme.py``); en Automático,
    Libadwaita sigue el modo del sistema y sus cambios llegan por la misma señal."""
    name = ctx.settings.get(SCHEME_KEY, "system")
    scheme = next((s for key, _l, _d, s in SCHEMES if key == name), Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(title="Preferencias", search_enabled=False)
        self.ctx = ctx
        self.add(self._general_page())
        self.add(self._integrations_page())

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        appearance = Adw.PreferencesGroup(
            title="Apariencia",
            description="Solo afecta a IA Workspace y a sus terminales; GNOME no cambia.",
        )
        current = self.ctx.settings.get(SCHEME_KEY, "system")
        if current not in {k for k, *_ in SCHEMES}:
            current = "system"
        self.scheme_checks: dict[str, Gtk.CheckButton] = {}
        leader: Gtk.CheckButton | None = None
        for key, title, subtitle, _scheme in SCHEMES:
            check = Gtk.CheckButton(group=leader, active=key == current, valign=Gtk.Align.CENTER)
            leader = leader or check
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable_widget=check)
            row.add_prefix(check)
            check.connect("toggled", self._on_scheme, key)
            self.scheme_checks[key] = check
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
        copy_row = Adw.SwitchRow(
            title="Copiar al seleccionar en terminales",
            subtitle="Copia automáticamente al soltar la selección del ratón",
        )
        copy_on_select = self.ctx.settings.get("terminal.copy_on_select", "true") != "false"
        copy_row.set_active(copy_on_select)
        copy_row.connect("notify::active", self._on_copy_on_select)
        self.copy_on_select_row = copy_row
        behaviour.add(copy_row)
        page.add(behaviour)

        data = Adw.PreferencesGroup(title="Ubicación de los datos")
        db_path = str(self.ctx.paths.database) if self.ctx.paths else self.ctx.db.path
        db_row = Adw.ActionRow(title="Base de datos local", subtitle=db_path, subtitle_selectable=True)
        db_row.add_css_class("property")
        data.add(db_row)
        page.add(data)
        return page

    def _on_scheme(self, check: Gtk.CheckButton, key: str) -> None:
        if not check.get_active():
            return  # el grupo también avisa de la opción que se desmarca
        self.ctx.settings.set(SCHEME_KEY, key)
        apply_color_scheme(self.ctx)

    def _on_copy_on_select(self, row: Adw.SwitchRow, _pspec: object) -> None:
        val = row.get_active()
        self.ctx.settings.set("terminal.copy_on_select", "true" if val else "false")

    def _integrations_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Integraciones", icon_name="network-wired-symbolic")
        check = Gtk.Button(label="Comprobar", valign=Gtk.Align.CENTER)
        check.add_css_class("flat")
        check.set_tooltip_text("Consultar instalación y sesión con los comandos oficiales de cada herramienta")
        check.connect("clicked", lambda _b: self._check_tools())
        tools = Adw.PreferencesGroup(
            title="Herramientas oficiales",
            description=(
                "Cada herramienta usa su propio inicio de sesión y tu suscripción actual. "
                "IA Workspace no pide claves API, no guarda credenciales ni tokens y no genera "
                "facturación adicional."
            ),
            header_suffix=check,
        )
        self._tool_rows: dict = {}
        for status in self.ctx.agents.statuses():
            row = Adw.ActionRow(title=status.kind.label, subtitle_selectable=True)
            row.add_prefix(w.tool_chip(status.kind))
            holder = Gtk.Box(valign=Gtk.Align.CENTER)
            row.add_suffix(holder)
            self._tool_rows[status.kind] = (row, holder)
            tools.add(row)
        tools.add(self._chatgpt_row())
        page.add(tools)

        note = Adw.PreferencesGroup(
            description=(
                "Google Antigravity se integra mediante su CLI oficial «agy», que gestiona el inicio "
                "de sesión dentro de la propia sesión. Si solo tienes la aplicación gráfica, "
                "ábrela por separado: IA Workspace no la controla."
            )
        )
        page.add(note)
        self._unsubscribe = self.ctx.agents.subscribe(lambda _k: self._render_tools())
        self.connect("closed", lambda _d: self._unsubscribe())
        self._render_tools()
        return page

    def _chatgpt_row(self) -> Adw.ActionRow:
        from ia_workspace.ui.chatgpt_browser import WEBKIT_AVAILABLE

        if not WEBKIT_AVAILABLE:
            row = Adw.ActionRow(title="ChatGPT", subtitle="Se usa en tu navegador con tu propia sesión")
            row.add_prefix(w.tool_chip("gpt", "GPT", "ChatGPT"))
            row.add_suffix(w.status_pill("Navegador", "idle"))
            return row
        row = Adw.ActionRow(
            title="ChatGPT",
            subtitle=(
                "Integrado en IA Workspace: inicias sesión en el sitio oficial y la sesión se guarda en "
                "el almacenamiento web propio de la aplicación (también puedes abrir cada conversación "
                "en tu navegador)."
            ),
        )
        row.add_prefix(w.tool_chip("gpt", "GPT", "ChatGPT"))
        clear = Gtk.Button(label="Borrar datos web…", valign=Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.set_tooltip_text("Cierra la sesión de ChatGPT guardada en IA Workspace")
        clear.connect("clicked", lambda _b: self._confirm_clear_web_data())
        row.add_suffix(clear)
        return row

    def _confirm_clear_web_data(self) -> None:
        dialog = Adw.AlertDialog(
            heading="¿Borrar los datos web de ChatGPT?",
            body=(
                "Se borrarán las cookies, la caché y el resto de datos que el sitio de ChatGPT guardó en "
                "IA Workspace, así que tendrás que volver a iniciar sesión. Tus conversaciones vinculadas, "
                "tu cuenta y tu navegador no se modifican."
            ),
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("clear", "Borrar datos web")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: self._clear_web_data() if r == "clear" else None)
        dialog.present(self)
        self._last_dialog = dialog

    def _clear_web_data(self) -> None:
        from ia_workspace.ui.chatgpt_browser import clear_website_data

        data_dir = self.ctx.paths.data_dir / "webkit" if self.ctx.paths is not None else None
        clear_website_data(data_dir, lambda ok: self.add_toast(Adw.Toast(
            title="Datos web de ChatGPT borrados" if ok else "No se pudieron borrar los datos web"
        )))

    def _check_tools(self) -> None:
        self.ctx.agents.detect()
        for status in self.ctx.agents.statuses():
            if status.installed:
                self.ctx.agents.refresh(status.kind)

    def _render_tools(self) -> None:
        styles = {"Sesión iniciada": "done", "Requiere iniciar sesión": "wait", "No instalado": "off"}
        for status in self.ctx.agents.statuses():
            row, holder = self._tool_rows[status.kind]
            note = describe_install(status.kind, status.executable)
            subtitle = status.executable or status.tool.install_hint
            row.set_subtitle(f"{subtitle}\n{note}" if note else subtitle)
            w.clear_box(holder)
            holder.append(w.status_pill(status.summary, styles.get(status.summary, "idle"),
                                        spinning=status.checking))
