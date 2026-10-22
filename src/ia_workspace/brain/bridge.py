"""Puente de contexto bajo demanda: integración del Cerebro Compartido con sesiones de agentes (fase 3B).

Permite que cada agente (Claude Code, OpenAI Codex y Google Antigravity) descubra
automáticamente dónde consultar la memoria Markdown de su proyecto sin recibir
ni leer toda la documentación al iniciar.
"""

from __future__ import annotations

from ia_workspace.agents.tools import TOOLS, codex_defines_developer_instructions
from ia_workspace.brain.cli import cli_command
from ia_workspace.brain.service import BrainService
from ia_workspace.domain.models import AgentKind, Project
from ia_workspace.sessions.models import BrainLink, BrainLinkStatus


def compose_prompt_with_reference(prompt: str, reference: str) -> str:
    """Compone de forma controlada el prompt inicial incorporando la referencia a la memoria."""
    clean_ref = reference.strip()
    clean_prompt = prompt.strip()
    return f"{clean_ref}\n\n---\n\n{clean_prompt}"


def resolve_brain_link(
    project: Project,
    kind: AgentKind,
    brain: BrainService | None,
    command: str | None = None,
    *,
    into_prompt: bool = False,
) -> BrainLink | None:
    """Determina cómo se presenta la memoria del proyecto para una sesión de agente.

    - Claude Code: vinculación oficial mediante ``--append-system-prompt`` al iniciar.
    - Codex CLI: override de sesión oficial con ``-c developer_instructions=...``
      salvo que el usuario ya tenga instrucciones propias en su configuración.
    - Antigravity: no admite parámetros CLI de sistema sin disparar turnos de IA;
      ofrece la referencia para copiar o la incorpora explícitamente al primer prompt.
    - Terminales ordinarias (SHELL): no vinculan memoria (devuelve ``None``).
    """
    if kind is AgentKind.SHELL:
        return None
    if brain is None:
        return BrainLink(
            status=BrainLinkStatus.UNAVAILABLE,
            detail="Cerebro compartido no disponible en este entorno.",
        )
    try:
        key = brain.existing_project_key(project.id)
    except Exception as exc:
        return BrainLink(
            status=BrainLinkStatus.UNAVAILABLE,
            detail=f"No se pudo consultar el Cerebro: {exc}",
        )
    if key is None:
        return BrainLink(
            status=BrainLinkStatus.UNAVAILABLE,
            detail="Este proyecto todavía no tiene memoria en el Cerebro.",
        )

    cmd = command or cli_command()
    try:
        reference = brain.agent_brief(project, cmd)
    except Exception as exc:
        return BrainLink(
            status=BrainLinkStatus.UNAVAILABLE,
            detail=f"No se pudo generar la referencia al Cerebro: {exc}",
        )
    tool = TOOLS.get(kind)
    if tool is None:
        return BrainLink(
            status=BrainLinkStatus.UNAVAILABLE,
            detail=f"{kind.label} no es una herramienta de agente configurada.",
            reference=reference,
        )

    if into_prompt:
        return BrainLink(
            status=BrainLinkStatus.APPLIED,
            detail="Referencia al Cerebro incorporada al inicio del prompt transferido.",
            reference=reference,
            via_launch=False,
        )

    if tool.context_mechanism == "append-system-prompt":
        return BrainLink(
            status=BrainLinkStatus.APPLIED,
            detail="Referencia al Cerebro vinculada automáticamente (--append-system-prompt).",
            reference=reference,
            via_launch=True,
        )

    if tool.context_mechanism == "developer-instructions":
        if codex_defines_developer_instructions(project.path):
            return BrainLink(
                status=BrainLinkStatus.COPY,
                detail="Codex ya define developer_instructions en su configuración: referencia disponible para copiar.",
                reference=reference,
                via_launch=False,
            )
        return BrainLink(
            status=BrainLinkStatus.APPLIED,
            detail="Referencia al Cerebro vinculada automáticamente (-c developer_instructions).",
            reference=reference,
            via_launch=True,
        )

    return BrainLink(
        status=BrainLinkStatus.COPY,
        detail=f"{kind.label} no admite instrucciones de sistema por CLI: referencia disponible para copiar.",
        reference=reference,
        via_launch=False,
    )
