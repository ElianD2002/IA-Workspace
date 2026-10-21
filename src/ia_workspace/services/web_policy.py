"""Política de navegación de la vista web de ChatGPT (sin GTK).

La vista integrada solo muestra páginas HTTPS: ChatGPT y los proveedores de inicio de
sesión oficiales a los que redirige (OpenAI, Google, Microsoft, Apple…), que no se
enumeran para no romper ningún flujo oficial. Cualquier otro esquema se bloquea; los
enlaces de correo o teléfono se abren con la aplicación predeterminada del sistema.

Nunca se registran URL completas: las redirecciones de inicio de sesión llevan códigos
de un solo uso en la URL. ``redact`` deja solo esquema y host.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EXTERNAL_SCHEMES = frozenset({"mailto", "tel"})
# WebKitGTK pide la decisión tanto para el marco principal como para los iframes, sin
# distinguirlos; las aplicaciones web usan iframes about:/data:/blob: para contenido
# propio, así que esos esquemas «internos» no se bloquean.
IN_PAGE_SCHEMES = frozenset({"about", "data", "blob"})
CHATGPT_ORIGIN = "https://chatgpt.com"

# Solo para las pruebas automatizadas: sirve las conversaciones desde un servidor local
# simulado en vez de chatgpt.com. Se ignora salvo que apunte a http en loopback.
TEST_BASE_ENV = "IAW_CHATGPT_BASE_URL"


class Decision(StrEnum):
    ALLOW = "allow"
    EXTERNAL = "external"  # abrir con la aplicación predeterminada del sistema
    BLOCK = "block"


def is_loopback_http(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
        parts.port  # valida el puerto
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def navigation_decision(uri: str, *, allow_loopback_http: bool = False) -> Decision:
    """Qué hacer con una navegación de la vista web (marco principal o iframe)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return Decision.BLOCK
    scheme = parts.scheme.lower()
    if scheme == "https" and parts.hostname:
        return Decision.ALLOW
    if scheme in IN_PAGE_SCHEMES:
        return Decision.ALLOW
    if allow_loopback_http and is_loopback_http(uri):
        return Decision.ALLOW
    if scheme in EXTERNAL_SCHEMES:
        return Decision.EXTERNAL
    return Decision.BLOCK


def test_base_url(env: dict[str, str]) -> str | None:
    """Base local para pruebas, solo si es http en loopback (nunca un host remoto)."""
    raw = (env.get(TEST_BASE_ENV) or "").strip().rstrip("/")
    return raw if raw and is_loopback_http(raw) else None


def resolve_for_view(canonical_url: str, base: str | None) -> str:
    """URL que carga la vista: la canónica o, en pruebas, la misma ruta en el servidor local."""
    if base and canonical_url.startswith(CHATGPT_ORIGIN + "/"):
        return base + canonical_url[len(CHATGPT_ORIGIN):]
    return canonical_url


def redact(uri: str | None) -> str:
    """Esquema y host, sin ruta, parámetros ni fragmento (pueden contener códigos)."""
    if not uri:
        return ""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "(URL no válida)"
    if not parts.scheme:
        return "(URL no válida)"
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))
