"""Composición de dependencias de la aplicación (sin GTK)."""

from __future__ import annotations

from dataclasses import dataclass

from ia_workspace.agents.catalog import AgentCatalog
from ia_workspace.config import AppPaths
from ia_workspace.domain.models import SELECTABLE_AGENTS
from ia_workspace.persistence.database import Database
from ia_workspace.persistence.repositories import (
    ChatLinkRepository,
    ProjectRepository,
    SettingsRepository,
    WorkspaceStateRepository,
)
from ia_workspace.services.chat_links import ChatLinkService
from ia_workspace.services.projects import ProjectService
from ia_workspace.services.workspace import WorkspaceController
from ia_workspace.sessions.manager import Dispatch, Schedule, SessionManager, _direct, _no_timer


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
        return cls(
            paths=paths,
            db=db,
            settings=settings,
            projects=ProjectService(ProjectRepository(db)),
            workspace=WorkspaceController(WorkspaceStateRepository(db), settings),
            sessions=sessions,
            agents=AgentCatalog(dispatch=dispatch),
            chat_links=ChatLinkService(ChatLinkRepository(db)),
            simulated_agents=simulated_agents,
        )

    def close(self) -> None:
        self.workspace.flush()
        self.db.close()
