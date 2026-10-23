"""Preferencias: apariencia, integraciones y ubicación de los datos."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import threading
from typing import Any

from gi.repository import Adw, GLib, Gtk

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


def _format_size(bytes_count: int) -> str:
    val = float(bytes_count)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(val)} B"
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{int(bytes_count)} B"


def apply_color_scheme(ctx: AppContext) -> None:
    """Aplica la apariencia guardada solo a IA Workspace (``Adw.StyleManager``; GNOME no
    cambia). Latte/Mocha se aplican a la interfaz y a las terminales al notificar
    ``notify::dark`` (``ui/theme.py`` y ``terminal/theme.py``); en Automático,
    Libadwaita sigue el modo del sistema y sus cambios llegan por la misma señal."""
    name = ctx.settings.get(SCHEME_KEY, "system")
    scheme = next((s for key, _l, _d, s in SCHEMES if key == name), Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, ctx: AppContext, parent_window: Gtk.Window | None = None) -> None:
        super().__init__(title="Preferencias", search_enabled=False)
        self.ctx = ctx
        self.parent_window = parent_window
        self._is_backing_up = False
        self._verifying_paths: set[Path] = set()
        self._verification_cache: dict[str, dict[str, Any]] = {}
        self.general_page = self._general_page()
        self.integrations_page = self._integrations_page()
        self.backups_page = self._backups_page()
        self.add(self.general_page)
        self.add(self.integrations_page)
        self.add(self.backups_page)

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

    # ---- Página de Respaldos y Recuperación (Fase 7B.4B) -----------------------

    def _backups_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(
            title="Respaldos",
            icon_name="document-save-symbolic",
        )

        # 1. Crear respaldo
        group_create = Adw.PreferencesGroup(
            title="Crear respaldo",
            description=(
                "Genera una instantánea de los datos internos de IA Workspace (base de datos, tareas, "
                "plantillas, historial y Cerebro) en un archivo .tar.gz. "
                "No incluye el código fuente ni los archivos de los proyectos externos."
            ),
        )
        row_create = Adw.ActionRow(
            title="Generar instantánea",
            subtitle="Guarda el estado de los editores y empaqueta los datos internos de la aplicación",
        )
        row_create.add_prefix(Gtk.Image.new_from_icon_name("document-save-symbolic"))

        self._backup_status_holder = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self._btn_create_backup = Gtk.Button(label="Crear respaldo", valign=Gtk.Align.CENTER)
        self._btn_create_backup.add_css_class("suggested-action")
        self._btn_create_backup.connect("clicked", lambda _b: self._on_create_backup_clicked())
        self._backup_status_holder.append(self._btn_create_backup)
        row_create.add_suffix(self._backup_status_holder)
        group_create.add(row_create)
        page.add(group_create)

        # 2. Respaldos existentes
        refresh_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        refresh_btn.set_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Actualizar lista de respaldos")
        refresh_btn.connect("clicked", lambda _b: self._refresh_backups_list())

        self._backups_group = Adw.PreferencesGroup(
            title="Respaldos disponibles",
            description=(
                "Archivos almacenados en la carpeta de respaldos. Puedes verificar su integridad "
                "física SQLite y hashes SHA-256."
            ),
            header_suffix=refresh_btn,
        )
        self._backup_rows: list[Adw.ActionRow] = []
        page.add(self._backups_group)
        self._refresh_backups_list()

        # 3. Restauración
        group_recovery = Adw.PreferencesGroup(
            title="Restauración",
            description="La restauración no está disponible con la aplicación abierta.",
        )
        row_offline = Adw.ActionRow(
            title="Restauración offline obligatoria",
            subtitle=(
                "Para garantizar la exclusividad del almacenamiento SQLite y evitar daños en los datos, "
                "cualquier restauración requiere cerrar completamente IA Workspace antes de proceder."
            ),
        )
        row_offline.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        row_offline.add_suffix(w.status_pill("Requiere app cerrada", "wait"))
        group_recovery.add(row_offline)
        page.add(group_recovery)

        # 4. Seguridad y confidencialidad
        group_security = Adw.PreferencesGroup(
            title="Seguridad y confidencialidad",
        )
        row_privacy = Adw.ActionRow(
            title="Contenido no cifrado",
            subtitle=(
                "El respaldo almacena únicamente la configuración y datos internos de la aplicación, "
                "sin incluir repositorios externos. Se genera con permisos de lectura exclusivos "
                "para el usuario (0600) pero sin cifrado en reposo."
            ),
        )
        row_privacy.add_prefix(Gtk.Image.new_from_icon_name("security-medium-symbolic"))
        group_security.add(row_privacy)
        page.add(group_security)

        return page

    def _refresh_backups_list(self) -> None:
        for row in self._backup_rows:
            self._backups_group.remove(row)
        self._backup_rows.clear()

        if self.ctx.paths is None or not self.ctx.paths.backups_dir.exists():
            empty = Adw.ActionRow(
                title="No hay respaldos disponibles",
                subtitle="Los respaldos generados se mostrarán en esta lista.",
            )
            empty.set_sensitive(False)
            self._backups_group.add(empty)
            self._backup_rows.append(empty)
            return

        backups_dir = self.ctx.paths.backups_dir
        try:
            files = [
                f for f in backups_dir.glob("*.tar.gz")
                if f.is_file() and not f.is_symlink() and not f.name.startswith(".")
            ]
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []

        if not files:
            empty = Adw.ActionRow(
                title="No hay respaldos disponibles",
                subtitle="Usa el botón superior para crear el primer respaldo.",
            )
            empty.set_sensitive(False)
            self._backups_group.add(empty)
            self._backup_rows.append(empty)
            return

        for bkp_file in files:
            st = bkp_file.stat()
            size_str = _format_size(st.st_size)
            date_str = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M")

            row = Adw.ActionRow(
                title=bkp_file.name,
                title_selectable=True,
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))

            suffix_box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
            pill_holder = Gtk.Box(valign=Gtk.Align.CENTER)

            cached = self._verification_cache.get(bkp_file.name)
            if cached is None:
                pill_holder.append(w.status_pill("Sin verificar", "idle"))
                row.set_subtitle(f"{date_str} · {size_str} · Estado: Sin verificar")
            elif cached.get("ok"):
                pill_holder.append(w.status_pill("Válido", "done"))
                row.set_subtitle(f"{date_str} · {size_str} · {cached.get('summary', 'Válido')}")
            else:
                pill_holder.append(w.status_pill("Error", "error"))
                row.set_subtitle(f"{date_str} · {size_str} · Error: {cached.get('error', 'Fallo')}")

            suffix_box.append(pill_holder)

            btn_verify = Gtk.Button(label="Verificar", valign=Gtk.Align.CENTER)
            btn_verify.add_css_class("flat")
            btn_verify.set_tooltip_text("Verificar integridad física SQLite y hashes SHA-256")
            btn_verify.connect(
                "clicked",
                lambda _b, p=bkp_file, r=row, ph=pill_holder, bv=btn_verify, ds=date_str, sz=size_str:
                    self._on_verify_backup_clicked(p, r, ph, bv, ds, sz),
            )
            suffix_box.append(btn_verify)

            row.add_suffix(suffix_box)
            self._backups_group.add(row)
            self._backup_rows.append(row)

    def _on_create_backup_clicked(self) -> None:
        if self._is_backing_up:
            return
        if not self.ctx.paths:
            self.add_toast(Adw.Toast(title="No se pueden crear respaldos en una sesión sin rutas de datos."))
            return

        self._is_backing_up = True
        self._btn_create_backup.set_sensitive(False)

        # 1. Guardar borradores y estados pendientes en el hilo principal
        root = self.parent_window or self.get_root()
        if root is not None and hasattr(root, "save_state"):
            root.save_state()
        if self.ctx.workspace is not None:
            self.ctx.workspace.flush()

        # 2. Sincronizar propuestas externas de proyectos registrados en el hilo principal
        if hasattr(self.ctx, "projects") and hasattr(self.ctx, "brain") and self.ctx.brain is not None:
            for project in self.ctx.projects.list():
                try:
                    self.ctx.brain.sync_proposals(project)
                except Exception:
                    pass

        w.clear_box(self._backup_status_holder)
        self._backup_status_holder.append(w.status_pill("Respaldando…", "working", spinning=True))

        paths = self.ctx.paths

        def bg_backup() -> None:
            from ia_workspace.services.backup import BackupService

            try:
                # db=None garantiza que BackupService abra su propia conexión de solo
                # lectura dentro de este hilo, sin compartir conexiones SQLite entre hilos.
                service = BackupService(paths=paths, db=None)
                backup_path = service.create_backup()
                GLib.idle_add(self._on_backup_success, backup_path)
            except Exception as exc:
                GLib.idle_add(self._on_backup_failed, str(exc))

        threading.Thread(target=bg_backup, daemon=True).start()

    def _on_backup_success(self, backup_path: Path) -> None:
        self._is_backing_up = False
        self._btn_create_backup.set_sensitive(True)
        w.clear_box(self._backup_status_holder)
        self._backup_status_holder.append(self._btn_create_backup)
        self._refresh_backups_list()
        self.add_toast(Adw.Toast(title=f"Respaldo creado: {backup_path.name}"))

    def _on_backup_failed(self, error_msg: str) -> None:
        self._is_backing_up = False
        self._btn_create_backup.set_sensitive(True)
        w.clear_box(self._backup_status_holder)
        self._backup_status_holder.append(self._btn_create_backup)
        self.add_toast(Adw.Toast(title=f"Error al crear respaldo: {error_msg}"))

    def _on_verify_backup_clicked(
        self,
        backup_path: Path,
        row: Adw.ActionRow,
        pill_holder: Gtk.Box,
        btn_verify: Gtk.Button,
        date_str: str,
        size_str: str,
    ) -> None:
        if backup_path in self._verifying_paths:
            return
        if not self.ctx.paths:
            return

        self._verifying_paths.add(backup_path)
        btn_verify.set_sensitive(False)
        w.clear_box(pill_holder)
        pill_holder.append(w.status_pill("Verificando…", "working", spinning=True))

        paths = self.ctx.paths

        def bg_verify() -> None:
            from ia_workspace.services.restore import RestoreService

            try:
                service = RestoreService(paths=paths)
                report = service.validate_backup(backup_path)
                GLib.idle_add(
                    self._on_verify_success,
                    backup_path,
                    row,
                    pill_holder,
                    btn_verify,
                    date_str,
                    size_str,
                    report,
                )
            except Exception as exc:
                GLib.idle_add(
                    self._on_verify_failed,
                    backup_path,
                    row,
                    pill_holder,
                    btn_verify,
                    date_str,
                    size_str,
                    str(exc),
                )

        threading.Thread(target=bg_verify, daemon=True).start()

    def _on_verify_success(
        self,
        backup_path: Path,
        row: Adw.ActionRow,
        pill_holder: Gtk.Box,
        btn_verify: Gtk.Button,
        date_str: str,
        size_str: str,
        report: Any,
    ) -> None:
        self._verifying_paths.discard(backup_path)
        btn_verify.set_sensitive(True)
        w.clear_box(pill_holder)
        pill_holder.append(w.status_pill("Válido", "done"))

        summary = f"Íntegro (SHA-256 ok, {report.total_files} archivos)"
        self._verification_cache[backup_path.name] = {"ok": True, "summary": summary}
        row.set_subtitle(f"{date_str} · {size_str} · {summary}")
        self.add_toast(Adw.Toast(title=f"Respaldo verificado con éxito: {backup_path.name}"))

    def _on_verify_failed(
        self,
        backup_path: Path,
        row: Adw.ActionRow,
        pill_holder: Gtk.Box,
        btn_verify: Gtk.Button,
        date_str: str,
        size_str: str,
        error_msg: str,
    ) -> None:
        self._verifying_paths.discard(backup_path)
        btn_verify.set_sensitive(True)
        w.clear_box(pill_holder)
        pill_holder.append(w.status_pill("Error", "error"))

        self._verification_cache[backup_path.name] = {"ok": False, "error": error_msg}
        row.set_subtitle(f"{date_str} · {size_str} · Error: {error_msg}")
        self.add_toast(Adw.Toast(title=f"Fallo de integridad: {error_msg}"))
