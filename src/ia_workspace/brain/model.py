"""Modelo del Cerebro compartido (sin GTK ni E/S).

Cada documento es un archivo Markdown con una cabecera mínima::

    ---
    title: Capas de la aplicación
    tags: api, next
    updated: 2026-10-21T18:00:00+00:00
    ---
    Contenido en Markdown…

La categoría y el módulo salen de la ruta (``arquitectura/capas.md``,
``modulos/pagos/webhooks.md``), que es la fuente de verdad: así un archivo movido a mano
no queda con metadatos contradictorios.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

MAX_DOCUMENT_BYTES = 64 * 1024  # divide los temas grandes en varios documentos
MAX_TITLE_LENGTH = 120
MAX_TAGS = 10
MAX_TAG_LENGTH = 32
SUMMARY_LENGTH = 160
INDEX_FILENAME = "INDEX.md"


class Scope(StrEnum):
    GLOBAL = "global"  # preferencias generales del usuario, comunes a todos los proyectos
    PROJECT = "project"  # memoria propia de un proyecto, aislada del resto

    @property
    def label(self) -> str:
        return "Memoria global" if self is Scope.GLOBAL else "Memoria del proyecto"


@dataclass(frozen=True)
class Category:
    key: str  # nombre de la carpeta
    scope: Scope
    label: str
    icon: str
    description: str
    modular: bool = False  # documentos agrupados por módulo: modulos/<módulo>/<doc>.md


CATEGORIES: tuple[Category, ...] = (
    Category("estado", Scope.PROJECT, "Estado actual", "dialog-information-symbolic",
             "Lo que un agente necesita saber hoy para retomar el trabajo."),
    Category("arquitectura", Scope.PROJECT, "Arquitectura", "applications-engineering-symbolic",
             "Cómo está construido el proyecto."),
    Category("modulos", Scope.PROJECT, "Documentación por módulo", "folder-documents-symbolic",
             "Detalle de cada módulo, en documentos separados para leer solo el necesario.", modular=True),
    Category("decisiones", Scope.PROJECT, "Decisiones técnicas", "object-select-symbolic",
             "Decisiones con su porqué. Los agentes las respetan salvo que la tarea diga lo contrario."),
    Category("reglas", Scope.PROJECT, "Reglas de desarrollo", "security-high-symbolic",
             "Límites que todos los agentes deben cumplir."),
    Category("proximos", Scope.PROJECT, "Próximos pasos", "go-next-symbolic",
             "Trabajo pendiente acordado."),
    Category("sesiones", Scope.PROJECT, "Resúmenes de sesiones", "document-open-recent-symbolic",
             "Hitos y sesiones relevantes, en orden cronológico."),
    Category("preferencias", Scope.GLOBAL, "Preferencias", "avatar-default-symbolic",
             "Cómo trabajas, aplicado a todos tus proyectos."),
    Category("convenciones", Scope.GLOBAL, "Convenciones de código", "text-x-generic-symbolic",
             "Convenciones que aplican a cualquier repositorio."),
    Category("entorno", Scope.GLOBAL, "Entorno", "utilities-terminal-symbolic",
             "Tu entorno de desarrollo local."),
)


def categories_for(scope: Scope) -> tuple[Category, ...]:
    return tuple(c for c in CATEGORIES if c.scope is scope)


def category(scope: Scope, key: str) -> Category | None:
    return next((c for c in CATEGORIES if c.scope is scope and c.key == key), None)


class BrainError(ValueError):
    """Error de validación apto para mostrarse en la interfaz."""


# ---- credenciales ---------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("una clave privada", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("una clave API de OpenAI", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("un token de GitHub", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("una clave de AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("una clave API de Google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("un token de Slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("un token JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("un token Bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}")),
    ("una contraseña", re.compile(r"(?i)\b(?:password|passwd|contraseña|pwd)\s*[:=]\s*\S{6,}")),
)


def find_secret(text: str) -> str | None:
    """Describe la primera credencial aparente del texto, o ``None``. Nunca la devuelve."""
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ---- nombres de archivo -----------------------------------------------------------------

def slugify(text: str, max_length: int = 60) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return slug[:max_length].rstrip("-") or "documento"


# ---- cabecera y documento -----------------------------------------------------------------

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


@dataclass(frozen=True)
class DocumentMeta:
    """Metadatos de un documento, sin su contenido (lo que listan el índice y la búsqueda)."""

    scope: Scope
    project_key: str  # "" en la memoria global
    relpath: str  # relativa a la raíz de su ámbito, con «/»
    title: str
    category: str
    module: str = ""
    tags: tuple[str, ...] = ()
    summary: str = ""
    updated: datetime | None = None
    size: int = 0


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    tags: tuple[str, ...]
    updated: datetime | None
    body: str
    problems: tuple[str, ...] = field(default_factory=tuple)  # avisos de integridad, no fatales


def clean_tags(raw: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    tags: list[str] = []
    for item in items:
        tag = " ".join(str(item).split()).lower()[:MAX_TAG_LENGTH]
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags[:MAX_TAGS])


def _first_heading(body: str) -> str:
    match = re.search(r"^#{1,6}\s+(.+?)\s*#*\s*$", body, re.M)
    return match.group(1).strip() if match else ""


def parse_document(text: str, fallback_title: str) -> ParsedDocument:
    """Lee la cabecera. Un archivo editado a mano sin cabecera sigue siendo válido."""
    problems: list[str] = []
    match = _FRONT_MATTER.match(text)
    fields: dict[str, str] = {}
    body = text
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            if not sep:
                problems.append(f"línea de cabecera no válida: {line.strip()[:40]!r}")
                continue
            fields[key.strip().lower()] = value.strip()
    else:
        problems.append("sin cabecera: se usa el primer título o el nombre del archivo")
    title = fields.get("title", "").strip() or _first_heading(body) or fallback_title
    updated = None
    if fields.get("updated"):
        try:
            updated = datetime.fromisoformat(fields["updated"])
        except ValueError:
            problems.append("fecha «updated» no válida")
    return ParsedDocument(
        title=title[:MAX_TITLE_LENGTH],
        tags=clean_tags(fields.get("tags", "")),
        updated=updated,
        body=body.lstrip("\n"),
        problems=tuple(problems),
    )


def _header_value(value: str) -> str:
    return " ".join(value.split())  # una sola línea: nada de saltos que rompan la cabecera


def serialize_document(title: str, tags: tuple[str, ...], body: str, updated: datetime | None = None) -> str:
    updated = updated or datetime.now(UTC).replace(microsecond=0)
    header = [
        "---",
        f"title: {_header_value(title)}",
        f"tags: {', '.join(_header_value(t) for t in tags)}",
        f"updated: {updated.isoformat()}",
        "---",
    ]
    text = body.replace("\r\n", "\n").strip("\n")
    return "\n".join(header) + "\n" + (text + "\n" if text else "")


def summarize(body: str, length: int = SUMMARY_LENGTH) -> str:
    """Primera línea con contenido (sin sintaxis Markdown), para el índice ligero."""
    in_code = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped or stripped.startswith("#"):
            continue
        plain = re.sub(r"^([-*+]|\d+[.)])\s+", "", stripped)
        plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
        plain = re.sub(r"^(>\s*)+", "", plain)
        # Énfasis y código, sin romper identificadores como «user_id» ni comparaciones «a > b».
        plain = re.sub(r"`+|(?<!\w)[*_]+|[*_]+(?!\w)", "", plain).strip()
        if plain:
            return plain if len(plain) <= length else plain[: length - 1].rstrip() + "…"
    return ""


def validate_document(title: str, body: str, tags: tuple[str, ...]) -> None:
    """Rechaza documentos sin título, demasiado grandes o con credenciales aparentes."""
    if not title.strip():
        raise BrainError("Escribe un título para el documento.")
    if len(title.strip()) > MAX_TITLE_LENGTH:
        raise BrainError(f"El título no puede superar {MAX_TITLE_LENGTH} caracteres.")
    size = len(serialize_document(title, tags, body).encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise BrainError(
            f"El documento ocupa {size // 1024} KiB y el máximo es {MAX_DOCUMENT_BYTES // 1024} KiB: "
            "divídelo en varios documentos más pequeños (por ejemplo, uno por módulo o por tema)."
        )
    if secret := find_secret(f"{title}\n{' '.join(tags)}\n{body}"):
        raise BrainError(
            f"El documento parece contener {secret}. La memoria no guarda credenciales ni tokens: "
            "quítala y vuelve a guardar."
        )
