"""Consulta del Cerebro para agentes (solo lectura de documentos).

    iaw-brain index                     # INDEX.md del proyecto (ligero)
    iaw-brain list [--category C]       # una línea por documento
    iaw-brain search <términos…> [--category C] [--limit N]
    iaw-brain read <ruta> [--max-chars N]
    … --global                          # lo mismo sobre la memoria global del usuario
    … --json                            # salida estructurada

El proyecto se deduce de la carpeta actual (la del agente): la carpeta registrada en IA
Workspace que la contiene. No hay ninguna opción para elegir otro proyecto, así que la
consulta solo alcanza la memoria de ese proyecto y la global. La salida es breve a
propósito: listar y buscar nunca vuelcan el contenido completo de los documentos.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ia_workspace.brain.model import BrainError, Scope, categories_for

EXIT_ERROR = 1
EXIT_NO_PROJECT = 2


def cli_command() -> str:
    """Orden que se indica a los agentes para consultar la memoria."""
    wrapper = Path(__file__).resolve().parents[3] / "scripts" / "iaw-brain"
    return str(wrapper) if wrapper.is_file() else "python3 -m ia_workspace.brain"


def find_project(projects, directory: Path):
    """Proyecto registrado cuya carpeta es ``directory`` o la contiene (la más específica)."""
    try:
        target = directory.resolve()
    except OSError:
        return None
    best = None
    for project in projects:
        root = Path(project.path)
        if target == root or target.is_relative_to(root):
            if best is None or len(root.parts) > len(Path(best.path).parts):
                best = project
    return best


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iaw-brain", description="Consulta la memoria de IA Workspace.")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--global", dest="global_scope", action="store_true", help="memoria global del usuario")
    common.add_argument("--json", action="store_true", help="salida en JSON")
    sub.add_parser("index", parents=[common], help="muestra INDEX.md")
    listing = sub.add_parser("list", parents=[common], help="lista los documentos")
    listing.add_argument("--category")
    search = sub.add_parser("search", parents=[common], help="busca por palabras clave")
    search.add_argument("terms", nargs="+")
    search.add_argument("--category")
    search.add_argument("--limit", type=int, default=8)
    read = sub.add_parser("read", parents=[common], help="lee un documento")
    read.add_argument("relpath")
    read.add_argument("--max-chars", type=int, default=0)
    return parser


def _meta_dict(meta) -> dict:
    return {"path": meta.relpath, "title": meta.title, "category": meta.category, "module": meta.module,
            "tags": list(meta.tags), "summary": meta.summary,
            "updated": meta.updated.isoformat() if meta.updated else None}


def main(argv: list[str] | None = None, cwd: Path | None = None, out=sys.stdout, err=sys.stderr) -> int:
    args = _parser().parse_args(argv)
    from ia_workspace.config import resolve_paths
    from ia_workspace.context import build_brain
    from ia_workspace.persistence.database import Database
    from ia_workspace.persistence.repositories import ProjectRepository

    paths = resolve_paths()
    if not paths.database.is_file():
        print("No hay datos de IA Workspace en este equipo.", file=err)
        return EXIT_ERROR
    db = Database(paths.database)
    try:
        brain = build_brain(db, paths)
        scope, key = Scope.GLOBAL, ""
        if not args.global_scope:
            project = find_project(ProjectRepository(db).list(), cwd or Path.cwd())
            if project is None:
                print("Esta carpeta no pertenece a ningún proyecto de IA Workspace "
                      "(usa --global para la memoria global).", file=err)
                return EXIT_NO_PROJECT
            scope, key = Scope.PROJECT, brain.project_key(project)
        valid = {c.key for c in categories_for(scope)}
        category = getattr(args, "category", None)
        if category and category not in valid:
            print(f"Categoría no válida. Opciones: {', '.join(sorted(valid))}", file=err)
            return EXIT_ERROR

        if args.command == "index":
            brain.sync(scope, key)
            text = brain.index_path(scope, key).read_text(encoding="utf-8")
            print(json.dumps({"index": text}, ensure_ascii=False) if args.json else text, end="" if not args.json
                  else "\n", file=out)
        elif args.command == "list":
            docs = brain.list(scope, key, category)
            if args.json:
                print(json.dumps([_meta_dict(d) for d in docs], ensure_ascii=False), file=out)
            else:
                for d in docs:
                    print(f"{d.relpath} · {d.title}" + (f" — {d.summary}" if d.summary else ""), file=out)
                if not docs:
                    print("(sin documentos)", file=out)
        elif args.command == "search":
            hits = brain.search(scope, key, " ".join(args.terms), category, args.limit)
            if args.json:
                print(json.dumps([{**_meta_dict(h.meta), "excerpt": h.excerpt} for h in hits], ensure_ascii=False),
                      file=out)
            else:
                for h in hits:
                    print(f"{h.meta.relpath} · {h.meta.title}\n  {h.excerpt}", file=out)
                if not hits:
                    print("(sin resultados)", file=out)
        elif args.command == "read":
            doc = brain.read(scope, key, args.relpath)
            body = doc.body
            truncated = bool(args.max_chars) and len(body) > args.max_chars
            if truncated:
                body = body[: args.max_chars].rstrip() + "\n…[recortado]"
            if args.json:
                print(json.dumps({**_meta_dict(doc.meta), "body": body, "truncated": truncated}, ensure_ascii=False),
                      file=out)
            else:
                print(f"# {doc.meta.title}\n\n{body}", file=out, end="" if body.endswith("\n") else "\n")
        return 0
    except BrainError as exc:
        print(str(exc), file=err)
        return EXIT_ERROR
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
