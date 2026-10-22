"""Visor de Markdown nativo y seguro para GTK 4.

Convierte Markdown en texto con etiquetas de ``Gtk.TextBuffer``: no hay HTML, WebKit ni
marcado Pango que interpretar, así que el contenido de un documento nunca puede ejecutar
nada ni alterar la interfaz. Cubre lo habitual en una memoria técnica: títulos, negrita,
cursiva, código en línea y en bloque, listas, citas, separadores y enlaces (se muestra su
texto; no se abren).
"""

from __future__ import annotations

import re

from gi.repository import Gtk, Pango

_INLINE = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+\*\*|__[^_\n]+__)"
    r"|(?P<italic>\*[^*\n]+\*|_[^_\n]+_)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\s]+\))"
)


def _ensure_tags(buffer: Gtk.TextBuffer) -> None:
    table = buffer.get_tag_table()
    if table.lookup("h1") is not None:
        return
    buffer.create_tag("h1", weight=Pango.Weight.BOLD, scale=1.6, pixels_above_lines=10, pixels_below_lines=6)
    buffer.create_tag("h2", weight=Pango.Weight.BOLD, scale=1.3, pixels_above_lines=10, pixels_below_lines=4)
    buffer.create_tag("h3", weight=Pango.Weight.BOLD, scale=1.1, pixels_above_lines=8, pixels_below_lines=2)
    buffer.create_tag("bold", weight=Pango.Weight.BOLD)
    buffer.create_tag("italic", style=Pango.Style.ITALIC)
    buffer.create_tag("code", family="monospace")
    buffer.create_tag("codeblock", family="monospace", left_margin=18, pixels_above_lines=1)
    buffer.create_tag("list", left_margin=18)
    buffer.create_tag("quote", left_margin=18, style=Pango.Style.ITALIC)
    buffer.create_tag("link", underline=Pango.Underline.SINGLE)
    buffer.create_tag("rule", foreground="gray")


def _insert_inline(buffer: Gtk.TextBuffer, text: str, *base: str) -> None:
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), text[pos:match.start()], *base)
        kind, token = match.lastgroup, match.group(0)
        if kind == "code":
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), token[1:-1], *base, "code")
        elif kind == "bold":
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), token[2:-2], *base, "bold")
        elif kind == "italic":
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), token[1:-1], *base, "italic")
        else:
            label = token[1:token.index("](")]
            buffer.insert_with_tags_by_name(buffer.get_end_iter(), label, *base, "link")
        pos = match.end()
    if pos < len(text):
        buffer.insert_with_tags_by_name(buffer.get_end_iter(), text[pos:], *base)


def render_markdown(buffer: Gtk.TextBuffer, text: str) -> None:
    """Sustituye el contenido de ``buffer`` por ``text`` interpretado como Markdown."""
    _ensure_tags(buffer)
    buffer.set_text("")
    in_code = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        end = buffer.get_end_iter()
        if in_code:
            buffer.insert_with_tags_by_name(end, raw + "\n", "codeblock")
            continue
        if heading := re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line):
            level = min(len(heading.group(1)), 3)
            _insert_inline(buffer, heading.group(2), f"h{level}")
        elif item := re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line):
            marker = "•" if item.group(2) in "-*+" else item.group(2)
            buffer.insert_with_tags_by_name(end, f"{marker} ", "list")
            _insert_inline(buffer, item.group(3), "list")
        elif quote := re.match(r"^>\s?(.*)$", line):
            _insert_inline(buffer, quote.group(1), "quote")
        elif re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            buffer.insert_with_tags_by_name(end, "────────────", "rule")
        else:
            _insert_inline(buffer, line)
        buffer.insert(buffer.get_end_iter(), "\n")
