"""Propuestas de actualización del Cerebro Compartido (fase 3C.1).

Permite a los agentes (Claude Code, OpenAI Codex y Google Antigravity) proponer nuevos
conocimientos o modificaciones de documentos Markdown existentes. Las propuestas se guardan
en archivos Markdown independientes bajo ``.proposals/`` y se registran en SQLite para su
seguimiento. El usuario tiene control total: revisa, compara diferencias y aprueba o rechaza
cada cambio antes de incorporarlo a la memoria oficial del proyecto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ia_workspace.brain.model import (
    MAX_DOCUMENT_BYTES,
    MAX_TITLE_LENGTH,
    BrainError,
    clean_tags,
    parse_document,
    serialize_document,
)
from ia_workspace.persistence.database import Database

PROPOSAL_DIRNAME = ".proposals"
PROPOSAL_ID_PATTERN = re.compile(r"^p-[a-z0-9]{8,16}$")


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONFLICT = "conflict"

    @property
    def label(self) -> str:
        match self:
            case ProposalStatus.PENDING:
                return "Pendiente"
            case ProposalStatus.APPROVED:
                return "Aprobada"
            case ProposalStatus.REJECTED:
                return "Rechazada"
            case ProposalStatus.CONFLICT:
                return "En conflicto"

    @property
    def badge_css(self) -> str:
        match self:
            case ProposalStatus.PENDING:
                return "warning"
            case ProposalStatus.APPROVED:
                return "success"
            case ProposalStatus.REJECTED:
                return "dim-label"
            case ProposalStatus.CONFLICT:
                return "error"


class ProposalOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"

    @property
    def label(self) -> str:
        return "Nuevo documento" if self is ProposalOperation.CREATE else "Actualización"


class BrainConflictError(BrainError):
    """Error al aprobar: el documento original cambió o ya existe."""


@dataclass(frozen=True)
class ProposalMeta:
    """Metadatos de una propuesta de actualización."""

    id: str
    project_id: int
    project_key: str
    agent_kind: str
    operation: ProposalOperation
    relpath: str
    title: str
    category: str
    module: str = ""
    tags: tuple[str, ...] = ()
    summary: str = ""
    base_hash: str = ""
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = datetime.now(UTC)
    reviewed_at: datetime | None = None
    review_note: str = ""


@dataclass(frozen=True)
class ProposalContent:
    """Propuesta completa: metadatos y cuerpo Markdown propuesto."""

    meta: ProposalMeta
    body: str


def serialize_proposal(meta: ProposalMeta, body: str) -> str:
    """Serializa la propuesta en Markdown con cabecera YAML front-matter."""
    lines = [
        "---",
        f"id: {meta.id}",
        f"project_id: {meta.project_id}",
        f"project_key: {meta.project_key}",
        f"agent: {meta.agent_kind}",
        f"operation: {meta.operation.value}",
        f"relpath: {meta.relpath}",
        f"title: {meta.title}",
        f"category: {meta.category}",
    ]
    if meta.module:
        lines.append(f"module: {meta.module}")
    if meta.tags:
        lines.append(f"tags: {', '.join(meta.tags)}")
    if meta.summary:
        lines.append(f"summary: {meta.summary}")
    if meta.base_hash:
        lines.append(f"base_hash: {meta.base_hash}")
    lines.append(f"status: {meta.status.value}")
    lines.append(f"created_at: {meta.created_at.isoformat()}")
    if meta.reviewed_at:
        lines.append(f"reviewed_at: {meta.reviewed_at.isoformat()}")
    if meta.review_note:
        lines.append(f"review_note: {meta.review_note}")
    lines.append("---")
    lines.append("")
    clean_body = body.strip("\n")
    if clean_body:
        lines.append(clean_body)
    return "\n".join(lines) + "\n"


def parse_proposal(text: str) -> tuple[dict[str, str], str]:
    """Parsea la cabecera YAML y el cuerpo de un archivo de propuesta."""
    front_matter_pattern = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
    match = front_matter_pattern.match(text)
    fields: dict[str, str] = {}
    body = text
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            if not line.strip():
                continue
            key, sep, val = line.partition(":")
            if sep:
                fields[key.strip().lower()] = val.strip()
    return fields, body.lstrip("\n")


class ProposalRepository:
    """Persistencia de metadatos de propuestas en SQLite."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, meta: ProposalMeta) -> None:
        with self._db.conn:
            self._db.conn.execute(
                """
                INSERT INTO brain_proposals (
                    id, project_id, project_key, agent_kind, operation,
                    relpath, title, category, module, tags, summary,
                    base_hash, status, created_at, reviewed_at, review_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta.id,
                    meta.project_id,
                    meta.project_key,
                    meta.agent_kind,
                    meta.operation.value,
                    meta.relpath,
                    meta.title,
                    meta.category,
                    meta.module,
                    ", ".join(meta.tags),
                    meta.summary,
                    meta.base_hash,
                    meta.status.value,
                    meta.created_at.isoformat(),
                    meta.reviewed_at.isoformat() if meta.reviewed_at else None,
                    meta.review_note,
                ),
            )

    def get(self, proposal_id: str) -> ProposalMeta | None:
        row = self._db.conn.execute(
            "SELECT * FROM brain_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return self._to_meta(row) if row is not None else None

    def list_for_project(
        self, project_id: int, status: ProposalStatus | None = None
    ) -> list[ProposalMeta]:
        if status is not None:
            rows = self._db.conn.execute(
                """
                SELECT * FROM brain_proposals
                WHERE project_id = ? AND status = ?
                ORDER BY created_at DESC
                """,
                (project_id, status.value),
            ).fetchall()
        else:
            rows = self._db.conn.execute(
                """
                SELECT * FROM brain_proposals
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [self._to_meta(r) for r in rows]

    def count_pending(self, project_id: int) -> int:
        row = self._db.conn.execute(
            """
            SELECT COUNT(*) FROM brain_proposals
            WHERE project_id = ? AND status IN (?, ?)
            """,
            (project_id, ProposalStatus.PENDING.value, ProposalStatus.CONFLICT.value),
        ).fetchone()
        return int(row[0]) if row else 0

    def update_status(
        self,
        proposal_id: str,
        status: ProposalStatus,
        reviewed_at: datetime | None = None,
        review_note: str = "",
        expected_status: tuple[ProposalStatus, ...] | None = None,
    ) -> bool:
        rev = reviewed_at.isoformat() if reviewed_at else None
        with self._db.conn:
            if expected_status:
                placeholders = ", ".join("?" for _ in expected_status)
                cur = self._db.conn.execute(
                    f"""
                    UPDATE brain_proposals
                    SET status = ?, reviewed_at = ?, review_note = ?
                    WHERE id = ? AND status IN ({placeholders})
                    """,
                    (status.value, rev, review_note, proposal_id, *(s.value for s in expected_status)),
                )
                return cur.rowcount > 0
            cur = self._db.conn.execute(
                """
                UPDATE brain_proposals
                SET status = ?, reviewed_at = ?, review_note = ?
                WHERE id = ?
                """,
                (status.value, rev, review_note, proposal_id),
            )
            return cur.rowcount > 0

    def delete(self, proposal_id: str) -> None:
        with self._db.conn:
            self._db.conn.execute("DELETE FROM brain_proposals WHERE id = ?", (proposal_id,))

    @staticmethod
    def _to_meta(row) -> ProposalMeta:
        created = datetime.fromisoformat(row["created_at"])
        reviewed = datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None
        tags_raw = row["tags"] or ""
        tags = tuple(t.strip() for t in tags_raw.split(",") if t.strip())
        return ProposalMeta(
            id=row["id"],
            project_id=row["project_id"],
            project_key=row["project_key"],
            agent_kind=row["agent_kind"],
            operation=ProposalOperation(row["operation"]),
            relpath=row["relpath"],
            title=row["title"],
            category=row["category"],
            module=row["module"] or "",
            tags=tags,
            summary=row["summary"] or "",
            base_hash=row["base_hash"] or "",
            status=ProposalStatus(row["status"]),
            created_at=created,
            reviewed_at=reviewed,
            review_note=row["review_note"] or "",
        )
