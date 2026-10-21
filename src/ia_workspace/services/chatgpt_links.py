"""Validación de enlaces de conversaciones de ChatGPT (sin red ni GTK).

IA Workspace no accede al contenido de las conversaciones: solo valida el enlace,
lo guarda asociado a un proyecto y lo abre en el navegador del usuario, que usa su
propia sesión de ChatGPT.

Formatos admitidos (solo HTTPS):

* Conversación privada: ``https://chatgpt.com/c/<id>`` y, dentro de un GPT o de un
  proyecto de ChatGPT, ``https://chatgpt.com/g/<gpt-o-proyecto>/c/<id>``. Solo puede
  abrirla la cuenta propietaria.
* Enlace compartido: ``https://chatgpt.com/share/<id>``. Es una copia pública de solo
  lectura; no se presenta como conversación privada.
* Los mismos formatos con el dominio histórico ``chat.openai.com`` (se normalizan a
  ``chatgpt.com``, al que redirigen).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

CANONICAL_HOST = "chatgpt.com"
ALLOWED_HOSTS = frozenset({"chatgpt.com", "www.chatgpt.com", "chat.openai.com"})
MAX_URL_LENGTH = 2048

_ID = r"[A-Za-z0-9][A-Za-z0-9-]{7,99}"
_GPT = r"g-[A-Za-z0-9-]{1,120}"
_PRIVATE = re.compile(rf"^/(?:g/(?P<gpt>{_GPT})/)?c/(?P<id>{_ID})/?$")
_SHARED = re.compile(rf"^/share/(?P<id>{_ID})/?$")


class LinkKind(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"

    @property
    def label(self) -> str:
        return "Conversación privada" if self is LinkKind.PRIVATE else "Enlace compartido"

    @property
    def short_label(self) -> str:
        return "Privada" if self is LinkKind.PRIVATE else "Compartida"

    @property
    def default_title(self) -> str:
        return "Conversación de ChatGPT" if self is LinkKind.PRIVATE else "Conversación compartida de ChatGPT"


class InvalidChatLinkError(ValueError):
    """El enlace no es una conversación de ChatGPT válida. El mensaje es apto para la UI."""


@dataclass(frozen=True)
class ParsedChatLink:
    url: str  # canónica: https://chatgpt.com/…, sin parámetros ni fragmento
    kind: LinkKind
    conversation_id: str

    @property
    def short(self) -> str:
        """Forma breve para mostrar, p. ej. ``chatgpt.com/c/…3f9a``."""
        prefix = "c" if self.kind is LinkKind.PRIVATE else "share"
        return f"{CANONICAL_HOST}/{prefix}/…{self.conversation_id[-4:]}"


def parse_chat_link(raw: str) -> ParsedChatLink:
    text = (raw or "").strip()
    if not text:
        raise InvalidChatLinkError("Pega el enlace de la conversación.")
    if len(text) > MAX_URL_LENGTH or any(ch.isspace() for ch in text):
        raise InvalidChatLinkError("El enlace no es válido.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise InvalidChatLinkError("El enlace no es válido.") from exc
    if parts.scheme.lower() != "https":
        raise InvalidChatLinkError("El enlace debe empezar por https://")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise InvalidChatLinkError("Solo se admiten enlaces de chatgpt.com (o del antiguo chat.openai.com).")
    if parts.username or parts.password or port not in (None, 443):
        raise InvalidChatLinkError("El enlace no es válido.")

    if match := _PRIVATE.match(parts.path):
        gpt = match.group("gpt")
        conversation_id = match.group("id")
        path = f"/g/{gpt}/c/{conversation_id}" if gpt else f"/c/{conversation_id}"
        return ParsedChatLink(f"https://{CANONICAL_HOST}{path}", LinkKind.PRIVATE, conversation_id)
    if match := _SHARED.match(parts.path):
        conversation_id = match.group("id")
        return ParsedChatLink(f"https://{CANONICAL_HOST}/share/{conversation_id}", LinkKind.SHARED, conversation_id)
    raise InvalidChatLinkError(
        "No parece el enlace de una conversación. Copia el enlace con la conversación abierta "
        "(chatgpt.com/c/…) o un enlace compartido (chatgpt.com/share/…)."
    )
