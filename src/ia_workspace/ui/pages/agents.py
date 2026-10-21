"""D. Centro de agentes — sesiones de todos los proyectos."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ia_workspace.sessions.models import SessionState
from ia_workspace.ui import widgets as w
from ia_workspace.ui.pages.base import Navigator, Page, scrolled_clamp

ATTENTION = (SessionState.WAITING_APPROVAL, SessionState.FAILED, SessionState.ERROR, SessionState.DISCONNECTED)


class AgentsPage(Page):
    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, "Centro de agentes")
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._stack = Gtk.Stack()
        self._stack.add_named(scrolled_clamp(self._body), "list")
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="system-run-symbolic",
                title="Ningún agente en ejecución",
                description=(
                    "Aquí verás las sesiones de Claude Code, Codex y Google Antigravity de todos "
                    "tus proyectos, aunque estén trabajando en segundo plano."
                ),
            ),
            "empty",
        )
        self.set_content(self._stack)

    def refresh(self) -> None:
        sessions = self.ctx.sessions.sessions()
        active = [s for s in sessions if s.state.is_active]
        attention = [s for s in sessions if s.state in ATTENTION]
        parts = [f"{len(active)} activa{'s' if len(active) != 1 else ''}"]
        if attention:
            parts.append(f"{len(attention)} requiere{'n' if len(attention) != 1 else ''} atención")
        self.window_title.set_subtitle(" · ".join(parts) if sessions else "")
        if not sessions:
            self._stack.set_visible_child_name("empty")
            return
        self._stack.set_visible_child_name("list")
        w.clear_box(self._body)
        for project in self.ctx.projects.list():
            mine = [s for s in sessions if s.project_id == project.id]
            if not mine:
                continue
            go = Gtk.Button(label="Ir al workspace")
            go.add_css_class("flat")
            go.connect("clicked", lambda _b, pid=project.id: self.nav.open_project(pid))
            self._body.append(w.group_header(project.name, w.short_path(project.path), go))
            box = w.boxed_list()
            for s in mine:
                row = Adw.ActionRow(title=s.spec.title or s.spec.kind.label, subtitle=s.spec.kind.label)
                row.add_prefix(w.tool_chip(s.spec.kind))
                row.add_suffix(w.session_pill(s.state, w.is_live_terminal(self.ctx.sessions, s.id)))
                if s.state.is_active:
                    stop = Gtk.Button(icon_name="media-playback-stop-symbolic", valign=Gtk.Align.CENTER,
                                      tooltip_text="Detener sesión")
                    stop.add_css_class("flat")
                    stop.connect("clicked", lambda _b, sid=s.id: self.ctx.sessions.stop(sid))
                    row.add_suffix(stop)
                box.append(row)
            self._body.append(box)
