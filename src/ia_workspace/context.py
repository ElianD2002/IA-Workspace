"""Composición de dependencias de la aplicación (sin GTK)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ia_workspace.agents.catalog import AgentCatalog
from ia_workspace.brain.service import BrainService
from ia_workspace.config import AppPaths
from ia_workspace.domain.models import SELECTABLE_AGENTS
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.brain_index import BrainIndex
from ia_workspace.persistence.repositories import (
    AgentAutonomyRepository,
    BrainProjectRepository,
    ChatLinkRepository,
    ProjectRepository,
    PromptTransferRepository,
    SettingsRepository,
    TaskRepository,
    WorkspaceStateRepository,
)
from ia_workspace.services.chat_links import ChatLinkService
from ia_workspace.services.projects import ProjectService
from ia_workspace.services.prompt_transfers import PromptTransferService
from ia_workspace.services.tasks import TaskService
from ia_workspace.services.workspace import WorkspaceController
from ia_workspace.sessions.manager import Dispatch, Schedule, SessionManager, _direct, _no_timer


def build_brain(db: Database, paths: AppPaths) -> BrainService:
    """Cerebro compartido en ``<datos>/brain`` (XDG al estar instalada; ``var/`` desde el código)."""
    from ia_workspace.brain.proposals import ProposalRepository
    projects = ProjectRepository(db)
    return BrainService(
        paths.data_dir / "brain",
        BrainIndex(db.conn),
        BrainProjectRepository(db),
        project_name=lambda pid: (p.name if (p := projects.get(pid)) is not None else None),
        proposals=ProposalRepository(db),
    )


def build_readonly_brain(paths: AppPaths) -> tuple[BrainService, ProjectRepository, Callable[[], None]]:
    """Cerebro de solo lectura para la CLI de los agentes: no escribe nada en disco.

    Proyectos y memorias se leen de la base real en solo lectura; el índice de búsqueda es
    una base temporal en memoria construida a partir de los Markdown (la fuente canónica).
    Funciona aunque el agente corra en un sandbox que prohíbe escribir fuera del proyecto.
    Devuelve el servicio, los proyectos registrados y una función para cerrar las conexiones.
    """
    real = Database.open_readonly(paths.database, min_version=4)
    scratch = Database(":memory:")
    projects = ProjectRepository(real)
    brain = BrainService(
        paths.data_dir / "brain",
        BrainIndex(scratch.conn),
        BrainProjectRepository(real),
        project_name=lambda pid: (p.name if (p := projects.get(pid)) is not None else None),
        readonly=True,
    )

    def close() -> None:
        scratch.close()
        real.close()

    return brain, projects, close


@dataclass
class AppContext:
    paths: AppPaths | None
    db: Database
    settings: SettingsRepository
    projects: ProjectService
    workspace: WorkspaceController
    sessions: SessionManager
    agents: AgentCatalog
    chat_links: ChatLinkService
    prompt_transfers: PromptTransferService
    tasks: TaskService
    autonomy: AgentAutonomyRepository
    quotas: QuotaService | None = None
    brain: BrainService | None = None  # None sin carpeta de datos (pruebas con ":memory:")
    simulated_agents: bool = False

    @classmethod
    def create(
        cls,
        db: Database,
        paths: AppPaths | None = None,
        dispatch: Dispatch = _direct,
        schedule: Schedule = _no_timer,
        simulated_agents: bool = False,
    ) -> AppContext:
        settings = SettingsRepository(db)
        sessions = SessionManager(dispatch=dispatch, schedule=schedule)
        if simulated_agents:
            from ia_workspace.sessions.simulated import SimulatedBackend

            for kind in SELECTABLE_AGENTS:
                sessions.register_backend(SimulatedBackend(kind))
        from ia_workspace.quota.repository import QuotaRepository
        from ia_workspace.quota.service import QuotaService

        quota_repo = QuotaRepository(db)
        quotas = QuotaService(repo=quota_repo, dispatch=dispatch)
        prompt_transfers = PromptTransferService(PromptTransferRepository(db))
        tasks = TaskService(TaskRepository(db), sessions=sessions)
        return cls(
            paths=paths,
            db=db,
            settings=settings,
            projects=ProjectService(ProjectRepository(db)),
            workspace=WorkspaceController(WorkspaceStateRepository(db), settings),
            sessions=sessions,
            agents=AgentCatalog(dispatch=dispatch),
            chat_links=ChatLinkService(ChatLinkRepository(db)),
            prompt_transfers=prompt_transfers,
            tasks=tasks,
            autonomy=AgentAutonomyRepository(db),
            quotas=quotas,
            brain=build_brain(db, paths) if paths is not None else None,
            simulated_agents=simulated_agents,
        )

    def close(self) -> None:
        self.workspace.flush()
        self.tasks.close()
        self.db.close()
