"""Confirmación de salida con terminales o agentes activos."""

from __future__ import annotations

from gi.repository import Adw

from ia_workspace.context import AppContext
from ia_workspace.sessions.models import SessionInfo
from ia_workspace.ui.widgets import is_live_terminal, session_label


def build_confirm_quit_dialog(ctx: AppContext, active: list[SessionInfo]) -> Adw.AlertDialog:
    names = {p.id: p.name for p in ctx.projects.list()}
    lines = [
        f"• {s.spec.title or s.spec.kind.label} en «{names.get(s.project_id, '—')}» — "
        f"{session_label(s.state, is_live_terminal(ctx.sessions, s.id)).lower()}"
        for s in active[:6]
    ]
    if len(active) > 6:
        lines.append(f"• y {len(active) - 6} más")
    count = len(active)
    dialog = Adw.AlertDialog(
        heading=(
            f"¿Cerrar IA Workspace con {count} sesión activa?"
            if count == 1
            else f"¿Cerrar IA Workspace con {count} sesiones activas?"
        ),
        body=(
            "Las terminales y sesiones se finalizarán de forma ordenada antes de salir, junto con "
            "los procesos que hayan iniciado. Los cambios ya "
            "escritos en los proyectos se conservan. Si quieres que sigan trabajando, "
            "minimiza la ventana en lugar de cerrarla.\n\n" + "\n".join(lines)
        ),
    )
    dialog.set_body_use_markup(False)
    dialog.add_response("cancel", "Seguir trabajando")
    dialog.add_response("quit", "Finalizar y salir")
    dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    return dialog
