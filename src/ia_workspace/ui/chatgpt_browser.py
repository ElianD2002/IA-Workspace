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
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
try:
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # noqa: E402

    WEBKIT_AVAILABLE = True
except (ValueError, ImportError):  # falta gir1.2-webkit-6.0
    WebKit = None  # type: ignore[assignment]
    WEBKIT_AVAILABLE = False

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ia_workspace.services.web_policy import (  # noqa: E402
    Decision,
    is_chatgpt_audio_origin,
    navigation_decision,
    test_base_url,
)

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
        on_permission_prompt: (
            Callable[[WebKit.UserMediaPermissionRequest, Callable[[bool], None]], None] | None
        ) = None,
        data_dir: Path | None = None,
        on_file_chooser_prompt: (
            Callable[[WebKit.FileChooserRequest, Callable[[str], None]], None] | None
        ) = None,
        on_no_image_prompt: (
            Callable[[WebKit.FileChooserRequest, Callable[[str], None]], None] | None
        ) = None,
    ) -> None:
        self._on_changed = on_changed
        self._on_external = on_external
        self._on_permission_prompt = on_permission_prompt
        self._on_file_chooser_prompt = on_file_chooser_prompt
        self._on_no_image_prompt = on_no_image_prompt
        self._data_dir = data_dir
        if allow_loopback_http is None:
            allow_loopback_http = test_base_url(dict(os.environ)) is not None
        self._allow_loopback = allow_loopback_http
        self.link_id: int | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.notice: str | None = None
        self.loading = False
        self._last_permission_dialog: Adw.AlertDialog | None = None
        self._last_file_chooser_dialog: Adw.AlertDialog | None = None
        self._temp_capture_files: set[Path] = set()

        self.view = WebKit.WebView(network_session=session, hexpand=True, vexpand=True)
        self.view.set_focusable(True)
        self.view.set_focus_on_click(True)
        settings = self.view.get_settings()
        settings.set_enable_javascript(True)  # la aplicación web oficial lo necesita
        settings.set_enable_developer_extras(False)
        settings.set_javascript_can_open_windows_automatically(False)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        settings.set_enable_media_stream(True)
        settings.set_enable_webaudio(True)
        settings.set_enable_webrtc(False)
        if os.environ.get("IAW_MOCK_MEDIA_DEVICES") == "1":
            settings.set_enable_mock_capture_devices(True)

        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.view.add_controller(key_ctrl)

        self.view.connect("decide-policy", self._on_decide_policy)
        self.view.connect("permission-request", self._on_permission_request)
        self.view.connect("run-file-chooser", self._on_run_file_chooser)
        self.view.connect("load-changed", self._on_load_changed)
        self.view.connect("load-failed", self._on_load_failed)
        self.view.connect("web-process-terminated", self._on_terminated)
        self.view.connect("notify::title", lambda *_: self._on_changed())

    def _on_key_pressed(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        mods = state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
        )
        ctrl = Gdk.ModifierType.CONTROL_MASK
        ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK

        # Pegar (Ctrl+V, Ctrl+Shift+V, Shift+Insert)
        if (mods == ctrl and keyval in (Gdk.KEY_v, Gdk.KEY_V)) or (
            mods == Gdk.ModifierType.SHIFT_MASK and keyval == Gdk.KEY_Insert
        ):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_PASTE)
            return True
        if mods == ctrl_shift and keyval in (Gdk.KEY_v, Gdk.KEY_V):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_PASTE_AS_PLAIN_TEXT)
            return True

        # Copiar (Ctrl+C, Ctrl+Insert)
        if (mods == ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C)) or (
            mods == ctrl and keyval == Gdk.KEY_Insert
        ):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_COPY)
            return True

        # Cortar (Ctrl+X, Shift+Delete)
        if (mods == ctrl and keyval in (Gdk.KEY_x, Gdk.KEY_X)) or (
            mods == Gdk.ModifierType.SHIFT_MASK and keyval == Gdk.KEY_Delete
        ):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_CUT)
            return True

        # Seleccionar todo (Ctrl+A)
        if mods == ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_SELECT_ALL)
            return True

        # Deshacer (Ctrl+Z) y Rehacer (Ctrl+Shift+Z, Ctrl+Y)
        if mods == ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_UNDO)
            return True
        if (mods == ctrl_shift and keyval in (Gdk.KEY_z, Gdk.KEY_Z)) or (
            mods == ctrl and keyval in (Gdk.KEY_y, Gdk.KEY_Y)
        ):
            self.view.execute_editing_command(WebKit.EDITING_COMMAND_REDO)
            return True

        return False

    # ---- acciones -------------------------------------------------------------------

    def stop_capture(self) -> None:
        """Detiene cualquier captura activa de audio en la vista web."""
        if WebKit is not None and hasattr(self.view, "set_microphone_capture_state"):
            self.view.set_microphone_capture_state(WebKit.MediaCaptureState.NONE)

    def close(self) -> None:
        """Cierra la conversación actual, detiene capturas y limpia temporales."""
        if self._last_permission_dialog is not None:
            try:
                self._last_permission_dialog.force_close()
            except Exception:
                pass
            self._last_permission_dialog = None
        if self._last_file_chooser_dialog is not None:
            try:
                self._last_file_chooser_dialog.force_close()
            except Exception:
                pass
            self._last_file_chooser_dialog = None
        self._cleanup_all_temp_files()
        self.stop_capture()
        self.link_id = self.url = None
        self.error = self.notice = None
        self.view.load_uri("about:blank")

    def open(self, link_id: int, url: str) -> bool:
        """Carga la conversación. Si ya está abierta y sin errores, no la recarga."""
        if link_id == self.link_id and self.url == url and self.error is None:
            return False
        self.stop_capture()
        self._cleanup_all_temp_files()
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
        self.stop_capture()
        self._cleanup_all_temp_files()
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
        if (
            WebKit is not None
            and isinstance(request, WebKit.UserMediaPermissionRequest)
            and request.props.is_for_audio_device
            and not request.props.is_for_video_device
        ):
            uri = self.view.get_uri() or self.url or ""
            if is_chatgpt_audio_origin(uri, allow_loopback_http=self._allow_loopback):
                self._prompt_microphone_permission(request)
                return True
        request.deny()  # cualquier otro permiso u origen: denegado de inmediato
        return True

    def _prompt_microphone_permission(self, request: WebKit.UserMediaPermissionRequest) -> None:
        if self._last_permission_dialog is not None:
            try:
                self._last_permission_dialog.force_close()
            except Exception:
                pass
            self._last_permission_dialog = None

        def handle_response(allow: bool) -> None:
            self._last_permission_dialog = None
            if allow:
                request.allow()
            else:
                request.deny()

        if self._on_permission_prompt is not None:
            self._on_permission_prompt(request, handle_response)
            return

        parent = self.view.get_root() or self.view
        dialog = Adw.AlertDialog(
            heading="¿Permitir acceso al micrófono?",
            body=(
                "ChatGPT solicita utilizar el micrófono para el dictado por voz. "
                "El audio se procesará exclusivamente en tu sesión activa de ChatGPT."
            ),
        )
        dialog.add_response("deny", "Denegar")
        dialog.add_response("allow", "Permitir")
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance("deny", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("allow")
        dialog.set_close_response("deny")

        def on_dialog_response(_d: Adw.AlertDialog, response: str) -> None:
            handle_response(response == "allow")

        dialog.connect("response", on_dialog_response)
        dialog.present(parent)
        self._last_permission_dialog = dialog

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
        self.stop_capture()
        self._cleanup_all_temp_files()
        self.error = "La vista web se cerró inesperadamente. Recarga para continuar."
        self.loading = False
        self._on_changed()

    # ---- gestión de archivos temporales y portapapeles ------------------------------

    def _init_clipboard_dir(self) -> Path:
        if self._data_dir is not None:
            base = self._data_dir / "clipboard_captures"
        else:
            base = Path(tempfile.gettempdir()) / f"iaw-clipboard-{os.getuid()}"
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
        self._prune_stale_temp_captures(base)
        return base

    def _prune_stale_temp_captures(self, folder: Path) -> None:
        """Elimina capturas temporales de más de 15 minutos de sesiones previas."""
        try:
            now = time.time()
            for p in folder.glob("clip_*.png"):
                try:
                    if now - p.stat().st_mtime > 900:
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def _cleanup_all_temp_files(self) -> None:
        """Elimina todos los archivos temporales de capturas gestionados por esta instancia."""
        for p in list(self._temp_capture_files):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_capture_files.clear()

    def _remove_temp_file(self, path: Path) -> bool:
        """Eliminación diferida una vez que WebKit ha tenido tiempo suficiente de procesarlo."""
        self._temp_capture_files.discard(path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return GLib.SOURCE_REMOVE

    # ---- selección de archivos (WebKitFileChooserRequest) ---------------------------

    def _on_run_file_chooser(self, _view, request) -> bool:
        if WebKit is None or not isinstance(request, WebKit.FileChooserRequest):
            return False

        uri = self.view.get_uri() or self.url or ""
        if not is_chatgpt_audio_origin(uri, allow_loopback_http=self._allow_loopback):
            self._open_standard_file_dialog(request)
            return True

        self._prompt_file_chooser_options(request)
        return True

    def _prompt_file_chooser_options(self, request: WebKit.FileChooserRequest) -> None:
        if self._last_file_chooser_dialog is not None:
            try:
                self._last_file_chooser_dialog.force_close()
            except Exception:
                pass
            self._last_file_chooser_dialog = None

        def handle_action(action: str) -> None:
            self._last_file_chooser_dialog = None
            if action == "clipboard":
                self._attach_from_clipboard(request)
            elif action == "choose_file":
                self._open_standard_file_dialog(request)
            else:
                request.cancel()

        if self._on_file_chooser_prompt is not None:
            self._on_file_chooser_prompt(request, handle_action)
            return

        parent = self.view.get_root() or self.view
        dialog = Adw.AlertDialog(
            heading="Adjuntar en ChatGPT",
            body=(
                "¿Deseas usar la captura de pantalla o imagen copiada en tu portapapeles, "
                "o seleccionar un archivo desde tu equipo?"
            ),
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("choose_file", "Elegir archivo…")
        dialog.add_response("clipboard", "Usar imagen del portapapeles")
        dialog.set_response_appearance("clipboard", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("clipboard")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            handle_action(response)

        dialog.connect("response", on_response)
        dialog.present(parent)
        self._last_file_chooser_dialog = dialog

    def _attach_from_clipboard(self, request: WebKit.FileChooserRequest) -> None:
        window = self.view.get_root()
        clipboard = window.get_clipboard() if hasattr(window, "get_clipboard") else None
        if clipboard is None:
            display = self.view.get_display() or Gdk.Display.get_default()
            clipboard = display.get_clipboard() if display else None

        if clipboard is None:
            self._show_no_image_dialog(request)
            return

        def on_read_texture(clip, res) -> None:
            try:
                texture = clip.read_texture_finish(res)
            except Exception:
                texture = None

            if texture is None:
                self._show_no_image_dialog(request)
                return

            self._save_and_select_texture(texture, request)

        clipboard.read_texture_async(None, on_read_texture)

    def _save_and_select_texture(
        self, texture: Gdk.Texture, request: WebKit.FileChooserRequest
    ) -> None:
        folder = self._init_clipboard_dir()
        ts = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename = f"clip_{ts}_{token}.png"
        target_path = folder / filename

        saved = False
        try:
            fd = os.open(str(target_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            os.close(fd)
            saved = texture.save_to_png(str(target_path))
        except Exception:
            saved = False

        if not saved or not target_path.is_file() or target_path.stat().st_size == 0:
            target_path.unlink(missing_ok=True)
            self._show_no_image_dialog(request)
            return

        self._temp_capture_files.add(target_path)
        request.select_files([str(target_path)])

    def _show_no_image_dialog(self, request: WebKit.FileChooserRequest) -> None:
        if self._last_file_chooser_dialog is not None:
            try:
                self._last_file_chooser_dialog.force_close()
            except Exception:
                pass
            self._last_file_chooser_dialog = None

        def handle_action(action: str) -> None:
            self._last_file_chooser_dialog = None
            if action == "choose_file":
                self._open_standard_file_dialog(request)
            else:
                request.cancel()

        if self._on_no_image_prompt is not None:
            self._on_no_image_prompt(request, handle_action)
            return

        parent = self.view.get_root() or self.view
        dialog = Adw.AlertDialog(
            heading="No hay imagen en el portapapeles",
            body=(
                "El portapapeles está vacío o no contiene una imagen válida. "
                "Copia una captura de pantalla e inténtalo de nuevo, o selecciona un archivo de tu equipo."
            ),
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("choose_file", "Elegir archivo…")
        dialog.set_response_appearance("choose_file", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("choose_file")
        dialog.set_close_response("cancel")

        def on_response(_d: Adw.AlertDialog, response: str) -> None:
            handle_action(response)

        dialog.connect("response", on_response)
        dialog.present(parent)
        self._last_file_chooser_dialog = dialog

    def _open_standard_file_dialog(self, request: WebKit.FileChooserRequest) -> None:
        window = self.view.get_root()
        parent_window = window if isinstance(window, Gtk.Window) else None

        file_dialog = Gtk.FileDialog(title="Elegir archivo para adjuntar")
        if request.get_select_multiple():
            def on_multiple_open(_source, result):
                try:
                    files = file_dialog.open_multiple_finish(result)
                    paths = [f.get_path() for f in files if f.get_path()]
                    if paths:
                        request.select_files(paths)
                    else:
                        request.cancel()
                except Exception:
                    request.cancel()

            file_dialog.open_multiple(parent_window, None, on_multiple_open)
        else:
            def on_single_open(_source, result):
                try:
                    f = file_dialog.open_finish(result)
                    path = f.get_path() if f else None
                    if path:
                        request.select_files([path])
                    else:
                        request.cancel()
                except Exception:
                    request.cancel()

            file_dialog.open(parent_window, None, on_single_open)
