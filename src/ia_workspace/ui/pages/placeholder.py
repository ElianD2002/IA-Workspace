"""Pantallas cuya funcionalidad llega en fases posteriores.

Existen ya para que la navegación sea la definitiva y para fijar su texto.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ia_workspace.ui.pages.base import Navigator, Page


class _PlannedPage(Page):
    TITLE = ""
    ICON = ""
    HEADING = ""
    DESCRIPTION = ""

    def __init__(self, nav: Navigator) -> None:
        super().__init__(nav, self.TITLE)
        phase = Gtk.Label(label="Próxima fase", halign=Gtk.Align.CENTER)
        phase.add_css_class("phase-label")
        self.set_content(
            Adw.StatusPage(icon_name=self.ICON, title=self.HEADING, description=self.DESCRIPTION, child=phase)
        )


class TasksPage(_PlannedPage):
    TITLE = "Tareas y prompts"
    ICON = "view-list-bullet-symbolic"
    HEADING = "Tareas y biblioteca de prompts"
    DESCRIPTION = (
        "Cada prompt transferido a un agente se convertirá en una tarea del proyecto, con su "
        "conversación de origen, la sesión que la ejecuta y su resultado."
    )


class MemoryPage(_PlannedPage):
    TITLE = "Cerebro compartido"
    ICON = "user-bookmarks-symbolic"
    HEADING = "Memoria por proyecto"
    DESCRIPTION = (
        "Decisiones técnicas, estado actual, próximos pasos y reglas para agentes, separados "
        "por proyecto. Tú decides qué conclusiones se guardan."
    )


class HistoryPage(_PlannedPage):
    TITLE = "Historial y revisión"
    ICON = "document-open-recent-symbolic"
    HEADING = "Historial y revisión"
    DESCRIPTION = "Sesiones, cambios en archivos, pruebas y aprobaciones de cada agente."
