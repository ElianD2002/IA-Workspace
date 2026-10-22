"""D. Centro de agentes — autonomía, autorizaciones y sesiones de todos los proyectos."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import SELECTABLE_AGENTS, AgentKind, AutonomyMode, Project
from ia_workspace.sessions.models import SessionState
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp

ATTENTION = (SessionState.WAITING_APPROVAL, SessionState.FAILED, SessionState.ERROR, SessionState.DISCONNECTED)

MODE_LABELS = [
    "Normal (predeterminado)",
    "Automático",
    "Sin confirmaciones (avanzado)",
]
MODE_VALUES = [
    AutonomyMode.NORMAL,
    AutonomyMode.AUTO,
    AutonomyMode.DANGER,
]


def autonomy_summary(kind: AgentKind, mode: AutonomyMode) -> str:
    if mode is AutonomyMode.NORMAL:
        return "Políticas predeterminadas de la CLI. Solicita confirmaciones habituales."
    if mode is AutonomyMode.AUTO:
        if kind is AgentKind.CLAUDE_CODE:
            return "Modo automático (--permission-mode auto). Reduce confirmaciones repetitivas."
        if kind is AgentKind.CODEX:
            return (
                "Sandbox workspace-write (--sandbox workspace-write --ask-for-approval never). "
                "Acciones bloqueadas por el sandbox pueden fallar sin solicitar permiso."
            )
        if kind is AgentKind.ANTIGRAVITY:
            return "Modo accept-edits (--mode=accept-edits). Los comandos de terminal pueden seguir solicitando autorización."
    if mode is AutonomyMode.DANGER:
        if kind is AgentKind.CLAUDE_CODE:
            flags = "--dangerously-skip-permissions"
        elif kind is AgentKind.CODEX:
            flags = "--dangerously-bypass-approvals-and-sandbox"
        elif kind is AgentKind.ANTIGRAVITY:
            flags = "--mode=accept-edits --dangerously-skip-permissions"
        else:
            flags = ""
        return (
            f"Sin confirmaciones ni sandbox ({flags}). "
            "Permite comandos destructivos, modificar archivos externos y operaciones Git sin autorización previa."
        )
    return ""


class AgentsPage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Centro de agentes")
        self._project: Project | None = None
        self._project_ids: list[int] = []
        self._project_model = Gtk.StringList()
        self._syncing = False
        self._last_dialog: Adw.AlertDialog | None = None

        self._project_picker = Gtk.DropDown(
            model=self._project_model,
            tooltip_text="Proyecto para configurar autonomía",
            visible=False,
        )
        self._project_picker.connect("notify::selected", self._on_project_picked)
        self.header.pack_start(self._project_picker)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self._autonomy_group = Adw.PreferencesGroup(
            title="Nivel de autonomía y autorización",
            description="Configura el nivel de confirmaciones de cada agente para el proyecto seleccionado.",
        )
        self._autonomy_rows: dict[AgentKind, Adw.ComboRow] = {}
        for kind in SELECTABLE_AGENTS:
            model = Gtk.StringList.new(MODE_LABELS)
            combo = Adw.ComboRow(
                title=kind.label,
                subtitle=autonomy_summary(kind, AutonomyMode.NORMAL),
                model=model,
            )
            combo.add_prefix(w.tool_chip(kind))
            combo.connect("notify::selected", self._on_mode_changed, kind)
            self._autonomy_group.add(combo)
            self._autonomy_rows[kind] = combo

        self._sessions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        from ia_workspace.ui.quota_panel import SubscriptionQuotasSection

        self._updating_quota_kinds: set[AgentKind] = set()
        self._quotas_section = SubscriptionQuotasSection(
            ctx=self.ctx,
            on_refresh_requested=self._on_refresh_quotas_requested,
        )

        self._body.append(self._quotas_section)
        self._body.append(self._autonomy_group)
        self._body.append(self._sessions_box)

        self._stack = Gtk.Stack()
        self._stack.add_named(scrolled_clamp(self._body), "main")
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="system-run-symbolic",
                title="No hay proyectos registrados",
                description="Registra o crea un proyecto para configurar la autonomía de sus agentes.",
            ),
            "empty",
        )
        self.set_content(self._stack)

    def refresh(self) -> None:
        self._render_quotas()
        projects = self.ctx.projects.list()
        if not projects:
            self._project = None
            self._project_picker.set_visible(False)
            self._stack.set_visible_child_name("empty")
            self.window_title.set_subtitle("")
            return

        self._stack.set_visible_child_name("main")

        preferred = (
            self._project.id
            if self._project is not None
            else (self.ctx.workspace.active_project_id or self.ctx.workspace.last_project_id)
        )
        by_id = {p.id: p for p in projects}
        self._project = by_id.get(preferred) or projects[0]

        self._syncing = True
        try:
            self._project_ids = [p.id for p in projects]
            self._project_model.splice(0, self._project_model.get_n_items(), [p.name for p in projects])
            if self._project is not None:
                self._project_picker.set_selected(self._project_ids.index(self._project.id))
            self._project_picker.set_visible(len(self._project_ids) > 1)
        finally:
            self._syncing = False

        self._render_autonomy()
        self._render_sessions()

    def _render_quotas(self) -> None:
        if not getattr(self.ctx, "quotas", None):
            return
        from pathlib import Path
        snapshots = self.ctx.quotas.get_all_snapshots()
        project_path = Path(self._project.path) if self._project else None
        self._quotas_section.update_snapshots(
            snapshots,
            updating_kinds=self._updating_quota_kinds,
            project_path=project_path,
        )

    def _on_refresh_quotas_requested(self) -> None:
        if not getattr(self.ctx, "quotas", None):
            return
        from ia_workspace.quota.models import QuotaStatus

        targets = [AgentKind.CODEX, AgentKind.ANTIGRAVITY]
        self._updating_quota_kinds = set(targets)
        self._quotas_section.set_updating(True)
        self._render_quotas()

        pending = set(targets)
        failed: set[AgentKind] = set()

        def on_done(kind: AgentKind, snap: object) -> None:
            # El servicio llama a esto en el hilo principal, una vez por proveedor y siempre
            # (también tras un error o un plazo agotado): el botón vuelve a estar disponible.
            pending.discard(kind)
            self._updating_quota_kinds.discard(kind)
            if getattr(snap, "status", None) in (QuotaStatus.ERROR, QuotaStatus.STALE):
                failed.add(kind)
            try:
                self._render_quotas()
            finally:
                if not pending:
                    self._quotas_section.set_updating(False)
                    if failed:
                        names = " y ".join(k.label for k in targets if k in failed)
                        self.nav.toast(f"No se pudieron actualizar las cuotas de {names}.")
                    else:
                        self.nav.toast("Cuotas de suscripción actualizadas.")

        for kind in targets:
            self.ctx.quotas.refresh_provider(
                kind,
                force=True,
                callback=lambda snap, k=kind: on_done(k, snap),
            )

    def _on_project_picked(self, *_args: object) -> None:
        if self._syncing:
            return
        idx = self._project_picker.get_selected()
        if 0 <= idx < len(self._project_ids):
            pid = self._project_ids[idx]
            self._project = self.ctx.projects.get(pid)
            self._render_autonomy()

    def _render_autonomy(self) -> None:
        if self._project is None:
            return

        self._autonomy_group.set_description(
            f"Configuración para «{self._project.name}». Se aplicará a las nuevas sesiones que inicies."
        )

        modes = self.ctx.autonomy.get_modes_for_project(self._project.id)

        self._syncing = True
        try:
            for kind in SELECTABLE_AGENTS:
                current_mode = modes.get(kind, AutonomyMode.NORMAL)
                combo = self._autonomy_rows[kind]
                combo.set_selected(MODE_VALUES.index(current_mode))
                combo.set_subtitle(autonomy_summary(kind, current_mode))
        finally:
            self._syncing = False

    def _on_mode_changed(self, combo: Adw.ComboRow, _pspec: object, kind: AgentKind) -> None:
        if self._syncing or self._project is None:
            return
        idx = combo.get_selected()
        if not (0 <= idx < len(MODE_VALUES)):
            return
        new_mode = MODE_VALUES[idx]
        old_mode = self.ctx.autonomy.get_mode(self._project.id, kind)
        if new_mode == old_mode:
            return

        if new_mode is AutonomyMode.DANGER:
            self._confirm_danger_mode(kind, combo, old_mode)
        else:
            self.ctx.autonomy.set_mode(self._project.id, kind, new_mode)
            combo.set_subtitle(autonomy_summary(kind, new_mode))
            self.nav.toast(f"{kind.label}: modo {new_mode.label.lower()} guardado.")

    def _confirm_danger_mode(self, kind: AgentKind, combo: Adw.ComboRow, old_mode: AutonomyMode) -> None:
        if self._project is None:
            return

        alert = Adw.AlertDialog(
            heading=f"¿Activar modo sin confirmaciones para {kind.label}?",
            body=(
                f"Advertencia de seguridad: Este modo omite todas las confirmaciones y las restricciones del sandbox para «{self._project.name}».\n\n"
                "Puede permitir al agente ejecutar comandos destructivos, modificar archivos fuera de la carpeta del proyecto y realizar operaciones de Git sin solicitar tu aprobación previa.\n\n"
                "Úsalo únicamente en entornos y proyectos de total confianza."
            ),
        )
        alert.add_response("cancel", "Cancelar")
        alert.add_response("confirm", "Activar modo avanzado")
        alert.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        self._last_dialog = alert

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            if response == "confirm":
                if self._project is not None:
                    self.ctx.autonomy.set_mode(self._project.id, kind, AutonomyMode.DANGER)
                    combo.set_subtitle(autonomy_summary(kind, AutonomyMode.DANGER))
                    self.nav.toast(f"Modo sin confirmaciones activado para {kind.label}.")
            else:
                self._syncing = True
                try:
                    old_idx = MODE_VALUES.index(old_mode) if old_mode in MODE_VALUES else 0
                    combo.set_selected(old_idx)
                finally:
                    self._syncing = False

        alert.connect("response", on_response)
        root = self.get_root()
        if root is not None:
            alert.present(root)

    def _render_sessions(self) -> None:
        w.clear_box(self._sessions_box)
        sessions = self.ctx.sessions.sessions()
        active = [s for s in sessions if s.state.is_active]
        attention = [s for s in sessions if s.state in ATTENTION]
        parts = [f"{len(active)} activa{'s' if len(active) != 1 else ''}"]
        if attention:
            parts.append(f"{len(attention)} requiere{'n' if len(attention) != 1 else ''} atención")
        self.window_title.set_subtitle(" · ".join(parts) if sessions else "")

        self._sessions_box.append(w.group_header("Sesiones en ejecución", "Agentes activos de todos los proyectos"))

        if not sessions:
            row = Adw.ActionRow(
                title="Ningún agente en ejecución",
                subtitle="Las sesiones activas de Claude Code, Codex y Google Antigravity aparecerán aquí.",
            )
            row.set_sensitive(False)
            box = w.boxed_list()
            box.append(row)
            self._sessions_box.append(box)
            return

        for project in self.ctx.projects.list():
            mine = [s for s in sessions if s.project_id == project.id]
            if not mine:
                continue
            go = Gtk.Button(label="Ir al workspace", valign=Gtk.Align.CENTER)
            go.add_css_class("flat")
            go.connect("clicked", lambda _b, pid=project.id: self.nav.open_project(pid))
            self._sessions_box.append(w.group_header(project.name, w.short_path(project.path), go))

            box = w.boxed_list()
            for s in mine:
                row = Adw.ActionRow(title=s.spec.title or s.spec.kind.label, subtitle=s.spec.kind.label)
                row.add_prefix(w.tool_chip(s.spec.kind))
                if s.spec.autonomy_mode is not AutonomyMode.NORMAL:
                    style = "warning" if s.spec.autonomy_mode is AutonomyMode.DANGER else "idle"
                    row.add_suffix(w.status_pill(s.spec.autonomy_mode.label, style))
                row.add_suffix(w.session_pill(s.state, w.is_live_terminal(self.ctx.sessions, s.id)))
                if s.state.is_active:
                    stop = Gtk.Button(
                        icon_name="media-playback-stop-symbolic",
                        valign=Gtk.Align.CENTER,
                        tooltip_text="Detener sesión",
                    )
                    stop.add_css_class("flat")
                    stop.connect("clicked", lambda _b, sid=s.id: self.ctx.sessions.stop(sid))
                    row.add_suffix(stop)
                box.append(row)
            self._sessions_box.append(box)
