"""Panel visual de consumo de suscripciones de IA (fase 5B.2).

Muestra el estado, ventanas de límite, porcentajes utilizados y restantes,
fechas de restablecimiento en hora local (America/Tegucigalpa) y advertencias
preventivas cuando el consumo alcanza el 80% o 95%.
No ejecuta inferencias ni altera sesiones activas.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gi.repository import Adw, Gtk

from ia_workspace.domain.models import AgentKind
from ia_workspace.quota.claude import detect_claude_statusline_conflict
from ia_workspace.quota.models import (
    ProviderQuotaSnapshot,
    QuotaGroup,
    QuotaStatus,
    QuotaWindow,
)
from ia_workspace.ui import widgets as w

TZ_TEGUCIGALPA = ZoneInfo("America/Tegucigalpa")


def format_local_datetime(dt: datetime | None) -> str:
    """Convierte un timestamp UTC a cadena legible en America/Tegucigalpa."""
    if dt is None:
        return "Sin mediciones"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TZ_TEGUCIGALPA)
    return local.strftime("%d/%m/%Y %H:%M %Z")


def format_reset_time(resets_at: datetime | None) -> str:
    """Formatea la fecha de restablecimiento en America/Tegucigalpa con tiempo relativo."""
    if resets_at is None:
        return ""
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = int((resets_at - now).total_seconds())
    local_str = resets_at.astimezone(TZ_TEGUCIGALPA).strftime("%d/%m/%Y %H:%M %Z")

    if secs <= 0:
        return f"Restablecido ({local_str})"
    hours = secs // 3600
    mins = (secs % 3600) // 60
    if hours >= 24:
        days = hours // 24
        rem_hours = hours % 24
        rel = f"en {days}d {rem_hours}h"
    elif hours > 0:
        rel = f"en {hours}h {mins}m"
    else:
        rel = f"en {mins}m"
    return f"Próximo restablecimiento: {local_str} ({rel})"


def format_window_duration(minutes: int | None, fallback_name: str = "") -> str:
    """Devuelve una etiqueta amigable para la duración de la ventana."""
    if minutes == 300:
        return "5 horas"
    if minutes == 10080:
        return "Semanal (7 días)"
    if minutes is not None:
        if minutes >= 1440 and minutes % 1440 == 0:
            return f"{minutes // 1440} días"
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes // 60} horas"
        return f"{minutes} minutos"
    return fallback_name


def status_pill_for_quota(status: QuotaStatus, is_updating: bool = False) -> Gtk.Box:
    """Genera la píldora de estado con el estilo visual correspondiente."""
    if is_updating:
        return w.status_pill("Actualizando...", "working", spinning=True)
    if status is QuotaStatus.AVAILABLE:
        return w.status_pill("Disponible", "done")
    if status is QuotaStatus.STALE:
        return w.status_pill("Desactualizado", "wait")
    if status is QuotaStatus.ERROR:
        return w.status_pill("Error de consulta", "error")
    if status is QuotaStatus.PENDING:
        return w.status_pill("Pendiente", "wait")
    return w.status_pill("Sin datos", "idle")


class ProviderQuotaCard(Gtk.Box):
    """Tarjeta individual de cuotas de un proveedor (Claude, Codex o Antigravity)."""

    def __init__(self, kind: AgentKind) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.kind = kind
        self.add_css_class("card")
        self.add_css_class("quota-card")
        self.set_margin_top(4)
        self.set_margin_bottom(6)
        self.set_margin_start(2)
        self.set_margin_end(2)

        # 1. Cabecera
        self._header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._header.append(w.tool_chip(self.kind))

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self._title_label = Gtk.Label(label=self.kind.label, xalign=0)
        self._title_label.add_css_class("heading")
        self._plan_label = Gtk.Label(label="", xalign=0)
        self._plan_label.add_css_class("caption")
        self._plan_label.add_css_class("dim-label")
        title_box.append(self._title_label)
        title_box.append(self._plan_label)
        self._header.append(title_box)

        self._status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._header.append(self._status_box)
        self.append(self._header)

        # 2. Aviso / Incompatibilidad / Detalle
        self._notice_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._notice_label = Gtk.Label(label="", xalign=0, wrap=True)
        self._notice_label.add_css_class("caption")
        self._notice_box.append(self._notice_label)
        self._notice_box.set_visible(False)
        self.append(self._notice_box)

        # 3. Contenedor de grupos y ventanas
        self._groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.append(self._groups_box)

        # 4. Pie de tarjeta (última actualización)
        self._footer_label = Gtk.Label(label="", xalign=0)
        self._footer_label.add_css_class("caption")
        self._footer_label.add_css_class("dim-label")
        self.append(self._footer_label)

    def update(
        self,
        snapshot: ProviderQuotaSnapshot,
        is_updating: bool = False,
        conflict_reason: str | None = None,
    ) -> None:
        """Actualiza todos los elementos visuales de la tarjeta."""
        # Plan: solo se muestra cuando proviene de información real confirmada
        raw_plan = snapshot.plan_type.strip()
        if raw_plan:
            if raw_plan.lower() == "plus":
                display_plan = "ChatGPT Plus"
            elif raw_plan.lower() == "team":
                display_plan = "ChatGPT Team"
            elif raw_plan.lower() == "pro" and self.kind is AgentKind.CODEX:
                display_plan = "ChatGPT Pro"
            else:
                display_plan = raw_plan
            self._plan_label.set_text(f"Plan: {display_plan}")
            self._plan_label.set_visible(True)
        else:
            self._plan_label.set_text("")
            self._plan_label.set_visible(False)

        # Píldora de estado
        w.clear_box(self._status_box)
        self._status_box.append(status_pill_for_quota(snapshot.status, is_updating=is_updating))

        # Aviso de conflicto o error
        if conflict_reason:
            self._notice_box.set_visible(True)
            self._notice_label.set_text(f"⚠️ Captura no disponible: {conflict_reason}")
            self._notice_label.remove_css_class("dim-label")
        elif snapshot.status is QuotaStatus.ERROR:
            self._notice_box.set_visible(True)
            self._notice_label.set_text(f"❌ {snapshot.detail or 'Error al consultar cuota'}")
            self._notice_label.remove_css_class("dim-label")
        elif snapshot.status is QuotaStatus.STALE and snapshot.detail:
            self._notice_box.set_visible(True)
            self._notice_label.set_text(f"⚠️ {snapshot.detail}")
            self._notice_label.remove_css_class("dim-label")
        elif snapshot.status is QuotaStatus.NO_DATA and not snapshot.groups:
            self._notice_box.set_visible(True)
            if self.kind is AgentKind.CLAUDE_CODE:
                self._notice_label.set_text(
                    "Sin mediciones registradas todavía. Las cuotas se capturarán automáticamente "
                    "al interactuar con Claude Code en cualquier proyecto."
                )
            else:
                self._notice_label.set_text(
                    "Sin datos de cuota disponibles. Pulsa «Actualizar cuotas» para consultar los límites."
                )
            self._notice_label.add_css_class("dim-label")
        else:
            self._notice_box.set_visible(False)
            self._notice_label.set_text("")

        # Renderizado de grupos
        w.clear_box(self._groups_box)
        has_windows = False
        show_group_titles = len(snapshot.groups) > 1 or self.kind is AgentKind.ANTIGRAVITY

        for group in snapshot.groups:
            if not group.windows:
                continue
            has_windows = True
            group_widget = self._build_group_widget(group, show_title=show_group_titles)
            self._groups_box.append(group_widget)

        if not has_windows and snapshot.status is not QuotaStatus.NO_DATA:
            empty_row = Gtk.Label(label="No se detectaron límites activos en esta cuenta.", xalign=0)
            empty_row.add_css_class("caption")
            empty_row.add_css_class("dim-label")
            self._groups_box.append(empty_row)

        # Pie
        footer_text = f"Última lectura: {format_local_datetime(snapshot.updated_at)}"
        if snapshot.status is QuotaStatus.STALE:
            footer_text += " · (Datos desactualizados, se conservan los valores anteriores)"
        self._footer_label.set_text(footer_text)

    def _build_group_widget(self, group: QuotaGroup, show_title: bool) -> Gtk.Box:
        gbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if show_title:
            gheader = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            gtitle = Gtk.Label(label=group.name, xalign=0)
            gtitle.add_css_class("heading")
            gtitle.add_css_class("caption")
            gheader.append(gtitle)
            if group.description:
                gdesc = Gtk.Label(label=group.description, xalign=0)
                gdesc.add_css_class("caption")
                gdesc.add_css_class("dim-label")
                gheader.append(gdesc)
            gbox.append(gheader)

        for win in group.windows:
            win_box = self._build_window_widget(win)
            gbox.append(win_box)
        return gbox

    def _build_window_widget(self, win: QuotaWindow) -> Gtk.Box:
        wbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        wbox.add_css_class("quota-window-row")

        # Fila superior: Nombre de ventana y porcentaje / advertencias
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        dur = format_window_duration(win.window_minutes, fallback_name=win.name)
        win_title = win.name if win.name and win.name != dur else dur
        name_label = Gtk.Label(label=win_title, xalign=0, hexpand=True)
        name_label.add_css_class("caption")
        top_row.append(name_label)

        used_pct = win.used_percentage
        rem_pct = win.remaining_percentage

        if used_pct is not None:
            pct_str = f"{used_pct:.1f}% usado · {rem_pct:.1f}% restante" if rem_pct is not None else f"{used_pct:.1f}% usado"
            pct_label = Gtk.Label(label=pct_str, xalign=1)
            pct_label.add_css_class("caption")
            top_row.append(pct_label)

            # Advertencias según umbrales (80% consumo elevado, 95% límite próximo)
            if used_pct >= 95.0:
                badge = w.status_pill("Límite próximo", "error")
                badge.set_tooltip_text("Se ha superado el 95% de la cuota de esta ventana.")
                top_row.append(badge)
            elif used_pct >= 80.0:
                badge = w.status_pill("Consumo elevado", "wait")
                badge.set_tooltip_text("Se ha superado el 80% de la cuota de esta ventana.")
                top_row.append(badge)
        else:
            # Nunca representar una métrica ausente como 0%
            pct_label = Gtk.Label(label="—", xalign=1)
            pct_label.add_css_class("caption")
            pct_label.add_css_class("dim-label")
            pct_label.set_tooltip_text("Métrica de porcentaje no provista por el proveedor")
            top_row.append(pct_label)

        wbox.append(top_row)

        # Fila intermedia: Barra de progreso nativa
        pbar = Gtk.ProgressBar()
        pbar.set_margin_top(2)
        pbar.set_margin_bottom(2)

        if used_pct is not None:
            fraction = max(0.0, min(1.0, float(used_pct) / 100.0))
            pbar.set_fraction(fraction)
            if used_pct >= 95.0:
                pbar.add_css_class("error")
            elif used_pct >= 80.0:
                pbar.add_css_class("warning")
        else:
            # Sin porcentaje: no rellenar de forma ficticia
            pbar.set_fraction(0.0)

        wbox.append(pbar)

        # Fila inferior: Próximo restablecimiento
        if win.resets_at:
            reset_str = format_reset_time(win.resets_at)
            if reset_str:
                reset_label = Gtk.Label(label=reset_str, xalign=0)
                reset_label.add_css_class("caption")
                reset_label.add_css_class("dim-label")
                wbox.append(reset_label)

        return wbox


class SubscriptionQuotasSection(Adw.PreferencesGroup):
    """Sección global de consumo de suscripciones en la página Agentes."""

    def __init__(self, ctx: object, on_refresh_requested: Callable[[], None] | None = None) -> None:
        super().__init__(
            title="Consumo de suscripciones",
            description="Límites reales de uso y cuotas de las cuentas oficiales sin consumo de inferencia.",
        )
        self.ctx = ctx
        self._on_refresh_requested = on_refresh_requested
        self._updating_providers: set[AgentKind] = set()

        # Botón de refresco en la cabecera del grupo
        self._refresh_btn = Gtk.Button(label="Actualizar cuotas", icon_name="view-refresh-symbolic")
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.connect("clicked", self._on_btn_clicked)
        self.set_header_suffix(self._refresh_btn)

        # Contenedor vertical de tarjetas
        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self._cards: dict[AgentKind, ProviderQuotaCard] = {}

        for kind in (AgentKind.CLAUDE_CODE, AgentKind.CODEX, AgentKind.ANTIGRAVITY):
            card = ProviderQuotaCard(kind)
            self._cards[kind] = card
            self._cards_box.append(card)

        self.add(self._cards_box)

    def _on_btn_clicked(self, _btn: Gtk.Button) -> None:
        if self._on_refresh_requested:
            self._on_refresh_requested()

    def set_updating(self, updating: bool) -> None:
        """Habilita o deshabilita el botón de refresco y actualiza el indicador visual."""
        self._refresh_btn.set_sensitive(not updating)
        if updating:
            self._refresh_btn.set_label("Actualizando...")
        else:
            self._refresh_btn.set_label("Actualizar cuotas")

    def update_snapshots(
        self,
        snapshots: dict[AgentKind, ProviderQuotaSnapshot],
        updating_kinds: set[AgentKind] | None = None,
        project_path: Path | None = None,
    ) -> None:
        """Actualiza todas las tarjetas con los snapshots recibidos."""
        updating = updating_kinds or set()
        for kind, card in self._cards.items():
            snap = snapshots.get(kind) or ProviderQuotaSnapshot(provider=kind, status=QuotaStatus.NO_DATA)
            conflict_reason: str | None = None
            if kind is AgentKind.CLAUDE_CODE:
                has_conflict, reason = detect_claude_statusline_conflict(project_dir=project_path)
                if has_conflict:
                    conflict_reason = reason
            card.update(snap, is_updating=kind in updating, conflict_reason=conflict_reason)
