"""Backend de sesiones que ejecuta procesos reales en terminales VTE (GTK 4).

El ``Vte.Terminal`` y su proceso pertenecen al handle, que vive en el
``SessionManager`` de la aplicación. La vista del workspace solo lo muestra: VTE
sigue leyendo el PTY aunque la terminal esté oculta o la ventana minimizada, de
modo que los procesos nunca se bloquean ni se reinician por la navegación.

Ciclo de vida de una sesión:

    start ─► STARTING ─(spawn ok)─► IDLE ─(el proceso termina)─► DONE
                │                     │
                └─(fallo)─► ERROR     └─ stop() ─► STOPPING ─► STOPPED

``stop()`` envía SIGHUP a toda la sesión de la terminal (igual que cerrar un
emulador de terminal) y, si algo sigue vivo tras ``STOP_GRACE_SECONDS``, SIGKILL.
``stop(force=True)`` envía SIGKILL de inmediato. Al terminar el shell, los procesos
que quedaran en su sesión (trabajos con ``nohup``, ``disown``…) también se finalizan:
IA Workspace no deja procesos huérfanos.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Vte", "3.91")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango, Vte  # noqa: E402

from ia_workspace.domain.models import AgentKind  # noqa: E402
from ia_workspace.sessions.backend import SessionSink  # noqa: E402
from ia_workspace.sessions.models import SessionSpec, SessionState  # noqa: E402
from ia_workspace.terminal import process, theme  # noqa: E402

STOP_GRACE_SECONDS = 5.0
STRAGGLER_GRACE_SECONDS = 1.0
SCROLLBACK_LINES = 10_000

ArgvBuilder = Callable[[SessionSpec], list[str]]


def _default_argv(spec: SessionSpec) -> list[str]:
    return list(spec.args) if spec.args else process.resolve_shell_argv()


def _exit_code(wait_status: int) -> int:
    try:
        return os.waitstatus_to_exitcode(wait_status)
    except ValueError:
        return wait_status


class VteTerminalHandle:
    def __init__(
        self,
        spec: SessionSpec,
        sink: SessionSink,
        argv: list[str],
        on_dispose: Callable[[VteTerminalHandle], None] | None = None,
    ) -> None:
        self.spec = spec
        self._on_dispose = on_dispose
        self.argv = argv
        self._sink = sink
        self.pid: int | None = None
        self._stop_requested = False
        self._exited = False
        self._reported = False
        self._timer: int | None = None

        self._terminal = Vte.Terminal(hexpand=True, vexpand=True, scrollback_lines=SCROLLBACK_LINES)
        self._terminal.add_css_class("managed-terminal")
        self._terminal.set_font(Pango.FontDescription.from_string("Monospace 11"))
        self._terminal.set_scroll_on_output(False)
        self._terminal.set_scroll_on_keystroke(True)
        self._terminal.connect("child-exited", self._on_child_exited)
        self._style_handler = theme.follow_style(self._terminal)

        self._terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            spec.cwd,
            argv,
            process.build_env(spec.project_id, spec.cwd),
            GLib.SpawnFlags.SEARCH_PATH,
            None,  # child_setup
            None,  # child_setup_data
            -1,  # sin tiempo límite
            None,  # cancellable
            self._on_spawned,
        )

    # ---- ViewableHandle --------------------------------------------------------

    def widget(self) -> Gtk.Widget:
        return self._terminal

    def dispose(self) -> None:
        self._cancel_timer()
        if self._style_handler:
            Adw.StyleManager.get_default().disconnect(self._style_handler)
            self._style_handler = 0
        if self._on_dispose is not None:
            self._on_dispose(self)
            self._on_dispose = None

    # ---- SessionHandle ---------------------------------------------------------

    def send_input(self, text: str) -> None:
        self._terminal.feed_child(text.encode("utf-8"))

    def stop(self, *, force: bool = False) -> None:
        self._stop_requested = True
        if self.pid is None or self._exited or self._reported:
            return  # aún lanzándose (se detendrá en _on_spawned) o ya terminado
        if force:
            self._kill_all()
            return
        process.signal_session(self.pid, signal.SIGHUP)
        if self._timer is None:
            self._timer = GLib.timeout_add(int(STOP_GRACE_SECONDS * 1000), self._on_grace_expired)

    # ---- consultas (UI y pruebas) -------------------------------------------------

    def text(self) -> str:
        content = self._terminal.get_text_format(Vte.Format.TEXT)
        return content or ""

    def has_foreground_work(self) -> bool:
        return self.pid is not None and not self._exited and process.has_foreground_work(self.pid)

    def managed_pids(self) -> list[int]:
        return [] if self.pid is None else [p.pid for p in process.session_members(self.pid)]

    def kill_now(self) -> None:
        """Red de seguridad al salir de la aplicación: SIGKILL sin esperar."""
        if self.pid is not None:
            self._kill_all()

    # ---- internos -----------------------------------------------------------------

    def _on_spawned(self, _terminal: Vte.Terminal, pid: int, error: GLib.Error | None, *_args: object) -> None:
        if error is not None or pid <= 0:
            message = error.message if error is not None else "no se pudo iniciar el proceso"
            self._report(SessionState.ERROR, f"No se pudo iniciar {self.argv[0]}: {message}", None)
            return
        self.pid = pid
        if self._stop_requested:
            self._kill_all()
            return
        self._sink.state_changed(SessionState.IDLE, f"PID {pid}")

    def _on_child_exited(self, _terminal: Vte.Terminal, status: int) -> None:
        self._exited = True
        self._cancel_timer()
        code = _exit_code(status)
        if self.pid is not None and process.session_members(self.pid):
            # Quedan procesos de esta terminal (nohup, disown, trabajos en segundo plano).
            process.signal_session(self.pid, signal.SIGTERM)
            GLib.timeout_add(int(STRAGGLER_GRACE_SECONDS * 1000), self._finish_after_stragglers, code)
            return
        self._report_exit(code)

    def _finish_after_stragglers(self, code: int) -> bool:
        self._kill_all()
        self._report_exit(code)
        return GLib.SOURCE_REMOVE

    def _report_exit(self, code: int) -> None:
        if self._stop_requested:
            self._report(SessionState.STOPPED, "Detenida", code)
        else:
            self._report(SessionState.DONE, f"Finalizada · código {code}", code)

    def _on_grace_expired(self) -> bool:
        self._timer = None
        if not self._exited:
            self._kill_all()
        return GLib.SOURCE_REMOVE

    def _kill_all(self) -> None:
        # Una vez notificado el final, el PID podría reutilizarse por otro proceso
        # del usuario: nunca se vuelve a señalizar.
        if self.pid is not None and not self._reported:
            process.kill_session(self.pid)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _report(self, state: SessionState, detail: str, code: int | None) -> None:
        if not self._reported:
            self._reported = True
            self._sink.state_changed(state, detail, code)


class VteTerminalBackend:
    """Terminales VTE. Hoy lanza el shell del usuario; en la fase 2B, con otro
    ``argv_builder``, lanzará las CLI oficiales (``claude``, ``codex``…)."""

    def __init__(self, kind: AgentKind = AgentKind.SHELL, argv_builder: ArgvBuilder = _default_argv) -> None:
        self.kind = kind
        self._argv_builder = argv_builder
        self._handles: list[VteTerminalHandle] = []

    def is_available(self) -> bool:
        return True

    def start(self, spec: SessionSpec, sink: SessionSink) -> VteTerminalHandle:
        if not Path(spec.cwd).is_dir():
            raise FileNotFoundError(f"La carpeta del proyecto no existe: {spec.cwd}")
        handle = VteTerminalHandle(spec, sink, self._argv_builder(spec), on_dispose=self.forget)
        self._handles.append(handle)
        return handle

    def kill_all(self) -> None:
        for handle in self._handles:
            handle.kill_now()

    def forget(self, handle: VteTerminalHandle) -> None:
        if handle in self._handles:
            self._handles.remove(handle)
