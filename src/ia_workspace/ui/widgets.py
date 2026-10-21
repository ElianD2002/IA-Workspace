"""Widgets pequeños y reutilizables que reproducen piezas del prototipo."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gi.repository import Adw, Gtk, Pango

from ia_workspace.domain.models import AgentKind, Project
from ia_workspace.sessions.models import SessionState

# Estado de sesión → clase visual del prototipo (working, wait, done, error, off, idle).
STATE_STYLE = {
    SessionState.STARTING: "working",
    SessionState.WORKING: "working",
    SessionState.IDLE: "idle",
    SessionState.WAITING_APPROVAL: "wait",
    SessionState.STOPPING: "wait",
    SessionState.DONE: "done",
    SessionState.ERROR: "error",
    SessionState.DISCONNECTED: "off",
    SessionState.STOPPED: "off",
}


def project_badge(project: Project, size: str = "") -> Gtk.Label:
    label = Gtk.Label(label=project.initials, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    label.add_css_class("proj-badge")
    label.add_css_class(f"proj-c{project.color_index % 8}")
    if size:
        label.add_css_class(size)
    label.set_tooltip_text(project.name)
    return label


def tool_chip(kind: AgentKind | str, mono: str | None = None, tooltip: str | None = None) -> Gtk.Label:
    key = kind.value if isinstance(kind, AgentKind) else kind
    text = mono or (kind.mono if isinstance(kind, AgentKind) else key.upper())
    chip = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    chip.add_css_class("tool-chip")
    chip.add_css_class(f"tool-{key}")
    tip = tooltip or (kind.label if isinstance(kind, AgentKind) else None)
    if tip:
        chip.set_tooltip_text(tip)
    return chip


def status_pill(text: str, style: str, spinning: bool = False) -> Gtk.Box:
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    box.add_css_class("status-pill")
    box.add_css_class(style)
    if spinning:
        spinner = Adw.Spinner()
        spinner.set_size_request(12, 12)
        box.append(spinner)
    box.append(Gtk.Label(label=text))
    return box


def session_pill(state: SessionState) -> Gtk.Box:
    return status_pill(state.label, STATE_STYLE[state], spinning=state is SessionState.WORKING)


def status_dot(style: str, tooltip: str | None = None) -> Gtk.Box:
    dot = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
    dot.add_css_class("status-dot")
    dot.add_css_class(style)
    if tooltip:
        dot.set_tooltip_text(tooltip)
    return dot


def caption(text: str, dim: bool = True, mono: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, ellipsize=Pango.EllipsizeMode.END)
    label.add_css_class("caption")
    if dim:
        label.add_css_class("dim-label")
    if mono:
        label.add_css_class("mono")
    return label


def group_header(title: str, subtitle: str = "", action: Gtk.Widget | None = None) -> Gtk.Box:
    box = Gtk.Box(spacing=12, margin_top=26, margin_bottom=10)
    heading = Gtk.Label(label=title, xalign=0)
    heading.add_css_class("group-title")
    box.append(heading)
    if subtitle:
        sub = caption(subtitle)
        sub.set_hexpand(True)
        box.append(sub)
    else:
        box.append(Gtk.Box(hexpand=True))
    if action is not None:
        box.append(action)
    return box


def boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    return listbox


def info_row(text: str, icon: str = "", dim: bool = True) -> Gtk.ListBoxRow:
    box = Gtk.Box(spacing=12, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
    if icon:
        box.append(Gtk.Image.new_from_icon_name(icon))
    label = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True)
    if dim:
        label.add_css_class("dim-label")
    box.append(label)
    row = Gtk.ListBoxRow(activatable=False, child=box)
    return row


def short_path(path: str) -> str:
    """Abrevia la carpeta personal con «~», como en el prototipo."""
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def relative_time(moment: datetime | None) -> str:
    if moment is None:
        return "Nunca"
    now = datetime.now(UTC)
    delta = now - (moment if moment.tzinfo else moment.replace(tzinfo=UTC))
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Ahora"
    if seconds < 3600:
        return f"Hace {seconds // 60} min"
    local = moment.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return f"Hoy, {local:%H:%M}"
    if seconds < 2 * 86400:
        return "Ayer"
    months = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
    return f"{local.day} {months[local.month - 1]}"


def clear_box(box: Gtk.Box | Gtk.ListBox | Gtk.FlowBox) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt
