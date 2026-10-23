"""Ventana principal: barra lateral + pila de pantallas.

Cada proyecto abierto tiene su propia ``WorkspaceView`` (widgets y estado
independientes), que se conserva en memoria mientras la aplicación vive. Solo una
es visible a la vez; las demás —y sus sesiones— siguen existiendo en segundo plano.
"""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ia_workspace import APP_NAME
from ia_workspace.context import AppContext
from ia_workspace.sessions.models import SessionEvent
from ia_workspace.ui.pages.agents import AgentsPage
from ia_workspace.ui.pages.base import Page
from ia_workspace.ui.pages.history import HistoryPage
from ia_workspace.ui.pages.home import HomePage
from ia_workspace.ui.pages.memory import MemoryPage
from ia_workspace.ui.pages.projects import ProjectsPage
from ia_workspace.ui.pages.tasks import TasksPage
from ia_workspace.ui.pages.workspace import WorkspaceView
from ia_workspace.ui.sidebar import NAV_ITEMS, Sidebar

_SIZE_KEY = "window.size"
_FLUSH_SECONDS = 5


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, ctx: AppContext) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.ctx = ctx
        self._screen = "home"
        self._pages: dict[str, Page] = {}
        self._workspaces: dict[int, WorkspaceView] = {}
        self._restore_size()
        self.set_size_request(360, 480)

        self._split = Adw.OverlaySplitView(
            min_sidebar_width=220, max_sidebar_width=280, sidebar_width_fraction=0.2
        )
        self.sidebar = Sidebar(ctx, self.navigate, self.open_project)
        self._split.set_sidebar(self.sidebar)

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        for name, cls in (
            ("home", HomePage),
            ("projects", ProjectsPage),
            ("agents", AgentsPage),
            ("tasks", TasksPage),
            ("memory", MemoryPage),
            ("history", HistoryPage),
        ):
            page = cls(self)
            self._pages[name] = page
            self._stack.add_named(page, name)
        self._split.set_content(self._stack)

        self._toasts = Adw.ToastOverlay(child=self._split)
        self._root = Gtk.Stack()
        self._root.add_named(self._toasts, "main")
        self.set_content(self._root)

        # Igual que el prototipo: por debajo de ~980 px la barra lateral se superpone.
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 980sp"))
        breakpoint.add_setter(self._split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        self._setup_actions()
        ctx.projects.subscribe(self._on_projects_changed)
        ctx.sessions.subscribe(self._on_session_event)
        self.connect("close-request", self._on_close_request)
        GLib.timeout_add_seconds(_FLUSH_SECONDS, self._periodic_flush)

        self.navigate("home")

    # ---- Navigator (lo que usan las pantallas) --------------------------------

    def navigate(self, screen: str) -> None:
        if screen not in self._pages:
            return
        self._screen = screen
        self.ctx.workspace.activate(None)
        page = self._pages[screen]
        page.refresh()
        self._stack.set_visible_child(page)
        self.sidebar.select_screen(screen)
        self._hide_overlay_sidebar()

    def open_project(self, project_id: int) -> None:
        project = self.ctx.projects.get(project_id)
        if project is None:
            self.toast("El proyecto ya no está registrado.")
            return
        view = self._workspaces.get(project_id)
        if view is None:
            view = WorkspaceView(self, project)
            self._workspaces[project_id] = view
            self._stack.add_named(view, f"ws-{project_id}")
        self._screen = "workspace"
        self.ctx.workspace.activate(project_id)
        self.ctx.projects.mark_opened(project_id)
        view.refresh()
        self._stack.set_visible_child(view)
        self.sidebar.select_screen("workspace", project_id)
        self._hide_overlay_sidebar()

    def request_add_project(self) -> None:
        from ia_workspace.ui.dialogs.add_project import AddProjectDialog

        AddProjectDialog(self.ctx, on_added=self._on_project_added).present(self)

    def sidebar_toggle(self) -> Gtk.Widget:
        button = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", tooltip_text="Barra lateral (F9)")
        self._split.bind_property(
            "show-sidebar",
            button,
            "active",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        return button

    def toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # ---- acciones --------------------------------------------------------------

    def _setup_actions(self) -> None:
        app = self.get_application()
        nav = Gio.SimpleAction.new("navigate", GLib.VariantType.new("s"))
        nav.connect("activate", lambda _a, param: self.navigate(param.get_string()))
        self.add_action(nav)
        for item in NAV_ITEMS:
            app.set_accels_for_action(f"win.navigate::{item.screen}", [item.accel])

        for name, handler, accels in (
            ("add-project", lambda *_: self.request_add_project(), ["<Control>o"]),
            ("toggle-sidebar", lambda *_: self._toggle_sidebar(), ["F9"]),
            ("new-prompt", lambda *_: self._new_prompt(), ["<Control>n"]),
            ("new-terminal", lambda *_: self._new_terminal(), ["<Control><Shift>t"]),
            ("toggle-context", lambda *_: self._toggle_context(), ["<Shift>F9"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            app.set_accels_for_action(f"win.{name}", accels)

    def _toggle_sidebar(self) -> None:
        self._split.set_show_sidebar(not self._split.get_show_sidebar())

    def _hide_overlay_sidebar(self) -> None:
        if self._split.get_collapsed():
            self._split.set_show_sidebar(False)

    def _toggle_context(self) -> None:
        project_id = self.ctx.workspace.active_project_id
        if project_id is not None and project_id in self._workspaces:
            self._workspaces[project_id].toggle_context()

    def _new_terminal(self) -> None:
        project_id = self.ctx.workspace.active_project_id
        if project_id is None or project_id not in self._workspaces:
            self.toast("Abre un proyecto para crear una terminal en su carpeta.")
            return
        self._workspaces[project_id].new_terminal()

    def _new_prompt(self) -> None:
        target = self.ctx.workspace.active_project_id or self.ctx.workspace.last_project_id
        if target is None or self.ctx.projects.get(target) is None:
            projects = self.ctx.projects.list()
            target = projects[0].id if projects else None
        if target is None:
            self.request_add_project()
            return
        self.open_project(target)
        self._workspaces[target].focus_prompt()

    # ---- observadores ----------------------------------------------------------

    def _on_project_added(self, project_id: int) -> None:
        self.open_project(project_id)
        self.toast("Proyecto registrado")

    def _on_projects_changed(self) -> None:
        existing = {p.id: p for p in self.ctx.projects.list()}
        for project_id in list(self._workspaces):
            view = self._workspaces[project_id]
            if project_id not in existing:
                self._stack.remove(view)
                del self._workspaces[project_id]
                self.ctx.workspace.forget(project_id)
            else:
                view.update_project(existing[project_id])
        self.sidebar.refresh_projects()
        if self._screen == "workspace" and self.ctx.workspace.active_project_id is None:
            self.navigate("projects")
        elif self._screen in self._pages:
            self._pages[self._screen].refresh()

    def _on_session_event(self, event: SessionEvent) -> None:
        view = self._workspaces.get(event.session.project_id)
        if view is not None:
            view.on_session_event(event)
        if event.kind == "output":
            return  # la salida solo interesa a su workspace; no redibujar el resto
        self.sidebar.refresh_indicators()
        if self._screen in self._pages:
            self._pages[self._screen].refresh()

    # ---- estado y cierre -------------------------------------------------------

    def _restore_size(self) -> None:
        raw = self.ctx.settings.get(_SIZE_KEY, "1366x768") or "1366x768"
        try:
            width, height = (max(480, int(v)) for v in raw.split("x", 1))
        except ValueError:
            width, height = 1366, 768
        self.set_default_size(width, height)

    def release_web_views(self) -> None:
        """Termina los procesos de WebKit de las vistas de ChatGPT (al salir)."""
        for view in self._workspaces.values():
            if view._browser is not None:
                view._browser.shutdown()

    def save_state(self) -> None:
        width, height = self.get_default_size()
        if not self.is_maximized() and width > 0 and height > 0:
            self.ctx.settings.set(_SIZE_KEY, f"{width}x{height}")
        for view in self._workspaces.values():
            view.save_state()
        self.ctx.workspace.flush()

    def _periodic_flush(self) -> bool:
        if self.ctx.sessions.is_shutting_down:
            return GLib.SOURCE_REMOVE  # do_shutdown hará el guardado final
        for view in self._workspaces.values():
            view.save_state()
            view._render_memory_hint()
        self.ctx.workspace.flush()
        return GLib.SOURCE_CONTINUE

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.save_state()
        app = self.get_application()
        if self.ctx.sessions.has_active and not self.ctx.sessions.is_shutting_down:
            app.request_quit()  # pide confirmación; la ventana sigue abierta
            return True
        if self.ctx.sessions.is_shutting_down:
            return True  # se cerrará sola cuando terminen las sesiones
        return False

    def show_shutting_down(self) -> None:
        """Pantalla de espera mientras las sesiones finalizan de forma ordenada."""
        spinner = Adw.Spinner(halign=Gtk.Align.CENTER)
        spinner.set_size_request(48, 48)
        status = Adw.StatusPage(
            title="Finalizando sesiones…",
            description=(
                "Se está pidiendo a cada agente que termine de forma ordenada. "
                "Los que no respondan se detendrán automáticamente en unos segundos."
            ),
            child=spinner,
        )
        toolbar = Adw.ToolbarView(content=status)
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False))
        self._root.add_named(toolbar, "closing")
        self._root.set_visible_child_name("closing")

