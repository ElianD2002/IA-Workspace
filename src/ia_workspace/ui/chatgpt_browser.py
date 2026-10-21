"""Vista web de ChatGPT dentro de IA Workspace (WebKitGTK 6.0).

Muestra el sitio oficial tal cual, con el User-Agent estándar de WebKitGTK: el usuario
inicia sesión con el flujo oficial de OpenAI y la sesión se guarda en el
almacenamiento persistente de WebKitGTK (una carpeta propia, compartida por todos los
proyectos). IA Workspace nunca lee, copia ni registra esas cookies, no inyecta ni
evalúa JavaScript y no extrae el contenido de las conversaciones.

Medidas de seguridad:
* Solo se navega por HTTPS (``services.web_policy``); otros esquemas se bloquean.
* Las ventanas nuevas (``target=_blank``) se abren en el navegador predeterminado.
* Se deniegan todos los permisos que pida el sitio (micrófono, notificaciones…).
* Las descargas se bloquean: hay que hacerlas desde el navegador predeterminado.
* Los errores TLS nunca se aceptan: se muestra el fallo y no se continúa.
* Sin herramientas de desarrollo, sin ventanas emergentes automáticas ni acceso a
  archivos locales.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402

    WEBKIT_AVAILABLE = True
except (ValueError, ImportError):  # falta gir1.2-webkit-6.0
    WebKit = None  # type: ignore[assignment]
    WEBKIT_AVAILABLE = False

from gi.repository import GLib, Gtk  # noqa: E402

from ia_workspace.services.web_policy import Decision, navigation_decision, test_base_url  # noqa: E402

_sessions: dict[str, object] = {}


def network_session(data_dir: Path | None):
    """Sesión de red persistente y compartida (una por carpeta y proceso).

    Con ``data_dir`` ``None`` (pruebas sin rutas) la sesión es efímera: no escribe nada
    en disco.
    """
    key = str(data_dir) if data_dir is not None else ":ephemeral:"
    if key in _sessions:
        return _sessions[key]
    if data_dir is None:
        session = WebKit.NetworkSession.new_ephemeral()
    else:
        data, cache = data_dir / "data", data_dir / "cache"
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        session = WebKit.NetworkSession.new(str(data), str(cache))
        session.get_cookie_manager().set_persistent_storage(
            str(data / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE
        )
    # Red de seguridad: ninguna descarga llega a escribirse en disco desde la vista.
    session.connect("download-started", lambda _s, download: download.cancel())
    _sessions[key] = session
    return session


def clear_website_data(data_dir: Path | None, on_done: Callable[[bool], None]) -> None:
    """Borra con la API oficial de WebKitGTK todos los datos del sitio (cookies, caché,
    almacenamiento local…). No los lee: solo pide a WebKit que los elimine."""
    manager = network_session(data_dir).get_website_data_manager()

    def finished(source, result) -> None:
        try:
            on_done(bool(source.clear_finish(result)))
        except GLib.Error:
            on_done(False)

    manager.clear(WebKit.WebsiteDataTypes.ALL, 0, None, finished)


def release_sessions() -> None:
    """Suelta las sesiones de red al salir (WebKit termina entonces su proceso de red)."""
    _sessions.clear()


def _is_error(error: GLib.Error, quark_fn, code) -> bool:
    return error.domain == GLib.quark_to_string(quark_fn()) and error.code == int(code)


class ChatBrowser:
    """Una vista web por proyecto. El widget puede moverse entre el panel y la vista ampliada."""

    def __init__(
        self,
        session,
        *,
        on_changed: Callable[[], None],
        on_external: Callable[[str], None],
        allow_loopback_http: bool | None = None,
    ) -> None:
        self._on_changed = on_changed
        self._on_external = on_external
        if allow_loopback_http is None:
            allow_loopback_http = test_base_url(dict(os.environ)) is not None
        self._allow_loopback = allow_loopback_http
        self.link_id: int | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.notice: str | None = None
        self.loading = False

        self.view = WebKit.WebView(network_session=session, hexpand=True, vexpand=True)
        settings = self.view.get_settings()
        settings.set_enable_javascript(True)  # la aplicación web oficial lo necesita
        settings.set_enable_developer_extras(False)
        settings.set_javascript_can_open_windows_automatically(False)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)

        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("permission-request", self._on_permission_request)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("web-process-terminated", self._on_terminated)
        self.view.connect("notify::title", lambda *_: self._on_changed())

    # ---- acciones -------------------------------------------------------------------

    def open(self, link_id: int, url: str) -> bool:
        """Carga la conversación. Si ya está abierta y sin errores, no la recarga."""
        if link_id == self.link_id and self.url == url and self.error is None:
            return False
        self.link_id, self.url = link_id, url
        self.error = self.notice = None
        self.view.load_uri(url)
        self._on_changed()
        return True

    def reload(self) -> None:
        self.error = self.notice = None
        if self.view.get_uri():
            self.view.reload()
        elif self.url:
            self.view.load_uri(self.url)
        self._on_changed()

    def shutdown(self) -> None:
        """Termina el proceso web de esta vista (al cerrar IA Workspace)."""
        self.view.terminate_web_process()

    @property
    def title(self) -> str:
        return self.view.get_title() or ""

    # ---- señales de WebKit -------------------------------------------------------------

    def _on_decide_policy(self, _view, decision, decision_type) -> bool:
        if decision_type == WebKit.PolicyDecisionType.RESPONSE:
            if not decision.is_mime_type_supported():
                decision.ignore()
                self.notice = "Las descargas no se hacen aquí: abre la conversación en el navegador."
                self._on_changed()
                return True
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        if decision_type == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            if navigation_decision(uri) is Decision.ALLOW and uri.startswith("https://"):
                self._on_external(uri)
            return True
        verdict = navigation_decision(uri, allow_loopback_http=self._allow_loopback)
        if verdict is Decision.ALLOW:
            return False
        decision.ignore()
        if verdict is Decision.EXTERNAL:
            self._on_external(uri)
        else:
            self.notice = "Se bloqueó una página que no usa una conexión segura (HTTPS)."
            self._on_changed()
        return True

    def _on_permission_request(self, _view, request) -> bool:
        request.deny()  # micrófono, cámara, notificaciones, ubicación…: nunca desde aquí
        return True

    def _on_load_changed(self, _view, event) -> None:
        self.loading = event != WebKit.LoadEvent.FINISHED
        self._on_changed()

    def _on_load_failed(self, _view, _event, _uri, error) -> bool:
        if _is_error(error, WebKit.network_error_quark, WebKit.NetworkError.CANCELLED) or _is_error(
            error, WebKit.policy_error_quark, WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE
        ):
            return False  # navegación cancelada o bloqueada a propósito: no es un fallo
        self.error = error.message or "No se pudo cargar la página."
        self.loading = False
        self._on_changed()
        return True  # se muestra el aviso de IA Workspace en lugar de la página de error

    def _on_terminated(self, _view, _reason) -> None:
        self.error = "La vista web se cerró inesperadamente. Recarga para continuar."
        self.loading = False
        self._on_changed()
