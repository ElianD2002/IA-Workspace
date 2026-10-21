"""Colores de las terminales: los del prototipo (``--term``/``--term-fg``) y una
paleta ANSI al estilo de GNOME. Siguen el tema claro/oscuro de Libadwaita."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, Vte  # noqa: E402

DARK = {
    "background": "#161618",
    "foreground": "#e6e6e9",
    "palette": (
        "#241f31", "#c01c28", "#2ec27e", "#f5c211", "#1e78e4", "#9841bb", "#0ab9dc", "#c0bfbc",
        "#5e5c64", "#ed333b", "#57e389", "#f8e45c", "#51a1ff", "#c061cb", "#4fd2fd", "#f6f5f4",
    ),
}
LIGHT = {
    "background": "#f6f6f8",
    "foreground": "#2b2b31",
    "palette": (
        "#241f31", "#c01c28", "#26a269", "#a2734c", "#1c71d8", "#813d9c", "#2190a4", "#9a9996",
        "#5e5c64", "#e01b24", "#2ec27e", "#c88800", "#3584e4", "#9141ac", "#0ab9dc", "#deddda",
    ),
}


def _rgba(value: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(value)
    return color


def apply(terminal: Vte.Terminal, dark: bool | None = None) -> None:
    if dark is None:
        dark = Adw.StyleManager.get_default().get_dark()
    scheme = DARK if dark else LIGHT
    terminal.set_colors(
        _rgba(scheme["foreground"]),
        _rgba(scheme["background"]),
        [_rgba(c) for c in scheme["palette"]],
    )


def follow_style(terminal: Vte.Terminal) -> int:
    """Aplica los colores y los actualiza al cambiar el tema. Devuelve el id de la señal."""
    apply(terminal)
    manager = Adw.StyleManager.get_default()
    return manager.connect("notify::dark", lambda m, _p: apply(terminal, m.get_dark()))
