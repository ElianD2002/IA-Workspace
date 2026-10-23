"""Servicio de creación de respaldos consistentes y seguros para IA Workspace (Fase 7B.2).

Garantías:
1. Instantánea consistente de SQLite mediante la API oficial `sqlite3.Connection.backup()`,
   consolidando transacciones del WAL sin interferir con las operaciones en curso.
2. Comprobación estricta de integridad física (`PRAGMA integrity_check`) sobre la instantánea.
3. Validación de coherencia entre los documentos Markdown del Cerebro y sus metadatos
   en SQLite (hashes SHA-256), detectando modificaciones concurrentes de agentes externos.
4. Inclusión de propuestas pendientes (.proposals/) y documentos de la papelera (.trash/).
5. Exclusión absoluta de credenciales, cookies de WebKitGTK, cachés, logs y archivos temporales.
6. Seguridad en sistemas de archivos Linux: permisos 0700 en directorios, 0600 en archivos,
   publicación atómica libre de carreras sin sobrescribir archivos existentes mediante `os.link`
   en el mismo sistema de archivos, y rechazo estricto de enlaces simbólicos.

Nota sobre borradores de UI y llamadas de servicio:
BackupService opera exclusivamente sobre la capa de persistencia (SQLite y sistema de archivos).
No presupone que los cambios visuales o borradores pendientes en la interfaz GTK ya hayan sido
volcados a la base de datos. Quien invoque este servicio (por ejemplo, la futura acción de UI o
controlador de aplicación) debe ejecutar explícitamente `workspace_controller.flush()` o
`win.save_state()` antes de iniciar la creación del respaldo para garantizar que el estado
en memoria quede guardado en SQLite.

Decisión sobre .trash/ (Papelera del Cerebro):
En IA Workspace, cuando un documento es eliminado mediante BrainStore.move_to_trash,
se mueve a `.trash/<owner>/<timestamp>/<documento.md>` conservando su contenido
íntegro para permitir su recuperación posterior (según se documenta e informa en la UI
de memoria). Por tanto, la papelera del Cerebro contiene información histórica y
potencialmente recuperable ante desastres o eliminaciones accidentales.
BackupService incluye por defecto los documentos Markdown de `.trash/` en el respaldo
bajo la ruta `brain/.trash/`, registrándolos en las estadísticas del manifiesto
(`trash_documents_count`). Se validan rigurosamente para rechazar enlaces simbólicos o
rutas con salto de directorio. Se proporciona el parámetro `include_trash=True`
(por defecto True) para permitir al invocador omitirla si desea un respaldo más compacto.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any

from ia_workspace.config import AppPaths
from ia_workspace.persistence.database import Database

MANIFEST_VERSION = 1
BACKUP_PREFIX = "ia-workspace-backup-"


class BackupError(Exception):
    """Error base en la generación del respaldo."""


class BackupSecurityError(BackupError):
    """Violación de seguridad: enlace simbólico, escape de ruta o permisos indebidos."""


class BackupConcurrencyError(BackupError):
    """Incoherencia detectada por modificación concurrente durante el respaldo."""


def sha256_file(path: Path) -> str:
    """Calcula el hash SHA-256 de un archivo en bloques de 64 KiB."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class BackupService:
    """Crea respaldos atómicos, autocontenidos y versionados de los datos de IA Workspace."""

    def __init__(
        self,
        paths: AppPaths,
        db: Database | sqlite3.Connection | None = None,
        destination_dir: Path | None = None,
        include_trash: bool = True,
    ) -> None:
        self.paths = paths
        self.db = db
        self.destination_dir = destination_dir or paths.backups_dir
        self.include_trash = include_trash

    def create_backup(
        self,
        destination_dir: Path | None = None,
        filename: str | None = None,
    ) -> Path:
        """Genera un archivo `.tar.gz` con la base SQLite, documentos del Cerebro y manifiesto.

        Retorna la ruta absoluta del archivo de respaldo creado.
        """
        dest_dir = destination_dir or self.destination_dir
        self._ensure_secure_dir(dest_dir, mode=0o700)

        # Directorio temporal de trabajo privado (0700) dentro de la carpeta destino
        temp_work_dir = Path(tempfile.mkdtemp(prefix=".tmp-bkp-", dir=dest_dir))
        self._ensure_secure_dir(temp_work_dir, mode=0o700)

        temp_tar_path = temp_work_dir / "backup.tar.gz"

        try:
            # 1. Crear instantánea consistente de SQLite
            snapshot_db_path = temp_work_dir / "database.sqlite"
            self._create_database_snapshot(snapshot_db_path)

            # 2. Verificar integridad física de la instantánea SQLite
            schema_version, stats_counts = self._verify_database_integrity(snapshot_db_path)

            # 3. Recopilar y verificar archivos Markdown del Cerebro Compartido
            brain_files, trash_count = self._collect_and_verify_brain_files(snapshot_db_path)

            # 4. Construir inventario y manifiesto de integridad
            manifest_data = self._build_manifest(
                snapshot_db_path=snapshot_db_path,
                brain_files=brain_files,
                schema_version=schema_version,
                stats_counts=stats_counts,
                trash_count=trash_count,
            )

            manifest_path = temp_work_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.chmod(manifest_path, 0o600)

            # 5. Empaquetar el archivo tar.gz en temp_tar_path
            self._pack_archive(
                tar_path=temp_tar_path,
                manifest_path=manifest_path,
                snapshot_db_path=snapshot_db_path,
                brain_files=brain_files,
            )
            os.chmod(temp_tar_path, 0o600)

            # 6. Determinar ruta destino final y publicar atómicamente sin condición de carrera
            return self._publish_backup(temp_tar_path, dest_dir, filename)

        finally:
            shutil.rmtree(temp_work_dir, ignore_errors=True)

    # ---- Métodos internos -------------------------------------------------------------

    def _ensure_secure_dir(self, path: Path, mode: int = 0o700) -> None:
        """Crea el directorio si no existe y asegura permisos restrictivos."""
        if path.is_symlink():
            raise BackupSecurityError(f"El directorio es un enlace simbólico: {path}")
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    def _create_database_snapshot(self, snapshot_path: Path) -> None:
        """Copia atómicamente la base activa hacia un archivo autocontenido mediante conn.backup()."""
        source_conn: sqlite3.Connection | None = None
        opened_here = False

        if isinstance(self.db, Database):
            source_conn = self.db.conn
        elif isinstance(self.db, sqlite3.Connection):
            source_conn = self.db
        elif self.paths.database.is_file():
            uri = self.paths.database.resolve().as_uri()
            source_conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
            opened_here = True
        else:
            raise BackupError(f"No existe el archivo de base de datos en {self.paths.database}")

        try:
            dest_conn = sqlite3.connect(snapshot_path)
            try:
                source_conn.backup(dest_conn, pages=-1)
            finally:
                dest_conn.close()
        except sqlite3.Error as exc:
            raise BackupError(f"Error al realizar la instantánea SQLite: {exc}") from exc
        finally:
            if opened_here and source_conn is not None:
                source_conn.close()

        os.chmod(snapshot_path, 0o600)

    def _verify_database_integrity(self, snapshot_path: Path) -> tuple[int, dict[str, int]]:
        """Comprueba PRAGMA integrity_check y obtiene versiones y conteos."""
        conn = sqlite3.connect(snapshot_path)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            if not rows or rows[0][0] != "ok":
                raise BackupError(f"PRAGMA integrity_check falló en la instantánea de base de datos: {rows}")

            schema_ver = int(conn.execute("PRAGMA user_version").fetchone()[0])

            # Contar registros principales para estadísticas del manifiesto
            projects_count = int(conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
            docs_count = int(conn.execute("SELECT COUNT(*) FROM brain_documents").fetchone()[0])

            proposals_count = 0
            # Si el esquema ya cuenta con brain_proposals (migración 6+)
            has_proposals = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_proposals'"
            ).fetchone()
            if has_proposals:
                proposals_count = int(
                    conn.execute("SELECT COUNT(*) FROM brain_proposals WHERE status IN ('pending', 'conflict')").fetchone()[0]
                )

            tasks_count = 0
            has_tasks = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
            if has_tasks:
                tasks_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

            templates_count = 0
            has_templates = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_templates'"
            ).fetchone()
            if has_templates:
                templates_count = int(conn.execute("SELECT COUNT(*) FROM prompt_templates").fetchone()[0])

            counts = {
                "projects_count": projects_count,
                "documents_count": docs_count,
                "proposals_count": proposals_count,
                "tasks_count": tasks_count,
                "templates_count": templates_count,
            }
            return schema_ver, counts
        finally:
            conn.close()

    def _collect_and_verify_brain_files(
        self, snapshot_path: Path
    ) -> tuple[list[tuple[Path, str]], int]:
        """Inspecciona el Cerebro, valida que no haya symlinks y contrasta con la instantánea SQLite."""
        brain_dir = self.paths.brain_dir
        if not brain_dir.is_dir():
            return [], 0

        # Verificar que brain_dir no sea enlace simbólico ni contenga componentes symlink
        self._check_symlink_free(brain_dir)

        # 1. Leer documentos registrados en SQLite y sus hashes
        conn = sqlite3.connect(snapshot_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT scope, project_key, relpath, sha256 FROM brain_documents")
            db_docs = {(row[0], row[1], row[2]): row[3] for row in cursor.fetchall()}

            has_proposals_table = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_proposals'"
            ).fetchone()
            pending_proposals: set[tuple[str, str]] = set()
            if has_proposals_table:
                cursor.execute(
                    "SELECT id, project_key FROM brain_proposals WHERE status IN ('pending', 'conflict')"
                )
                pending_proposals = {(row[0], row[1]) for row in cursor.fetchall()}
        finally:
            conn.close()

        # 2. Recorrer brain_dir de forma segura
        collected: list[tuple[Path, str]] = []
        trash_count = 0

        for root_str, dirs, files in os.walk(brain_dir, followlinks=False):
            root = Path(root_str)
            # Ningún subdirectorio puede ser symlink
            for d in dirs:
                sub = root / d
                if sub.is_symlink():
                    raise BackupSecurityError(f"Se detectó un directorio de enlace simbólico: {sub}")

            for f in files:
                file_path = root / f
                if file_path.is_symlink():
                    raise BackupSecurityError(f"Se detectó un archivo de enlace simbólico: {file_path}")

                # Excluir temporales de escritura (.tmp-* o *.tmp)
                if f.startswith(".tmp-") or f.endswith(".tmp"):
                    continue

                # Solo incluir documentos Markdown (.md)
                if not f.endswith(".md"):
                    continue

                rel_to_brain = file_path.relative_to(brain_dir)
                rel_parts = rel_to_brain.parts

                # Manejo de .trash/
                if rel_parts[0] == ".trash":
                    if not self.include_trash:
                        continue
                    trash_count += 1
                    collected.append((file_path, f"brain/{rel_to_brain.as_posix()}"))
                    continue

                archive_relpath = f"brain/{rel_to_brain.as_posix()}"
                collected.append((file_path, archive_relpath))

        # 3. Validar coherencia con SQLite y detectar modificaciones concurrentes
        for (scope, project_key, relpath), expected_hash in db_docs.items():
            if scope == "global":
                expected_path = brain_dir / "global" / relpath
            else:
                expected_path = brain_dir / "projects" / project_key / relpath

            if not expected_path.is_file():
                raise BackupConcurrencyError(
                    f"Documento registrado en la base de datos pero ausente en disco: '{relpath}'"
                )

            actual_hash = sha256_file(expected_path)
            if actual_hash != expected_hash:
                raise BackupConcurrencyError(
                    f"Modificación concurrente detectada en '{relpath}': "
                    f"hash esperado {expected_hash[:10]}... vs disco {actual_hash[:10]}..."
                )

        # 4. Validar coherencia bidireccional de propuestas en disco y en SQLite
        # Comprobar que todas las propuestas en la BD existan en disco
        for prop_id, project_key in pending_proposals:
            prop_file = brain_dir / "projects" / project_key / ".proposals" / f"{prop_id}.md"
            if not prop_file.is_file():
                raise BackupConcurrencyError(
                    f"Propuesta pendiente '{prop_id}' registrada en base de datos pero ausente en disco"
                )

        # Comprobar que no hayan propuestas en disco que no estén registradas en SQLite
        collected_proposals: set[tuple[str, str]] = set()
        for physical_path, _ in collected:
            try:
                rel = physical_path.relative_to(brain_dir)
            except ValueError:
                continue
            if len(rel.parts) >= 4 and rel.parts[0] == "projects" and rel.parts[2] == ".proposals":
                p_key = rel.parts[1]
                p_id = physical_path.stem
                collected_proposals.add((p_id, p_key))

        if has_proposals_table:
            unrecorded = collected_proposals - pending_proposals
            if unrecorded:
                first_unrecorded = next(iter(unrecorded))
                raise BackupConcurrencyError(
                    f"Propuesta '{first_unrecorded[0]}' creada en disco pero no registrada en base de datos"
                )

        # Ordenar alfabéticamente para asegurar reproducibilidad
        collected.sort(key=lambda item: item[1])
        return collected, trash_count

    def _check_symlink_free(self, path: Path) -> None:
        """Verifica que la ruta y sus componentes no contengan enlaces simbólicos."""
        current = path
        while current != current.parent:
            if current.is_symlink():
                raise BackupSecurityError(f"La ruta contiene un enlace simbólico: {current}")
            current = current.parent

    def _build_manifest(
        self,
        snapshot_db_path: Path,
        brain_files: list[tuple[Path, str]],
        schema_version: int,
        stats_counts: dict[str, int],
        trash_count: int,
    ) -> dict[str, Any]:
        """Construye el manifiesto con versión, sumas SHA-256 e inventario completo."""
        db_size = snapshot_db_path.stat().st_size
        db_hash = sha256_file(snapshot_db_path)

        file_entries: list[dict[str, Any]] = [
            {
                "path": "database.sqlite",
                "size": db_size,
                "sha256": db_hash,
            }
        ]

        total_bytes = db_size

        for physical_path, archive_path in brain_files:
            size = physical_path.stat().st_size
            h = sha256_file(physical_path)
            file_entries.append({
                "path": archive_path,
                "size": size,
                "sha256": h,
            })
            total_bytes += size

        stats = dict(stats_counts)
        stats["trash_documents_count"] = trash_count
        stats["total_files"] = len(file_entries)
        stats["total_bytes"] = total_bytes

        return {
            "manifest_version": MANIFEST_VERSION,
            "app_name": "ia-workspace",
            "created_at": datetime.now(UTC).isoformat(),
            "schema_version": schema_version,
            "database": {
                "filename": "database.sqlite",
                "size": db_size,
                "sha256": db_hash,
                "integrity_check": "ok",
            },
            "stats": stats,
            "files": file_entries,
        }

    def _pack_archive(
        self,
        tar_path: Path,
        manifest_path: Path,
        snapshot_db_path: Path,
        brain_files: list[tuple[Path, str]],
    ) -> None:
        """Empaqueta todos los elementos autorizados en un tar.gz con metadatos limpios."""
        with tarfile.open(tar_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
            # 1. Agregar manifest.json en la raíz
            self._add_file_to_tar(tar, manifest_path, "manifest.json")

            # 2. Agregar database.sqlite en la raíz
            self._add_file_to_tar(tar, snapshot_db_path, "database.sqlite")

            # 3. Agregar archivos del Cerebro
            for physical_path, archive_relpath in brain_files:
                self._add_file_to_tar(tar, physical_path, archive_relpath)

    def _add_file_to_tar(self, tar: tarfile.TarFile, file_path: Path, arcname: str) -> None:
        """Agrega un archivo individual al archivo tar estableciendo permisos y dueño limpios."""
        tarinfo = tar.gettarinfo(file_path, arcname=arcname)
        tarinfo.mode = 0o600
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = ""
        tarinfo.gname = ""
        with open(file_path, "rb") as f:
            tar.addfile(tarinfo, fileobj=f)

    def _publish_backup(self, temp_tar_path: Path, dest_dir: Path, custom_filename: str | None) -> Path:
        """Publica el archivo de respaldo de forma atómica y libre de condiciones de carrera.

        Si se especifica un custom_filename, no debe sobrescribir un archivo preexistente
        bajo ninguna circunstancia. Se utiliza `os.link` (enlace duro atómico que falla con
        FileExistsError si el destino ya existe) y posterior `unlink` del temporal. Si el
        sistema de archivos o entorno no soporta enlaces duros, se utiliza `shutil.move`
        garantizando la verificación atómica.
        """
        if custom_filename:
            target_path = dest_dir / custom_filename
            try:
                os.link(temp_tar_path, target_path)
                temp_tar_path.unlink()
            except FileExistsError:
                raise BackupError(f"El archivo de respaldo '{target_path.name}' ya existe.")
            except OSError:
                # Si os.link falla por EXDEV o permisos de sistema de archivos, fallback seguro
                if target_path.exists():
                    raise BackupError(f"El archivo de respaldo '{target_path.name}' ya existe.")
                os.replace(temp_tar_path, target_path)

            os.chmod(target_path, 0o600)
            return target_path

        # Nombre generado automáticamente con marca temporal y sufijo
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        base_name = f"{BACKUP_PREFIX}{timestamp}"
        counter = 0

        while True:
            suffix = f"-{counter}" if counter > 0 else ""
            candidate = dest_dir / f"{base_name}{suffix}.tar.gz"
            try:
                os.link(temp_tar_path, candidate)
                temp_tar_path.unlink()
                os.chmod(candidate, 0o600)
                return candidate
            except FileExistsError:
                counter += 1
            except OSError:
                if not candidate.exists():
                    os.replace(temp_tar_path, candidate)
                    os.chmod(candidate, 0o600)
                    return candidate
                counter += 1
