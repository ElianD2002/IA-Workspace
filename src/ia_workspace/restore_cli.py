"""Herramienta CLI offline de recuperación y restauración segura de respaldos (Fase 7B.5A).

Permite listar, inspeccionar, validar y restaurar respaldos de IA Workspace
con la aplicación completamente cerrada, sin inicializar GTK, WebKit ni agentes.
Delega toda la lógica de validación, bloqueos, sustitución atómica y rollback en
RestoreService.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys

from ia_workspace.config import AppPaths, resolve_paths
from ia_workspace.services.restore import (
    BackupValidationReport,
    RestoreError,
    RestoreExclusivityError,
    RestoreSecurityError,
    RestoreService,
    RestoreValidationError,
)


def format_size(num_bytes: int) -> str:
    """Formatea un tamaño en bytes a representación legible."""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def resolve_backup_file(paths: AppPaths, name_or_path: str | Path) -> Path:
    """Resuelve la ruta a un archivo de respaldo dado su nombre o ruta."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p.resolve()
    candidate = paths.backups_dir / name_or_path
    if candidate.is_file():
        return candidate.resolve()
    return p


def cmd_list(paths: AppPaths) -> int:
    """Lista los respaldos disponibles en la carpeta de respaldos."""
    backups_dir = paths.backups_dir
    if not backups_dir.is_dir():
        print(f"No existe el directorio de respaldos: {backups_dir}")
        return 0

    files = [
        f for f in backups_dir.glob("*.tar.gz")
        if f.is_file() and not f.is_symlink() and not f.name.startswith(".")
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if not files:
        print(f"No hay respaldos disponibles en: {backups_dir}")
        return 0

    print(f"Respaldos disponibles en {backups_dir}:\n")
    print(f"  {'NOMBRE':<46} {'FECHA':<18} {'TAMAÑO':<10}")
    print(f"  {'-'*46} {'-'*18} {'-'*10}")
    for f in files:
        st = f.stat()
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%d/%m/%Y %H:%M")
        size = format_size(st.st_size)
        print(f"  {f.name:<46} {mtime:<18} {size:<10}")

    print(f"\nTotal: {len(files)} respaldo(s).")
    return 0


def cmd_info(paths: AppPaths, backup_arg: str, *, validate_only: bool = False) -> int:
    """Valida un respaldo y muestra información detallada sin modificar datos."""
    backup_path = resolve_backup_file(paths, backup_arg)
    if not backup_path.is_file():
        print(f"Error: no se encontró el archivo de respaldo '{backup_arg}'.", file=sys.stderr)
        return 1

    service = RestoreService(paths)
    try:
        report = service.validate_backup(backup_path)
    except (RestoreValidationError, RestoreSecurityError) as exc:
        print(f"\n[FALLO DE VALIDACIÓN] Respaldo '{backup_path.name}':", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[ERROR] No se pudo inspeccionar el respaldo: {exc}", file=sys.stderr)
        return 1

    st = backup_path.stat()
    size_str = format_size(st.st_size)

    if validate_only:
        print(f"Respaldo '{backup_path.name}': VÁLIDO (integridad física SQLite y sumas SHA-256 correctas).")
        return 0

    print("\nInformación del respaldo:")
    print(f"  Archivo:           {backup_path.name}")
    print(f"  Ruta completa:     {backup_path}")
    print(f"  Tamaño archivo:    {size_str} ({st.st_size:,} bytes)")
    print(f"  Fecha creación:    {report.created_at}")
    print(f"  Versión esquema:   SQLite v{report.schema_version} (Manifiesto v{report.manifest_version})")
    print(f"  Total entradas:    {report.total_files} archivos ({format_size(report.total_bytes)} descomprimidos)")
    print(f"  Integridad:        {'Válida (SHA-256 y PRAGMA integrity_check ok)' if report.integrity_ok else 'Inválida'}")
    print("\nContenido inventariado:")
    stats = report.stats or {}
    print(f"  • Proyectos registrados:      {stats.get('projects_count', 0)}")
    print(f"  • Documentos del Cerebro:     {stats.get('documents_count', 0)}")
    print(f"  • Propuestas pendientes:      {stats.get('proposals_count', 0)}")
    print(f"  • Tareas:                     {stats.get('tasks_count', 0)}")
    print(f"  • Plantillas de prompts:      {stats.get('templates_count', 0)}")
    return 0


def prompt_confirmation(backup_path: Path, report: BackupValidationReport) -> bool:
    """Muestra advertencias de seguridad y exige confirmación explícita por escrito."""
    stats = report.stats or {}
    print("\n" + "=" * 70)
    print(" ADVERTENCIA CRÍTICA: RESTAURACIÓN DE DATOS")
    print("=" * 70)
    print("Esta operación sustituirá COMPLETAMENTE:")
    print("  • La base de datos local activa (ia-workspace.db)")
    print("  • Todos los documentos y propuestas del Cerebro Compartido (brain/)")
    print()
    print("Alcance del respaldo a restaurar:")
    print(f"  • Archivo:                           {backup_path.name}")
    print(f"  • Fecha de creación del respaldo:    {report.created_at}")
    print(f"  • Proyectos registrados a restaurar: {stats.get('projects_count', 0)}")
    print(f"  • Documentos del Cerebro a restaurar:{stats.get('documents_count', 0):>2}")
    print(f"  • Propuestas pendientes a restaurar: {stats.get('proposals_count', 0):>2}")
    print(f"  • Tareas a restaurar:                {stats.get('tasks_count', 0):>2}")
    print(f"  • Plantillas de prompts a restaurar: {stats.get('templates_count', 0):>2}")
    print()
    print("Notas importantes de seguridad:")
    print("  • Los archivos y código fuente de proyectos externos NO están incluidos")
    print("    en el respaldo y NO serán modificados ni eliminados.")
    print("  • Se conservan las sesiones de WebKit, registros de respaldo y configuraciones.")
    print("  • IA Workspace debe permanecer completamente CERRADO durante todo el proceso.")
    print("-" * 70)
    print("Para confirmar de forma irrevocable y proceder con la restauración,")
    print("escribe exactamente la palabra 'RESTAURAR' en mayúsculas.")
    print("-" * 70)

    try:
        user_input = input("Escribe 'RESTAURAR' para proceder o pulsa Enter para cancelar: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nOperación cancelada.")
        return False

    return user_input == "RESTAURAR"


def cmd_restore(paths: AppPaths, backup_arg: str) -> int:
    """Ejecuta el flujo seguro de validación, advertencia, confirmación y restauración."""
    backup_path = resolve_backup_file(paths, backup_arg)
    if not backup_path.is_file():
        print(f"Error: no se encontró el archivo de respaldo '{backup_arg}'.", file=sys.stderr)
        return 1

    service = RestoreService(paths)

    # 1. Validar minuciosamente antes de solicitar confirmación
    print(f"Validando integridad del respaldo '{backup_path.name}'...")
    try:
        report = service.validate_backup(backup_path)
    except (RestoreValidationError, RestoreSecurityError) as exc:
        print(f"\n[ERROR DE VALIDACIÓN] El archivo de respaldo no es válido o está dañado:\n  {exc}", file=sys.stderr)
        print("\nRestauración abortada. No se ha modificado ningún dato activo.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[ERROR INESPERADO AL VALIDAR] {exc}", file=sys.stderr)
        return 1

    # 2. Advertencias detalladas y solicitud explícita de confirmación escrita
    if not prompt_confirmation(backup_path, report):
        print("\nRestauración cancelada por el usuario. No se ha modificado ningún archivo.")
        return 1

    # 3. Invocar exclusivamente RestoreService para la restauración transaccional
    print("\nIniciando restauración transaccional...")
    try:
        service.restore_backup(backup_path, confirmed=True)
        print("\n" + "=" * 70)
        print(" RESTAURACIÓN COMPLETADA CON ÉXITO")
        print("=" * 70)
        print("Se han restaurado correctamente:")
        stats = report.stats or {}
        print(f"  • Base de datos SQLite (esquema v{report.schema_version})")
        print(f"  • {stats.get('projects_count', 0)} proyectos registrados")
        print(
            f"  • {stats.get('documents_count', 0)} documentos y "
            f"{stats.get('proposals_count', 0)} propuestas del Cerebro Compartido"
        )
        print(
            f"  • {stats.get('tasks_count', 0)} tareas y "
            f"{stats.get('templates_count', 0)} plantillas de prompts"
        )
        print("\nYa puedes iniciar IA Workspace con normalidad.")
        return 0

    except RestoreExclusivityError as exc:
        print(f"\n[ERROR DE EXCLUSIVIDAD] {exc}", file=sys.stderr)
        print(
            "\nInstrucciones de seguridad:\n"
            "  1. IA Workspace debe estar completamente cerrado para restaurar.\n"
            "  2. Verifica que no existan procesos activos de la aplicación ni lectores SQLite.\n"
            "  3. Cierra la aplicación y vuelve a ejecutar este comando.",
            file=sys.stderr,
        )
        return 1
    except (RestoreValidationError, RestoreSecurityError) as exc:
        print(f"\n[ERROR DE SEGURIDAD O INTEGRIDAD DURANTE LA RESTAURACIÓN] {exc}", file=sys.stderr)
        print("Se ha efectuado un rollback automático al estado original.", file=sys.stderr)
        return 1
    except RestoreError as exc:
        print(f"\n[ERROR DE RESTAURACIÓN] {exc}", file=sys.stderr)
        print("Se ha efectuado un rollback automático al estado original.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n[ERROR INESPERADO DURANTE LA RESTAURACIÓN] {exc}", file=sys.stderr)
        print("Se ha efectuado un rollback automático al estado original.", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada principal para la herramienta CLI de restauración."""
    parser = argparse.ArgumentParser(
        prog="iaw-restore",
        description="Herramienta segura de recuperación y restauración offline de IA Workspace.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Ruta al directorio de datos de la aplicación (por defecto: resuelto automáticamente)",
    )

    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMANDO")

    # list
    subparsers.add_parser("list", help="Listar respaldos locales disponibles en backups_dir")

    # info
    p_info = subparsers.add_parser("info", help="Mostrar metadatos y validar un archivo de respaldo")
    p_info.add_argument("backup", help="Nombre o ruta del archivo de respaldo (.tar.gz)")

    # validate
    p_val = subparsers.add_parser(
        "validate",
        help="Validar exhaustivamente la integridad del respaldo sin modificar datos",
    )
    p_val.add_argument("backup", help="Nombre o ruta del archivo de respaldo (.tar.gz)")

    # restore
    p_res = subparsers.add_parser(
        "restore",
        help="Restaurar un respaldo previa validación y confirmación explícita",
    )
    p_res.add_argument("backup", help="Nombre o ruta del archivo de respaldo (.tar.gz)")

    args = parser.parse_args(argv)

    if args.data_dir:
        paths = AppPaths(data_dir=args.data_dir.resolve())
    else:
        paths = resolve_paths()

    if args.subcommand is None:
        parser.print_help()
        return 0

    if args.subcommand == "list":
        return cmd_list(paths)
    if args.subcommand == "info":
        return cmd_info(paths, args.backup)
    if args.subcommand == "validate":
        return cmd_info(paths, args.backup, validate_only=True)
    if args.subcommand == "restore":
        return cmd_restore(paths, args.backup)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
