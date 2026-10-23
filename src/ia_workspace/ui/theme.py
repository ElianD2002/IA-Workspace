"""Gestión de temas de IA Workspace: paletas oficiales Catppuccin Latte y Mocha.

Aplica Catppuccin Latte en modo claro y Catppuccin Mocha en modo oscuro sobre Libadwaita y GTK4.
El tema cambia dinámicamente según la preferencia del sistema GNOME o la configuración
de la aplicación sin requerir reinicio.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

# Paletas oficiales Catppuccin (https://catppuccin.com/palette/)
MOCHA = {
    "rosewater": "#f5e0dc",
    "flamingo": "#f2cdcd",
    "pink": "#f5c2e7",
    "mauve": "#cba6f7",
    "red": "#f38ba8",
    "maroon": "#eba0ac",
    "peach": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "teal": "#94e2d5",
    "sky": "#89dceb",
    "sapphire": "#74c7ec",
    "blue": "#89b4fa",
    "lavender": "#b4befe",
    "text": "#cdd6f4",
    "subtext1": "#bac2de",
    "subtext0": "#a6adc8",
    "overlay2": "#9399b2",
    "overlay1": "#7f849c",
    "overlay0": "#6c7086",
    "surface2": "#585b70",
    "surface1": "#45475a",
    "surface0": "#313244",
    "base": "#1e1e2e",
    "mantle": "#181825",
    "crust": "#11111b",
}

LATTE = {
    "rosewater": "#dc8a78",
    "flamingo": "#dd7878",
    "pink": "#ea76cb",
    "mauve": "#8839ef",
    "red": "#d20f39",
    "maroon": "#e64553",
    "peach": "#fe640b",
    "yellow": "#df8e1d",
    "green": "#40a02b",
    "teal": "#179299",
    "sky": "#04a5e5",
    "sapphire": "#209fb5",
    "blue": "#1e66f5",
    "lavender": "#7287fd",
    "text": "#4c4f69",
    "subtext1": "#5c5f77",
    "subtext0": "#6c6f85",
    "overlay2": "#7c7f93",
    "overlay1": "#8c8fa1",
    "overlay0": "#9ca0b0",
    "surface2": "#acb0be",
    "surface1": "#bcc0cc",
    "surface0": "#ccd0da",
    "base": "#eff1f5",
    "mantle": "#e6e9ef",
    "crust": "#dce0e8",
}


def get_catppuccin_css(dark: bool) -> str:
    """Genera las definiciones CSS de Libadwaita y variables raíz para Catppuccin."""
    c = MOCHA if dark else LATTE

    if dark:
        card_bg = c["surface0"]
        card_border = c["surface1"]
        sidebar_bg = c["mantle"]
        header_bg = c["mantle"]
        popover_bg = c["surface0"]
        accent_fg = c["crust"]
        shade_val = "rgba(0, 0, 0, 0.36)"
        secondary_opacity = "0.70"
    else:
        card_bg = "#ffffff"
        card_border = c["surface0"]
        sidebar_bg = c["mantle"]
        header_bg = c["mantle"]
        popover_bg = c["mantle"]
        accent_fg = "#ffffff"
        shade_val = "rgba(0, 0, 0, 0.07)"
        secondary_opacity = "0.78"

    return f"""
/* Catppuccin {"Mocha (Dark)" if dark else "Latte (Light)"} */
@define-color accent_color {c["blue"]};
@define-color accent_bg_color {c["blue"]};
@define-color accent_fg_color {accent_fg};

@define-color window_bg_color {c["base"]};
@define-color window_fg_color {c["text"]};

@define-color view_bg_color {c["base"]};
@define-color view_fg_color {c["text"]};

@define-color headerbar_bg_color {header_bg};
@define-color headerbar_fg_color {c["text"]};
@define-color headerbar_border_color {card_border};
@define-color headerbar_backdrop_color {header_bg};

@define-color card_bg_color {card_bg};
@define-color card_fg_color {c["text"]};
@define-color card_border_color {card_border};

@define-color popover_bg_color {popover_bg};
@define-color popover_fg_color {c["text"]};

@define-color dialog_bg_color {c["base"]};
@define-color dialog_fg_color {c["text"]};

