"""Inspección de solo lectura de una carpeta de proyecto.

Nunca escribe en la carpeta ni ejecuta procesos: lee ``.git/HEAD`` directamente
y busca archivos conocidos para describir el stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


@dataclass(frozen=True)
class FolderInfo:
    is_git: bool = False
    branch: str | None = None
    stack: tuple[str, ...] = ()
    context_files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stack_label(self) -> str:
        return " · ".join(self.stack)


def _git_branch(root: Path) -> tuple[bool, str | None]:
    git = root / ".git"
    head: Path | None = None
    if git.is_dir():
        head = git / "HEAD"
    elif git.is_file():  # worktree o submódulo: "gitdir: <ruta>"
        try:
            content = git.read_text(encoding="utf-8").strip()
        except OSError:
            return True, None
        if content.startswith("gitdir:"):
            gitdir = Path(content.removeprefix("gitdir:").strip())
            head = (gitdir if gitdir.is_absolute() else root / gitdir) / "HEAD"
    if head is None:
        return False, None
    try:
        ref = head.read_text(encoding="utf-8").strip()
    except OSError:
        return True, None
    if ref.startswith("ref: refs/heads/"):
        return True, ref.removeprefix("ref: refs/heads/")
    return True, ref[:8] if ref else None  # HEAD separado


def _node_stack(root: Path) -> list[str]:
    try:
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["Node.js"]
    deps: dict[str, object] = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    found = []
    for dep, label in (
        ("next", "Next.js"),
        ("react", "React"),
        ("vue", "Vue"),
        ("svelte", "Svelte"),
        ("express", "Express"),
        ("@nestjs/core", "NestJS"),
        ("prisma", "Prisma"),
        ("@prisma/client", "Prisma"),
    ):
        if dep in deps and label not in found:
            found.append(label)
    if "Next.js" in found and "React" in found:
        found.remove("React")
    return found or ["Node.js"]


def detect_stack(root: Path) -> tuple[str, ...]:
    stack: list[str] = []
    if (root / "package.json").is_file():
        stack += _node_stack(root)
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        stack.append("Python")
    if (root / "Cargo.toml").is_file():
        stack.append("Rust")
    if (root / "go.mod").is_file():
        stack.append("Go")
    try:
        if any(p.suffix in (".csproj", ".sln") for p in root.iterdir()):
            stack.append(".NET")
    except OSError:
        pass
    if (root / "composer.json").is_file():
        stack.append("PHP")
    if (root / "pom.xml").is_file() or (root / "build.gradle").is_file():
        stack.append("Java")
    return tuple(dict.fromkeys(stack))


def inspect_folder(path: str | Path) -> FolderInfo:
    root = Path(path)
    if not root.is_dir():
        return FolderInfo()
    is_git, branch = _git_branch(root)
    contexts = tuple(name for name in CONTEXT_FILES if (root / name).is_file())
    return FolderInfo(is_git=is_git, branch=branch, stack=detect_stack(root), context_files=contexts)
