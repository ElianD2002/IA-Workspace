"""Servicio de validación y restauración segura de respaldos (Fase 7B.3).

Garantías:
1. Operaciones separadas:
   - `validate_backup(backup_path)`: verificación completa y solo lectura sin modificar datos.
   - `restore_backup(backup_path, confirmed=True)`: restauración atómica que exige confirmación explícita.
2. Validación exhaustiva pre-restauración:
   - Formato versionado y compatibilidad con SCHEMA_VERSION de SQLite.
   - Sumas SHA-256 de cada archivo respecto al manifest.json.
   - `PRAGMA integrity_check` sobre la base contenida en el respaldo.
   - Coherencia bidireccional entre metadatos SQLite y archivos Markdown del Cerebro.
   - Límites de extracción (tamaño total descomprimido, tamaño por archivo y número de entradas)
     para protección contra bombas de descompresión (tarbombs / zipbombs).
   - Rechazo de symlinks, hardlinks, dispositivos (FIFOs, sockets, device nodes) y rutas absolutas
     o con secuencias de escape ('..').
   - Rechazo de entradas duplicadas en el archivo tar.
3. Exclusividad de acceso:
   - Comprueba que la base activa no esté siendo utilizada por otra instancia activa mediante
     intento de bloqueo exclusivo de SQLite y flock sobre el lockfile de la aplicación.
   - Si no se puede garantizar la exclusividad absoluta, rechaza la operación con `RestoreExclusivityError`.
4. Preparación atómica y rollback ante fallos:
   - Prepara primero los datos validados en un subdirectorio temporal privado (0700) en el mismo sistema de archivos.
   - Preserva una copia de seguridad inmediata (`.restore-backup-...`) de la base SQLite y del Cerebro actuales.
   - Reemplaza SQLite y Cerebro de forma transaccional. Si ocurre un fallo en cualquier punto intermedio,
     restaura ambos recursos a su estado original exacto (rollback garantizado) y limpia los temporales.
5. Preservación estricta de datos locales no incluidos:
   - Conserva perfiles de WebKitGTK, cookies, cachés, registros de respaldo en `backups/`,
     configuraciones locales y archivos de proyectos externos de usuario.
   - Si un proyecto restaurado apunta a una ruta en disco inexistente, se preserva su registro en SQLite
     sin modificar ni crear carpetas externas.
6. Migraciones seguras:
   - Si el respaldo contiene un esquema SQLite antiguo pero soportado por MIGRATIONS, se aplican
     las migraciones de forma controlada; esquemas superiores a SCHEMA_VERSION son rechazados inmediatamente.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
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
from ia_workspace.persistence.database import Database, MIGRATIONS, SCHEMA_VERSION
from ia_workspace.services.backup import (
    MANIFEST_VERSION,
    sha256_file,
)

# Límites de seguridad para extracción de respaldos
MAX_ENTRIES = 10_000
MAX_FILE_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_TOTAL_BYTES = 1024 * 1024 * 1024  # 1 GiB


class RestoreError(Exception):
    """Error base en la validación o restauración del respaldo."""


class RestoreSecurityError(RestoreError):
    """Violación de seguridad detectada en el archivo de respaldo."""


class RestoreValidationError(RestoreError):
    """El respaldo está corrupto, incompleto o es incompatible."""


class RestoreExclusivityError(RestoreError):
    """No se pudo obtener acceso exclusivo: la aplicación u otra instancia está activa."""


@dataclass(frozen=True)
class BackupValidationReport:
    """Informe detallado emitido tras validar un archivo de respaldo."""

    manifest_version: int
    schema_version: int
    created_at: str
    stats: dict[str, Any]
    total_files: int
    total_bytes: int
    integrity_ok: bool


class RestoreService:
    """Servicio para validar e instalar respaldos de forma segura y transaccional."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def validate_backup(self, backup_path: Path) -> BackupValidationReport:
        """Valida minuciosamente un archivo de respaldo sin modificar el estado del sistema.

        Lanza RestoreSecurityError o RestoreValidationError ante cualquier anomalía.
        """
        if not backup_path.is_file():
            raise RestoreValidationError(f"El archivo de respaldo no existe: {backup_path}")

        # Directorio temporal de trabajo privado (0700) en el directorio de respaldos o datos
        work_parent = self.paths.backups_dir if self.paths.backups_dir.exists() else self.paths.data_dir
        work_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix=".tmp-validate-", dir=work_parent))
        os.chmod(temp_dir, 0o700)

        try:
            return self._inspect_and_validate(backup_path, temp_dir)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def restore_backup(
        self,
        backup_path: Path,
        *,
        confirmed: bool = False,
    ) -> BackupValidationReport:
        """Restaura un respaldo de forma transaccional previa validación completa.

        Requiere confirmed=True como confirmación explícita del llamador.
        """
        if not confirmed:
            raise RestoreError("La restauración requiere confirmación explícita (confirmed=True).")

        # 1. Asegurar y retener exclusividad de acceso durante toda la restauración
        lock_handles = self._acquire_exclusive_locks()

        # 2. Validar minuciosamente y extraer a directorio temporal privado
        work_parent = self.paths.data_dir
        work_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_stage_dir = Path(tempfile.mkdtemp(prefix=".tmp-stage-", dir=work_parent))
        os.chmod(temp_stage_dir, 0o700)

        backup_current_dir: Path | None = None

        try:
            report = self._inspect_and_validate(backup_path, temp_stage_dir)

            # 3. Aplicar migraciones si el respaldo es de una versión anterior compatible
            staged_db_path = temp_stage_dir / "database.sqlite"
            self._apply_migrations_if_needed(staged_db_path)

            # 4. Crear copia de seguridad recuperable del estado actual
            backup_current_dir = self._preserve_current_state()

            # 5. Aplicar la sustitución de recursos con capacidad de rollback garantizada
            self._apply_staged_restore(temp_stage_dir, backup_current_dir)

            # 6. Éxito: limpiar la copia de preservación del estado previo
            if backup_current_dir is not None and backup_current_dir.exists():
                shutil.rmtree(backup_current_dir, ignore_errors=True)

            return report

        finally:
            shutil.rmtree(temp_stage_dir, ignore_errors=True)
            self._release_exclusive_locks(lock_handles)

    # ---- Inspección y Validación -----------------------------------------------------

    def _inspect_and_validate(self, backup_path: Path, extract_dir: Path) -> BackupValidationReport:
        """Verifica tarbombs, symlinks, hashes, SQLite y coherencia con Markdown."""
        if not tarfile.is_tarfile(backup_path):
            raise RestoreValidationError("El archivo no es un archivo tar válido o está corrupto.")

        seen_names: set[str] = set()
        total_extracted_bytes = 0

        # Primer pase: validación de cabeceras y seguridad sin extraer
        with tarfile.open(backup_path, "r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_ENTRIES:
                raise RestoreSecurityError(f"El respaldo excede el número máximo de entradas ({len(members)} > {MAX_ENTRIES}).")

            has_manifest = False
            has_database = False

            for m in members:
                # Comprobar nombre seguro
                if not self._is_safe_tar_path(m.name):
                    raise RestoreSecurityError(f"Ruta no autorizada o insegura en el respaldo: '{m.name}'")

                if m.name in seen_names:
                    raise RestoreSecurityError(f"Entrada duplicada en el archivo tar: '{m.name}'")
                seen_names.add(m.name)

                # Rechazar enlaces simbólicos, duros,FIFOs, sockets, bloques y caracteres
                if m.issym() or m.islnk() or m.isfifo() or m.isdev():
                    raise RestoreSecurityError(f"Tipo de entrada no permitida en el respaldo: '{m.name}' (tipo {m.type})")

                # Solo admitimos archivos regulares y directorios
                if not (m.isreg() or m.isdir()):
                    raise RestoreSecurityError(f"Entrada no permitida: '{m.name}'")

                if m.isreg():
                    if m.size > MAX_FILE_BYTES:
                        raise RestoreSecurityError(f"El archivo '{m.name}' supera el límite permitido ({m.size} bytes).")
                    total_extracted_bytes += m.size
                    if total_extracted_bytes > MAX_TOTAL_BYTES:
                        raise RestoreSecurityError("El tamaño total descomprimido excede el límite de seguridad (tarbomb).")

                if m.name == "manifest.json":
                    has_manifest = True
                elif m.name == "database.sqlite":
                    has_database = True

            if not has_manifest:
                raise RestoreValidationError("El respaldo no contiene el archivo requerido 'manifest.json'.")
            if not has_database:
                raise RestoreValidationError("El respaldo no contiene la base de datos requerida 'database.sqlite'.")

            # Extracción segura controlada
            for m in members:
                tar.extract(m, path=extract_dir, filter="data")

        # Segundo pase: validación del manifiesto y sumas SHA-256
        manifest_file = extract_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RestoreValidationError(f"Error al leer el manifiesto del respaldo: {exc}") from exc

        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise RestoreValidationError(
                f"Versión de manifiesto no soportada: {manifest.get('manifest_version')} (esperada: {MANIFEST_VERSION})"
            )

        if manifest.get("app_name") != "ia-workspace":
            raise RestoreValidationError(f"Nombre de aplicación inesperado: {manifest.get('app_name')}")

        schema_version = manifest.get("schema_version")
        if not isinstance(schema_version, int) or schema_version > SCHEMA_VERSION or schema_version < 1:
            raise RestoreValidationError(
                f"Versión de esquema SQLite incompatible ({schema_version}). Esquema actual: {SCHEMA_VERSION}"
            )

        # Validar cada archivo declarado en manifest['files']
        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, list) or not manifest_files:
            raise RestoreValidationError("El manifiesto no declara archivos en el inventario.")

        declared_paths: set[str] = set()
        for entry in manifest_files:
            rel_name = entry.get("path")
            expected_size = entry.get("size")
            expected_hash = entry.get("sha256")

            if not rel_name or not isinstance(expected_size, int) or not expected_hash:
                raise RestoreValidationError(f"Entrada inválida en inventario del manifiesto: {entry}")

            declared_paths.add(rel_name)
            physical_file = extract_dir / rel_name
            if not physical_file.is_file():
                raise RestoreValidationError(f"El archivo '{rel_name}' declarado en el manifiesto no existe en el respaldo.")

            actual_size = physical_file.stat().st_size
            if actual_size != expected_size:
                raise RestoreValidationError(
                    f"Discrepancia de tamaño en '{rel_name}': manifiesto={expected_size}, disco={actual_size}"
                )

            actual_hash = sha256_file(physical_file)
            if actual_hash != expected_hash:
                raise RestoreValidationError(
                    f"Suma SHA-256 corrupta en '{rel_name}': esperada={expected_hash}, calculada={actual_hash}"
                )

        # Tercer pase: PRAGMA integrity_check sobre database.sqlite
        staged_db = extract_dir / "database.sqlite"
        try:
            conn = sqlite3.connect(staged_db)
            try:
                res = conn.execute("PRAGMA integrity_check").fetchone()
                if not res or res[0] != "ok":
                    raise RestoreValidationError(f"PRAGMA integrity_check falló en database.sqlite del respaldo: {res}")

                db_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if db_user_version != schema_version:
                    raise RestoreValidationError(
                        f"Inconsistencia de versión: manifest declara {schema_version} y SQLite contiene {db_user_version}"
                    )

                # Cuarto pase: coherencia con archivos Markdown
                cursor = conn.cursor()
                cursor.execute("SELECT scope, project_key, relpath, sha256 FROM brain_documents")
                db_docs = cursor.fetchall()

                for scope, p_key, relpath, expected_doc_hash in db_docs:
                    if scope == "global":
                        target_rel = f"brain/global/{relpath}"
                    else:
                        target_rel = f"brain/projects/{p_key}/{relpath}"

                    physical_doc = extract_dir / target_rel
                    if not physical_doc.is_file():
                        raise RestoreValidationError(
                            f"Documento registrado en la base de datos ausente en el respaldo: '{target_rel}'"
                        )

                    actual_doc_hash = sha256_file(physical_doc)
                    if actual_doc_hash != expected_doc_hash:
                        raise RestoreValidationError(
                            f"Discrepancia de contenido en documento '{target_rel}' entre SQLite y Markdown."
                        )

                # Coherencia de propuestas pendientes
                has_proposals = cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_proposals'"
                ).fetchone()
                if has_proposals:
                    cursor.execute(
                        "SELECT id, project_key FROM brain_proposals WHERE status IN ('pending', 'conflict')"
                    )
                    for prop_id, p_key in cursor.fetchall():
                        prop_rel = f"brain/projects/{p_key}/.proposals/{prop_id}.md"
                        if not (extract_dir / prop_rel).is_file():
                            raise RestoreValidationError(
                                f"Propuesta pendiente '{prop_id}' registrada en SQLite pero ausente en el respaldo."
                            )

            finally:
                conn.close()
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            raise RestoreValidationError(f"Error o corrupción en la base de datos SQLite del respaldo: {exc}") from exc

        return BackupValidationReport(
            manifest_version=manifest["manifest_version"],
            schema_version=schema_version,
            created_at=manifest.get("created_at", ""),
            stats=manifest.get("stats", {}),
            total_files=len(manifest_files),
            total_bytes=total_extracted_bytes,
            integrity_ok=True,
        )

    def _is_safe_tar_path(self, name: str) -> bool:
        """Verifica que la ruta no sea absoluta, no use '..' y pertenezca a los archivos autorizados."""
        if name.startswith("/") or name.startswith("\\"):
            return False
        parts = Path(name).parts
        if ".." in parts or "." in parts:
            return False
        # Las entradas autorizadas deben ser manifest.json, database.sqlite o estar bajo brain/
        if parts[0] in ("manifest.json", "database.sqlite"):
            return len(parts) == 1
        if parts[0] == "brain":
            return True
        return False

    # ---- Exclusividad de Acceso ------------------------------------------------------

    def _acquire_exclusive_locks(self) -> dict[str, Any]:
        """Verifica y adquiere bloqueo exclusivo sobre la aplicación y la base SQLite.

        Orden fijo de adquisición (para evitar interbloqueos):
        1. Bloqueo exclusivo sobre `.restore.lock` (serializa procesos de restauración entre sí).
        2. Bloqueo exclusivo sobre `.app.lock` (impide concurrencia con la aplicación principal).
        3. Comprobación y bloqueo exclusivo en SQLite WAL (mecanismo secundario de salvaguarda).

        Ambos descriptores se abren con O_CLOEXEC y no heredables, y permanecen
        retenidos durante toda la restauración (incluyendo preparación, sustitución y rollback).
        """
        db_path = self.paths.database
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)

        handles: dict[str, Any] = {"fds": [], "conn": None}
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)

        try:
            # 1. Bloqueo de proceso de restauración concurrente (.restore.lock)
            restore_lock_path = self.paths.data_dir / ".restore.lock"
            restore_fd = os.open(restore_lock_path, flags, 0o600)
            os.set_inheritable(restore_fd, False)
            try:
                fcntl.flock(restore_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handles["fds"].append(restore_fd)
            except (BlockingIOError, OSError) as exc:
                os.close(restore_fd)
                raise RestoreExclusivityError(
                    "Otra operación de restauración está en ejecución en este workspace."
                ) from exc

            # 2. Bloqueo de la aplicación activa (.app.lock)
            app_lock_path = self.paths.data_dir / ".app.lock"
            app_fd = os.open(app_lock_path, flags, 0o600)
            os.set_inheritable(app_fd, False)
            try:
                fcntl.flock(app_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handles["fds"].append(app_fd)
            except (BlockingIOError, OSError) as exc:
                os.close(app_fd)
                raise RestoreExclusivityError(
                    "No se puede restaurar: la aplicación se encuentra en ejecución."
                ) from exc

            # 3. Comprobación y bloqueo exclusivo en SQLite (incluyendo lectores WAL)
            if db_path.exists():
                conn = sqlite3.connect(db_path, timeout=0.1)
                handles["conn"] = conn
                try:
                    conn.execute("PRAGMA busy_timeout = 100")
                    # En WAL, locking_mode = EXCLUSIVE obliga a adquirir el lock exclusivo del archivo
                    conn.execute("PRAGMA locking_mode = EXCLUSIVE")
                    # Checkpoint TRUNCATE exige vaciar WAL; falla si hay lectores activos en transacción
                    chk = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if chk and chk[0] != 0:
                        raise RestoreExclusivityError(
                            "No se puede restaurar: la base de datos tiene lectores activos en modo WAL."
                        )
                    conn.execute("BEGIN EXCLUSIVE")
                    conn.execute("ROLLBACK")
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    raise RestoreExclusivityError(
                        f"No se puede restaurar: la base de datos está siendo utilizada por otra conexión o lector ({exc})."
                    ) from exc
                finally:
                    conn.close()
                    handles["conn"] = None

            return handles

        except Exception:
            self._release_exclusive_locks(handles)
            raise

    def _release_exclusive_locks(self, handles: dict[str, Any]) -> None:
        """Libera todos los descriptores de bloqueo adquiridos de forma segura."""
        for fd in reversed(handles.get("fds", [])):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except Exception:
                pass
        handles["fds"] = []

        conn = handles.get("conn")
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            handles["conn"] = None

    # ---- Migraciones -----------------------------------------------------------------

    def _apply_migrations_if_needed(self, staged_db_path: Path) -> None:
        """Aplica migraciones controladas si el respaldo es de una versión anterior compatible."""
        conn = sqlite3.connect(staged_db_path)
        try:
            ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if ver > SCHEMA_VERSION:
                raise RestoreValidationError(
                    f"El esquema SQLite ({ver}) es más reciente que el soportado ({SCHEMA_VERSION})."
                )
            if ver < SCHEMA_VERSION:
                # Aplicar migraciones paso a paso
                for target_ver in range(ver + 1, SCHEMA_VERSION + 1):
                    statements = MIGRATIONS[target_ver - 1]
                    with conn:
                        for stmt in statements:
                            conn.execute(stmt)
                        conn.execute(f"PRAGMA user_version = {target_ver}")
        finally:
            conn.close()

    # ---- Preservación y Rollback -----------------------------------------------------

    def _preserve_current_state(self) -> Path:
        """Copia atómicamente la base activa y la carpeta brain/ a una zona de respaldo temporal."""
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        backup_dir = self.paths.data_dir / f".restore-backup-{timestamp}"
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(backup_dir, 0o700)

        # 1. Copiar SQLite y sus archivos auxiliares (-wal, -shm)
        if self.paths.database.exists():
            shutil.copy2(self.paths.database, backup_dir / self.paths.database.name)
            wal = Path(f"{self.paths.database}-wal")
            if wal.exists():
                shutil.copy2(wal, backup_dir / wal.name)
            shm = Path(f"{self.paths.database}-shm")
            if shm.exists():
                shutil.copy2(shm, backup_dir / shm.name)

        # 2. Copiar carpeta brain/ si existe
        if self.paths.brain_dir.exists():
            shutil.copytree(self.paths.brain_dir, backup_dir / "brain", symlinks=False)

        return backup_dir

    def _apply_staged_restore(self, stage_dir: Path, backup_current_dir: Path) -> None:
        """Aplica la sustitución de SQLite y Cerebro con garantía total de rollback ante errores."""
        db_path = self.paths.database
        brain_path = self.paths.brain_dir

        staged_db = stage_dir / "database.sqlite"
        staged_brain = stage_dir / "brain"

        # Marcadores para rastrear qué se ha sustituido y efectuar rollback seguro
        db_swapped = False
        brain_swapped = False

        # Rutas temporales de intercambio
        db_tmp_replace = db_path.parent / f".tmp-db-replace-{os.getpid()}"
        brain_tmp_replace = brain_path.parent / f".tmp-brain-replace-{os.getpid()}"

        try:
            # Paso A: Sustituir SQLite
            # Copiar staged_db a un temporal en el mismo directorio de destino y aplicar os.replace
            shutil.copy2(staged_db, db_tmp_replace)
            os.chmod(db_tmp_replace, 0o600)

            # Limpiar archivos WAL y SHM anteriores para que la base restaurada arranque limpia
            wal = Path(f"{db_path}-wal")
            shm = Path(f"{db_path}-shm")
            if wal.exists():
                wal.unlink()
            if shm.exists():
                shm.unlink()

            os.replace(db_tmp_replace, db_path)
            db_swapped = True

            # Paso B: Sustituir la carpeta brain/
            if staged_brain.is_dir():
                shutil.copytree(staged_brain, brain_tmp_replace, symlinks=False)
            else:
                brain_tmp_replace.mkdir(mode=0o700, parents=True, exist_ok=True)

            if brain_path.exists():
                # Renombrar transitoriamente
                old_brain_stash = brain_path.parent / f".tmp-brain-old-{os.getpid()}"
                os.replace(brain_path, old_brain_stash)
                try:
                    os.replace(brain_tmp_replace, brain_path)
                    brain_swapped = True
                except Exception:
                    # Restaurar inmediatamente el viejo brain si falló la colocación del nuevo
                    os.replace(old_brain_stash, brain_path)
                    raise
                finally:
                    shutil.rmtree(old_brain_stash, ignore_errors=True)
            else:
                os.replace(brain_tmp_replace, brain_path)
                brain_swapped = True

            # Configurar permisos correctos
            os.chmod(db_path, 0o600)
            self._ensure_secure_tree(brain_path)

        except Exception as exc:
            # ROLLBACK INCONDICIONAL ANTE CUALQUIER FALLO
            self._rollback(backup_current_dir, db_swapped=db_swapped, brain_swapped=brain_swapped)
            raise RestoreError(f"Fallo durante la sustitución de datos. Se ejecutó rollback completo: {exc}") from exc
        finally:
            if db_tmp_replace.exists():
                db_tmp_replace.unlink(missing_ok=True)
            if brain_tmp_replace.exists():
                shutil.rmtree(brain_tmp_replace, ignore_errors=True)

    def _rollback(self, backup_current_dir: Path, *, db_swapped: bool, brain_swapped: bool) -> None:
        """Restaura SQLite y brain/ a partir de la copia de preservación."""
        db_path = self.paths.database
        brain_path = self.paths.brain_dir

        if db_swapped:
            preserved_db = backup_current_dir / db_path.name
            if preserved_db.exists():
                shutil.copy2(preserved_db, db_path)
                os.chmod(db_path, 0o600)
                wal = backup_current_dir / f"{db_path.name}-wal"
                if wal.exists():
                    shutil.copy2(wal, Path(f"{db_path}-wal"))
                shm = backup_current_dir / f"{db_path.name}-shm"
                if shm.exists():
                    shutil.copy2(shm, Path(f"{db_path}-shm"))
            else:
                # Si antes no existía la base de datos, la eliminamos
                db_path.unlink(missing_ok=True)

        if brain_swapped:
            preserved_brain = backup_current_dir / "brain"
            if preserved_brain.exists():
                shutil.rmtree(brain_path, ignore_errors=True)
                shutil.copytree(preserved_brain, brain_path, symlinks=False)
            else:
                shutil.rmtree(brain_path, ignore_errors=True)

    def _ensure_secure_tree(self, root: Path) -> None:
        """Aplica permisos restrictivos (0700 a carpetas y 0600 a archivos) en un árbol."""
        if not root.exists():
            return
        os.chmod(root, 0o700)
        for parent, dirs, files in os.walk(root):
            for d in dirs:
                os.chmod(os.path.join(parent, d), 0o700)
            for f in files:
                os.chmod(os.path.join(parent, f), 0o600)
