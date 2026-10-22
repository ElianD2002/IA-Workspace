"""Colores de las terminales VTE: paletas oficiales Catppuccin Mocha (oscuro) y Latte (claro).

Configura colores ANSI de 16 colores, fondo, primer plano, cursor y selección siguiendo
la especificación oficial de Catppuccin y adaptándose dinámicamente al tema del sistema.
"""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, Gdk, Vte  # noqa: E402

# Paleta oficial Catppuccin Mocha (Modo oscuro)
# 16 colores ANSI: Surface1, Red, Green, Yellow, Blue, Pink, Teal, Subtext1,
#                  Surface2, Red, Green, Yellow, Blue, Pink, Teal, Subtext0
MOCHA_ANSI = (
    "#45475a",  # 0: Black (Surface1)
    "#f38ba8",  # 1: Red
    "#a6e3a1",  # 2: Green
    "#f9e2af",  # 3: Yellow
    "#89b4fa",  # 4: Blue
    "#f5c2e7",  # 5: Magenta (Pink)
    "#94e2d5",  # 6: Cyan (Teal)
    "#bac2de",  # 7: White (Subtext1)
    "#585b70",  # 8: Bright Black (Surface2)
    "#f38ba8",  # 9: Bright Red
    "#a6e3a1",  # 10: Bright Green
    "#f9e2af",  # 11: Bright Yellow
    "#89b4fa",  # 12: Bright Blue
    "#f5c2e7",  # 13: Bright Magenta
    "#94e2d5",  # 14: Bright Cyan
    "#a6adc8",  # 15: Bright White (Subtext0)
)

# Paleta oficial Catppuccin Latte (Modo claro)
# 16 colores ANSI: Subtext1, Red, Green, Yellow, Blue, Pink, Teal, Surface2,
#                  Subtext0, Red, Green, Yellow, Blue, Pink, Teal, Surface1
LATTE_ANSI = (
    "#5c5f77",  # 0: Black (Subtext1)
    "#d20f39",  # 1: Red
    "#40a02b",  # 2: Green
    "#df8e1d",  # 3: Yellow
    "#1e66f5",  # 4: Blue
    "#ea76cb",  # 5: Magenta (Pink)
    "#179299",  # 6: Cyan (Teal)
    "#acb0be",  # 7: White (Surface2)
    "#6c6f85",  # 8: Bright Black (Subtext0)
    "#d20f39",  # 9: Bright Red
    "#40a02b",  # 10: Bright Green
    "#df8e1d",  # 11: Bright Yellow
    "#1e66f5",  # 12: Bright Blue
    "#ea76cb",  # 13: Bright Magenta
    "#179299",  # 14: Bright Cyan
    "#bcc0cc",  # 15: Bright White (Surface1)
)

DARK = {
    "background": "#1e1e2e",  # Base
    "foreground": "#cdd6f4",  # Text
    "cursor": "#f5e0dc",      # Rosewater
    "cursor_foreground": "#11111b",  # Crust
    "highlight": "#585b70",   # Surface2
    "highlight_foreground": "#cdd6f4",  # Text
    "palette": MOCHA_ANSI,
}

LIGHT = {
    "background": "#eff1f5",  # Base
    "foreground": "#4c4f69",  # Text
    "cursor": "#dc8a78",      # Rosewater
    "cursor_foreground": "#eff1f5",  # Base
    "highlight": "#acb0be",   # Surface2
    "highlight_foreground": "#4c4f69",  # Text
    "palette": LATTE_ANSI,
}


def _rgba(value: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(value)
    return color


def apply(terminal: Vte.Terminal, dark: bool | None = None) -> None:
    """Aplica colores Catppuccin oficiales a una terminal VTE sin alterar procesos en curso."""
    if dark is None:
        dark = Adw.StyleManager.get_default().get_dark()
    scheme = DARK if dark else LIGHT

    terminal.set_colors(
        _rgba(scheme["foreground"]),
        _rgba(scheme["background"]),
        [_rgba(c) for c in scheme["palette"]],
    )

    if hasattr(terminal, "set_color_cursor") and "cursor" in scheme:
        terminal.set_color_cursor(_rgba(scheme["cursor"]))
    if hasattr(terminal, "set_color_cursor_foreground") and "cursor_foreground" in scheme:
        terminal.set_color_cursor_foreground(_rgba(scheme["cursor_foreground"]))
    if hasattr(terminal, "set_color_highlight") and "highlight" in scheme:
        terminal.set_color_highlight(_rgba(scheme["highlight"]))
    if hasattr(terminal, "set_color_highlight_foreground") and "highlight_foreground" in scheme:
        terminal.set_color_highlight_foreground(_rgba(scheme["highlight_foreground"]))


def follow_style(terminal: Vte.Terminal) -> int:
    """Aplica los colores Catppuccin y los actualiza al cambiar el tema. Devuelve el id de la señal."""
    apply(terminal)
    manager = Adw.StyleManager.get_default()
    return manager.connect("notify::dark", lambda m, _p: apply(terminal, m.get_dark()))