@define-color sidebar_bg_color {sidebar_bg};
@define-color sidebar_fg_color {c["text"]};
@define-color sidebar_border_color {card_border};
@define-color secondary_sidebar_bg_color {sidebar_bg};
@define-color secondary_sidebar_fg_color {c["text"]};

@define-color destructive_color {c["red"]};
@define-color destructive_bg_color {c["red"]};
@define-color destructive_fg_color {accent_fg};

@define-color error_color {c["red"]};
@define-color error_bg_color {c["red"]};
@define-color error_fg_color {accent_fg};

@define-color success_color {c["green"]};
@define-color success_bg_color {c["green"]};
@define-color success_fg_color {accent_fg};

@define-color warning_color {c["yellow"]};
@define-color warning_bg_color {c["yellow"]};
@define-color warning_fg_color {accent_fg};

@define-color border_color {card_border};
@define-color shade_color {shade_val};
@define-color scrollbar_outline_color {card_border};

:root {{
  /* Variables de Libadwaita 1.6+ (sus widgets usan estas, no los @define-color). */
  --window-bg-color: {c["base"]};
  --window-fg-color: {c["text"]};
  --view-bg-color: {c["base"]};
  --view-fg-color: {c["text"]};
  --headerbar-bg-color: {header_bg};
  --headerbar-fg-color: {c["text"]};
  --headerbar-border-color: {card_border};
  --headerbar-backdrop-color: {header_bg};
  --sidebar-fg-color: {c["text"]};
  --sidebar-border-color: {card_border};
  --secondary-sidebar-bg-color: {sidebar_bg};
  --secondary-sidebar-fg-color: {c["text"]};
  --dialog-bg-color: {c["base"]};
  --dialog-fg-color: {c["text"]};
  --popover-bg-color: {popover_bg};
  --popover-fg-color: {c["text"]};
  --accent-color: {c["blue"]};
  --accent-bg-color: {c["blue"]};
  --accent-fg-color: {accent_fg};
  --warning-color: {c["yellow"]};
  --warning-bg-color: {c["yellow"]};
  --success-color: {c["green"]};
  --success-bg-color: {c["green"]};
  --error-color: {c["red"]};
  --error-bg-color: {c["red"]};
  --sidebar-bg-color: {sidebar_bg};
  --border-color: {card_border};
  --card-bg-color: {card_bg};
  --card-fg-color: {c["text"]};
  --terminal-bg-color: {c["base"]};
  --terminal-fg-color: {c["text"]};
}}

.dim-label,
label.dim-label,
.subtitle {{
  opacity: {secondary_opacity};
}}
"""


# Por encima de la prioridad de usuario: un tema GTK global (~/.config/gtk-4.0/gtk.css,
# p. ej. una variante solo clara) se aplica con prioridad USER y, de lo contrario, impondría
# sus colores aunque se eligiera Mocha. Este proveedor solo define colores y variables (ningún
# selector de widgets), así que no sustituye las reglas del tema ni de style.css: solo hace
# que, dentro de IA Workspace, la paleta elegida sea la que resuelven. No modifica el tema global.
THEME_PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1

_theme_provider: Gtk.CssProvider | None = None
_style_manager_signal_id: int | None = None


def apply_theme(dark: bool | None = None, display: Gdk.Display | None = None) -> None:
    """Aplica o actualiza el CSS del tema Catppuccin en el display actual."""
    global _theme_provider
    if display is None:
        display = Gdk.Display.get_default()
    if display is None:
        return

    if dark is None:
        dark = Adw.StyleManager.get_default().get_dark()

    if _theme_provider is None:
        _theme_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(display, _theme_provider, THEME_PRIORITY)

    css = get_catppuccin_css(dark)
    _theme_provider.load_from_string(css)


def setup_theme(display: Gdk.Display | None = None) -> None:
    """Configura el tema Catppuccin para seguir automáticamente el estilo claro/oscuro."""
    global _style_manager_signal_id
    apply_theme(display=display)

    manager = Adw.StyleManager.get_default()
    if _style_manager_signal_id is None:
        _style_manager_signal_id = manager.connect(
            "notify::dark", lambda m, _pspec: apply_theme(dark=m.get_dark())
        )
