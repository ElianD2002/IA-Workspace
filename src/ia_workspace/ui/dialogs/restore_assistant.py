"""Asistente gráfico independiente de recuperación y restauración offline de IA Workspace (Fase 7B.5B).

Provee una ventana nativa en GTK4 + Libadwaita para seleccionar, validar
e instalar respaldos offline con la aplicación principal completamente cerrada.
No inicializa IAWorkspaceApplication, WebKitGTK, terminales ni agentes, y funciona
aun cuando la base de datos activa esté corrupta.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import threading
from zoneinfo import ZoneInfo

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from ia_workspace.config import AppPaths, resolve_paths
from ia_workspace.restore_cli import format_size
from ia_workspace.services.restore import (
    BackupValidationReport,
    RestoreError,
    RestoreExclusivityError,
    RestoreSecurityError,
    RestoreService,
    RestoreValidationError,
)
from ia_workspace import APP_RESTORE_ID
from ia_workspace.ui import theme
import ia_workspace.ui.widgets as w

TZ_TEGUCIGALPA = ZoneInfo("America/Tegucigalpa")


def _format_local_datetime(raw: str | None) -> str:
    """Convierte fecha ISO del respaldo a cadena legible en America/Tegucigalpa."""
    if not raw:
        return "No especificada"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_TEGUCIGALPA).strftime("%d/%m/%Y %H:%M %Z")
    except Exception:
        return raw


def _pluralize(count: int, singular: str, plural: str) -> str:
    """Formatea la cantidad y la etiqueta gramatical en singular o plural."""
    return f"{count} {singular if count == 1 else plural}"


class RestoreAssistantWindow(Adw.ApplicationWindow):
    """Ventana independiente del asistente de recuperación de respaldos."""

    def __init__(self, app: Adw.Application | None = None, paths: AppPaths | None = None, initial_backup: Path | None = None) -> None:
        if app is not None:
            super().__init__(application=app, title="Recuperación de Respaldo — IA Workspace")
        else:
            super().__init__(title="Recuperación de Respaldo — IA Workspace")
        self.paths = paths or resolve_paths()
        self.restore_service = RestoreService(self.paths)
        self._selected_backup: Path | None = None
        self._validation_report: BackupValidationReport | None = None
        self._is_working: bool = False

        self.set_default_size(720, 680)
        self.set_size_request(520, 500)

        # Prevenir cierre accidental durante restauración
        self.connect("close-request", self._on_close_request)

        # Construir estructura visual
        self._build_ui()

        # Si se pasó un respaldo inicial, seleccionarlo
        if initial_backup and initial_backup.is_file():
            self._set_backup_file(initial_backup)
        else:
            self._select_most_recent_backup()

    def _build_ui(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)

        # 1. HeaderBar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        header.set_show_start_title_buttons(False)

        title_widget = Adw.WindowTitle(
            title="Recuperación de Respaldo",
            subtitle="Asistente Offline de IA Workspace",
        )
        header.set_title_widget(title_widget)
        box.append(header)

        # 2. Scrolled content
        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        box.append(scrolled)

        clamp = Adw.Clamp(maximum_size=700, tightening_threshold=560)
        scrolled.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(20)
        content.set_margin_bottom(24)
        content.set_margin_start(20)
        content.set_margin_end(20)
        clamp.set_child(content)

        # Banner informativo
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner_box.add_css_class("card")
        banner_box.set_margin_bottom(4)

        icon_box = Gtk.Box(valign=Gtk.Align.CENTER)
        icon_box.set_margin_start(16)
        icon_box.set_margin_top(16)
        icon_box.set_margin_bottom(16)
        icon = Gtk.Image.new_from_icon_name("system-software-update-symbolic")
        icon.set_pixel_size(32)
        icon_box.append(icon)
        banner_box.append(icon_box)

        banner_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        banner_text.set_margin_top(16)
        banner_text.set_margin_bottom(16)
        banner_text.set_margin_end(16)
        banner_text.set_hexpand(True)

        lbl_title = Gtk.Label(label="Restauración Segura Fuera de Línea", xalign=0)
        lbl_title.add_css_class("title-4")
        banner_text.append(lbl_title)

        lbl_desc = Gtk.Label(
            label="Restaura la base de datos interna y los documentos del Cerebro Compartido a partir "
                  "de una copia de seguridad verificada. La aplicación principal debe permanecer cerrada.",
            xalign=0,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD,
        )
        lbl_desc.add_css_class("dim-label")
        banner_text.append(lbl_desc)

        banner_box.append(banner_text)
        content.append(banner_box)

        # Toast overlay para notificaciones rápidas
        self._toast_overlay = Adw.ToastOverlay()
        content.append(self._toast_overlay)

        # Grupo 1: Selección de Respaldo
        grp_selection = Adw.PreferencesGroup(title="1. Archivo de Respaldo")
        content.append(grp_selection)

        self._file_row = Adw.ActionRow(
            title="Ningún respaldo seleccionado",
            subtitle="Elige un archivo .tar.gz para inspeccionar su integridad",
        )
        self._file_row.add_prefix(Gtk.Image.new_from_icon_name("document-save-symbolic"))

        # Botón para abrir diálogo de archivo
        self._btn_browse = Gtk.Button(label="Seleccionar…", valign=Gtk.Align.CENTER)
        self._btn_browse.connect("clicked", self._on_browse_clicked)

        # Botón para re-validar
        self._btn_validate = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Volver a validar el respaldo",
            valign=Gtk.Align.CENTER,
        )
        self._btn_validate.set_sensitive(False)
        self._btn_validate.connect("clicked", lambda _b: self._start_validation())

        self._file_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._file_suffix.append(self._btn_validate)
        self._file_suffix.append(self._btn_browse)
        self._file_row.add_suffix(self._file_suffix)
        grp_selection.add(self._file_row)

        # Píldora de estado de validación
        self._val_status_row = Adw.ActionRow(title="Estado de validación")
        self._val_status_holder = Gtk.Box(valign=Gtk.Align.CENTER)
        self._val_status_holder.append(w.status_pill("Pendiente", "idle"))
        self._val_status_row.add_suffix(self._val_status_holder)
        grp_selection.add(self._val_status_row)

        # Grupo 2: Contenido e Integridad
        self._grp_details = Adw.PreferencesGroup(title="2. Contenido del Respaldo")
        content.append(self._grp_details)

        self._row_date = Adw.ActionRow(title="Fecha de creación", subtitle="—")
        self._grp_details.add(self._row_date)

        self._row_entries = Adw.ActionRow(title="Entradas empaquetadas", subtitle="—")
        self._grp_details.add(self._row_entries)

        self._row_items = Adw.ActionRow(
            title="Inventario de datos",
            subtitle="Proyectos, documentos del Cerebro, propuestas, tareas y plantillas",
        )
        self._grp_details.add(self._row_items)

        # Grupo 3: Advertencia y Confirmación
        grp_confirm = Adw.PreferencesGroup(title="3. Confirmación Obligatoria")
        content.append(grp_confirm)

        close_warn_row = Adw.ActionRow(
            title="IA Workspace debe estar completamente cerrado",
            subtitle="Cierra la aplicación principal y cualquier terminal activa antes de continuar. "
                     "La restauración requiere acceso exclusivo y será rechazada si la aplicación sigue en ejecución.",
        )
        close_warn_row.add_prefix(Gtk.Image.new_from_icon_name("security-high-symbolic"))
        grp_confirm.add(close_warn_row)

        warn_row = Adw.ActionRow(
            title="Sustitución irreversible de datos activos",
            subtitle="Se reemplazarán la base SQLite y las notas del Cerebro Compartido. "
                     "El código de proyectos externos y las sesiones de WebKit se conservan intactos.",
        )
        warn_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        warn_row.add_css_class("warning")
        grp_confirm.add(warn_row)

        self._confirm_row = Adw.ActionRow(
            title="Escribe RESTAURAR para habilitar la operación",
            subtitle="La confirmación distingue entre mayúsculas y minúsculas",
        )
        self._entry_confirm = Gtk.Entry(
            placeholder_text="RESTAURAR",
            valign=Gtk.Align.CENTER,
            max_length=20,
        )
        self._entry_confirm.set_width_chars(14)
        self._entry_confirm.connect("changed", self._on_confirm_text_changed)
        self._entry_confirm.set_sensitive(False)
        self._confirm_row.add_suffix(self._entry_confirm)
        grp_confirm.add(self._confirm_row)

        # Botones de acción inferiores
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        action_box.set_halign(Gtk.Align.END)
        action_box.set_margin_top(8)

        self._btn_cancel = Gtk.Button(label="Salir")
        self._btn_cancel.connect("clicked", lambda _b: self.close())
        action_box.append(self._btn_cancel)

        self._btn_restore = Gtk.Button(label="Restaurar Respaldo")
        self._btn_restore.add_css_class("destructive-action")
        self._btn_restore.set_sensitive(False)
        self._btn_restore.connect("clicked", self._on_restore_clicked)
        action_box.append(self._btn_restore)

        content.append(action_box)

        # Área de progreso y resultado final
        self._progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._progress_box.set_visible(False)
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_pulse_step(0.1)
        self._progress_label = Gtk.Label(label="Procesando…", xalign=0)
        self._progress_label.add_css_class("dim-label")
        self._progress_box.append(self._progress_label)
        self._progress_box.append(self._progress_bar)
        content.append(self._progress_box)

    def _select_most_recent_backup(self) -> None:
        """Busca el respaldo más reciente en backups_dir si existe."""
        if not self.paths.backups_dir.is_dir():
            return
        files = [
            f for f in self.paths.backups_dir.glob("*.tar.gz")
            if f.is_file() and not f.is_symlink() and not f.name.startswith(".")
        ]
        if files:
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            self._set_backup_file(files[0])

    def _set_backup_file(self, path: Path) -> Path | None:
        self._selected_backup = path
        st = path.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=TZ_TEGUCIGALPA).strftime("%d/%m/%Y %H:%M %Z")
        self._file_row.set_title(path.name)
        self._file_row.set_subtitle(f"Tamaño: {format_size(st.st_size)} · Modificado: {mtime}")
        self._btn_validate.set_sensitive(True)
        self._start_validation()
        return path

    def _on_browse_clicked(self, _btn: Gtk.Button) -> None:
        """Abre un diálogo nativo Gtk.FileDialog para seleccionar el respaldo .tar.gz."""
        dialog = Gtk.FileDialog(title="Seleccionar Respaldo de IA Workspace")
        filter_tar = Gtk.FileFilter()
        filter_tar.set_name("Respaldos de IA Workspace (*.tar.gz)")
        filter_tar.add_pattern("*.tar.gz")

        filter_all = Gtk.FileFilter()
        filter_all.set_name("Todos los archivos")
        filter_all.add_pattern("*")

        filters = gi.repository.Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_tar)
        filters.append(filter_all)
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_tar)

        if self.paths.backups_dir.is_dir():
            dialog.set_initial_folder(gi.repository.Gio.File.new_for_path(str(self.paths.backups_dir)))

        def on_open_finish(source: Gtk.FileDialog, res: gi.repository.Gio.AsyncResult) -> None:
            try:
                gfile = source.open_finish(res)
                if gfile:
                    chosen_path = Path(gfile.get_path()).resolve()
                    self._set_backup_file(chosen_path)
            except Exception:
                # El usuario canceló la selección
                pass

        dialog.open(self, None, on_open_finish)

    def _start_validation(self) -> None:
        if not self._selected_backup or self._is_working:
            return

        self._validation_report = None
        self._entry_confirm.set_text("")
        self._entry_confirm.set_sensitive(False)
        self._btn_restore.set_sensitive(False)

        w.clear_box(self._val_status_holder)
        self._val_status_holder.append(w.status_pill("Verificando…", "working", spinning=True))

        self._row_date.set_subtitle("Verificando…")
        self._row_entries.set_subtitle("Verificando…")
        self._row_items.set_subtitle("Verificando…")

        backup_path = self._selected_backup

        def bg_worker() -> None:
            try:
                report = self.restore_service.validate_backup(backup_path)
                GLib.idle_add(self._on_validation_success, report)
            except (RestoreValidationError, RestoreSecurityError) as exc:
                GLib.idle_add(self._on_validation_failure, str(exc))
            except Exception as exc:
                GLib.idle_add(self._on_validation_failure, f"Error inesperado: {exc}")

        threading.Thread(target=bg_worker, daemon=True).start()

    def _on_validation_success(self, report: BackupValidationReport) -> None:
        self._validation_report = report
        w.clear_box(self._val_status_holder)
        self._val_status_holder.append(w.status_pill("Válido (SHA-256 e integridad ok)", "done"))

        self._row_date.set_subtitle(_format_local_datetime(report.created_at))
        files_label = _pluralize(report.total_files, "archivo", "archivos")
        self._row_entries.set_subtitle(
            f"{files_label} ({format_size(report.total_bytes)} descomprimidos) "
            f"· Esquema v{report.schema_version}"
        )

        stats = report.stats or {}
        p_cnt = stats.get("projects_count", 0)
        d_cnt = stats.get("documents_count", 0)
        pr_cnt = stats.get("proposals_count", 0)
        t_cnt = stats.get("tasks_count", 0)
        pt_cnt = stats.get("templates_count", 0)

        p_str = _pluralize(p_cnt, "proyecto", "proyectos")
        d_str = _pluralize(d_cnt, "documento Cerebro", "documentos Cerebro")
        pr_str = _pluralize(pr_cnt, "propuesta", "propuestas")
        t_str = _pluralize(t_cnt, "tarea", "tareas")
        pt_str = _pluralize(pt_cnt, "plantilla", "plantillas")

        self._row_items.set_subtitle(
            f"{p_str} · {d_str} · {pr_str} · {t_str} · {pt_str}"
        )

        self._entry_confirm.set_sensitive(True)
        self._on_confirm_text_changed(self._entry_confirm)

    def _on_validation_failure(self, error_msg: str) -> None:
        self._validation_report = None
        w.clear_box(self._val_status_holder)
        self._val_status_holder.append(w.status_pill("Inválido", "error"))

        self._row_date.set_subtitle("—")
        self._row_entries.set_subtitle("—")
        self._row_items.set_subtitle("El archivo está corrupto o alterado")

        self._entry_confirm.set_text("")
        self._entry_confirm.set_sensitive(False)
        self._btn_restore.set_sensitive(False)

        self._show_error_dialog("Respaldo Inválido", error_msg)

    def _on_confirm_text_changed(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        is_ready = (
            text == "RESTAURAR"
            and self._validation_report is not None
            and not self._is_working
        )
        self._btn_restore.set_sensitive(is_ready)

    def _on_restore_clicked(self, _btn: Gtk.Button) -> None:
        if not self._selected_backup or not self._validation_report or self._is_working:
            return

        if self._entry_confirm.get_text().strip() != "RESTAURAR":
            return

        self._set_working_state(True)
        backup_path = self._selected_backup

        def bg_restore() -> None:
            try:
                report = self.restore_service.restore_backup(backup_path, confirmed=True)
                GLib.idle_add(self._on_restore_success, report)
            except RestoreExclusivityError as exc:
                GLib.idle_add(self._on_restore_exclusivity_error, str(exc))
            except (RestoreValidationError, RestoreSecurityError, RestoreError) as exc:
                GLib.idle_add(self._on_restore_failure, str(exc))
            except Exception as exc:
                GLib.idle_add(self._on_restore_failure, f"Error inesperado durante la restauración: {exc}")

        threading.Thread(target=bg_restore, daemon=True).start()

    def _set_working_state(self, working: bool) -> None:
        self._is_working = working
        self._btn_browse.set_sensitive(not working)
        self._btn_validate.set_sensitive(not working and self._selected_backup is not None)
        self._entry_confirm.set_sensitive(not working and self._validation_report is not None)
        self._btn_restore.set_sensitive(not working and self._entry_confirm.get_text().strip() == "RESTAURAR")
        self._btn_cancel.set_sensitive(not working)

        self._progress_box.set_visible(working)
        if working:
            self._progress_label.set_text("Restaurando datos de forma transaccional…")
            self._progress_bar.pulse()
        else:
            self._progress_bar.set_fraction(0.0)

    def _on_restore_success(self, report: BackupValidationReport) -> None:
        self._set_working_state(False)
        self._entry_confirm.set_text("")

        stats = report.stats or {}
        msg = (
            f"La base de datos SQLite y los documentos del Cerebro Compartido han sido "
            f"restaurados con éxito a partir de '{self._selected_backup.name if self._selected_backup else ''}'.\n\n"
            f"• Proyectos: {stats.get('projects_count', 0)}\n"
            f"• Documentos del Cerebro: {stats.get('documents_count', 0)}\n"
            f"• Tareas: {stats.get('tasks_count', 0)}\n"
            f"• Plantillas: {stats.get('templates_count', 0)}\n\n"
            "Ya puedes iniciar IA Workspace normalmente."
        )

        dialog = Adw.AlertDialog(
            heading="Restauración Completada",
            body=msg,
        )
        dialog.add_response("ok", "Cerrar")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda _d, _r: self.close())
        dialog.present(self)

    def _on_restore_exclusivity_error(self, error_msg: str) -> None:
        self._set_working_state(False)
        msg = (
            f"{error_msg}\n\n"
            "Instrucciones:\n"
            "1. Cierra completamente la aplicación IA Workspace.\n"
            "2. Asegúrate de que no haya terminales ni procesos secundarios activos.\n"
            "3. Pulsa 'Reintentar' cuando la aplicación esté cerrada."
        )
        dialog = Adw.AlertDialog(
            heading="Aplicación en Ejecución",
            body=msg,
        )
        dialog.add_response("close", "Entendido")
        dialog.set_default_response("close")
        dialog.present(self)

    def _on_restore_failure(self, error_msg: str) -> None:
        self._set_working_state(False)
        msg = (
            f"Ocurrió un error al restaurar el respaldo:\n\n{error_msg}\n\n"
            "Se ha realizado un rollback automático al estado anterior; tus datos activos "
            "no han sufrido modificaciones."
        )
        self._show_error_dialog("Error en Restauración", msg)

    def _show_error_dialog(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
        )
        dialog.add_response("ok", "Aceptar")
        dialog.set_default_response("ok")
        dialog.present(self)

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if self._is_working:
            # Prevenir cierre accidental si la restauración está en curso
            toast = Adw.Toast(title="La restauración está en curso. No cierres la ventana.")
            self._toast_overlay.add_toast(toast)
            return True
        return False


class RestoreAssistantApplication(Adw.Application):
    """Aplicación Libadwaita ligera e independiente para recuperación offline."""

    def __init__(self, paths: AppPaths, initial_backup: Path | None = None) -> None:
        super().__init__(
            application_id=APP_RESTORE_ID,
            flags=gi.repository.Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.paths = paths
        self.initial_backup = initial_backup
        self.window: RestoreAssistantWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._setup_icon()

    def _setup_icon(self) -> None:
        display = Gdk.Display.get_default()
        source_icons = Path(__file__).resolve().parents[3] / "data" / "icons"
        if display is not None and (source_icons / "hicolor").is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(source_icons))
        Gtk.Window.set_default_icon_name(APP_RESTORE_ID)

    def do_activate(self) -> None:
        theme.setup_theme()
        if not self.window:
            self.window = RestoreAssistantWindow(
                self,
                paths=self.paths,
                initial_backup=self.initial_backup,
            )
        self.window.present()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="iaw-restore-gui",
        description="Asistente gráfico independiente de recuperación de respaldos de IA Workspace.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Ruta al directorio de datos de IA Workspace",
    )
    parser.add_argument(
        "backup",
        nargs="?",
        default=None,
        help="Archivo opcional .tar.gz para cargar al inicio",
    )

    args = parser.parse_args(argv)

    if args.data_dir:
        paths = AppPaths(data_dir=args.data_dir.resolve())
    else:
        paths = resolve_paths()

    initial_backup: Path | None = None
    if args.backup:
        p = Path(args.backup).expanduser()
        if not p.is_file() and (paths.backups_dir / args.backup).is_file():
            initial_backup = (paths.backups_dir / args.backup).resolve()
        elif p.is_file():
            initial_backup = p.resolve()

    app = RestoreAssistantApplication(paths=paths, initial_backup=initial_backup)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
