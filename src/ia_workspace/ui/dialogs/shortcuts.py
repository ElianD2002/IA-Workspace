"""Atajos de teclado."""

from __future__ import annotations

from gi.repository import Adw

from ia_workspace.ui.sidebar import NAV_ITEMS

SECTIONS = (
    ("General", (
        ("Agregar proyecto", "<Control>o"),
        ("Nuevo prompt", "<Control>n"),
        ("Mostrar u ocultar barra lateral", "F9"),
        ("Menú principal", "F10"),
        ("Preferencias", "<Control>comma"),
        ("Atajos de teclado", "<Control>question"),
        ("Salir", "<Control>q"),
    )),
    ("Navegación", tuple((item.label, item.accel) for item in NAV_ITEMS)),
)


def build_shortcuts_dialog() -> Adw.ShortcutsDialog:
    dialog = Adw.ShortcutsDialog()
    for title, items in SECTIONS:
        section = Adw.ShortcutsSection(title=title)
        for label, accel in items:
            section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
        dialog.add(section)
    return dialog
