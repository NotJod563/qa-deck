"""Inspection, planning, backup, and file operations for License Manager."""

import hashlib
import json
import os
import shutil
import stat
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from logging import Logger, getLogger
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from uuid import NAMESPACE_URL, uuid4, uuid5

from qa_deck.domain import (
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    RollbackStatus,
)
from qa_deck.plugins import RiskLevel
from qa_deck.plugins.builtin.license_manager.models import (
    BackupInspectionResult,
    ChangePlan,
    LicenseFileState,
    LicenseFileStatus,
    LicenseInspectionResult,
    LicenseInspectionStatus,
    LicenseManagerConfiguration,
    LicenseOperationResult,
    PlannedChange,
    SkippedChange,
)
from qa_deck.storage import OperationLogRepository

PLUGIN_IDENTIFIER = "license-manager"
HIDE_ACTION = "hide-licenses"
RESTORE_ACTION = "restore-licenses"
_LOGGER = getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _FileProbe:
    exists: bool | None
    is_file: bool | None
    size: int | None = None
    mtime_ns: int | None = None


class MoveWithoutOverwriteError(OSError):
    """A no-clobber move failed, possibly after creating its destination."""

    def __init__(self, message: str, *, destination_created: bool) -> None:
        super().__init__(message)
        self.destination_created = destination_created


def move_without_overwrite(source: Path, destination: Path) -> None:
    """Move a regular file with an atomic no-clobber destination creation."""
    if source.parent.resolve() != destination.parent.resolve():
        raise MoveWithoutOverwriteError(
            "Source and destination must share a directory",
            destination_created=False,
        )
    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        raise MoveWithoutOverwriteError(
            "Destination could not be created without overwrite",
            destination_created=False,
        ) from error
    try:
        source.unlink()
    except OSError as error:
        raise MoveWithoutOverwriteError(
            "Destination was created but source could not be removed",
            destination_created=True,
        ) from error


class LicenseManagerService:
    def inspect(
        self,
        configuration: PluginConfiguration | None,
    ) -> LicenseInspectionResult:
        if configuration is None:
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.NOT_CONFIGURED,
                directory=None,
                files=(),
                message="License Manager ще не налаштовано для цього продукту.",
            )
        try:
            typed = LicenseManagerConfiguration.from_plugin_configuration(
                configuration
            )
        except (TypeError, ValueError):
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.ERROR,
                directory=None,
                files=(),
                message="Збережена конфігурація License Manager некоректна.",
            )
        if not configuration.enabled:
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.DISABLED,
                directory=None,
                files=(),
                message="License Manager вимкнений для цього продукту.",
            )

        directory = Path(typed.license_directory)
        try:
            metadata = directory.stat(follow_symlinks=False)
        except FileNotFoundError:
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.DIRECTORY_MISSING,
                directory=typed.license_directory,
                files=(),
                message="Налаштований каталог ліцензій не існує.",
            )
        except OSError:
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.ERROR,
                directory=typed.license_directory,
                files=(),
                message="Не вдалося перевірити каталог ліцензій.",
            )

        if not S_ISDIR(metadata.st_mode):
            return LicenseInspectionResult(
                status=LicenseInspectionStatus.NOT_A_DIRECTORY,
                directory=typed.license_directory,
                files=(),
                message="Шлях License Manager не є каталогом.",
            )

        states = tuple(
            self._inspect_file(directory, filename)
            for filename in typed.license_files
        )
        return LicenseInspectionResult(
            status=LicenseInspectionStatus.READY,
            directory=typed.license_directory,
            files=states,
        )

    def build_plan(
        self,
        product_id: str,
        configuration: PluginConfiguration | None,
        action_identifier: str,
    ) -> ChangePlan:
        if action_identifier not in {HIDE_ACTION, RESTORE_ACTION}:
            raise ValueError("Unsupported License Manager action")

        inspection = self.inspect(configuration)
        changes: list[PlannedChange] = []
        skipped: list[SkippedChange] = []
        warnings: list[str] = []
        blocking_error: str | None = None

        if inspection.status is not LicenseInspectionStatus.READY:
            blocking_error = inspection.message or "Операція зараз недоступна."
            warnings.append(blocking_error)
        else:
            for state in inspection.files:
                self._append_plan_item(
                    action_identifier,
                    state,
                    changes,
                    skipped,
                )
            if any(
                state.status is LicenseFileStatus.ERROR
                for state in inspection.files
            ):
                blocking_error = "Стан одного або кількох файлів недоступний."
            elif any(
                state.status is LicenseFileStatus.CONFLICT
                for state in inspection.files
            ):
                blocking_error = "Спочатку усуньте конфлікт ліцензійних файлів."
            if blocking_error:
                warnings.append(blocking_error)

        fingerprint = self._fingerprint(
            product_id,
            action_identifier,
            inspection,
        )
        return ChangePlan(
            product_id=product_id,
            plugin_identifier=PLUGIN_IDENTIFIER,
            action_identifier=action_identifier,
            risk_level=RiskLevel.CAUTION,
            requires_confirmation=True,
            changes=tuple(changes),
            skipped=tuple(skipped),
            warnings=tuple(warnings),
            fingerprint=fingerprint,
            blocking_error=blocking_error,
        )

    def execute(
        self,
        *,
        product_id: str,
        configuration: PluginConfiguration | None,
        action_identifier: str,
        expected_fingerprint: str,
        confirmed: bool,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> LicenseOperationResult:
        active_logger = logger or _LOGGER
        if action_identifier not in {HIDE_ACTION, RESTORE_ACTION}:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                "Невідома дія License Manager.",
                0,
                0,
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
        if configuration is None:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                "License Manager ще не налаштовано для цього продукту.",
                0,
                0,
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
        try:
            typed = LicenseManagerConfiguration.from_plugin_configuration(
                configuration
            )
        except ValueError:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                "Збережена конфігурація License Manager некоректна.",
                0,
                0,
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
        if configuration.product_id != product_id:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                "Конфігурація не належить вибраному продукту.",
                0,
                0,
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
        if not configuration.enabled:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                "License Manager вимкнений для цього продукту.",
                0,
                0,
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )

        plan = self.build_plan(product_id, configuration, action_identifier)
        if plan.blocking_error:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.BLOCKED,
                plan.blocking_error,
                0,
                len(plan.skipped),
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
        if plan.fingerprint != expected_fingerprint:
            result = self._finish(
                product_id,
                action_identifier,
                OperationStatus.REJECTED,
                "Стан файлів змінився. Сформуйте Change Plan повторно.",
                0,
                len(plan.skipped),
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )
            return replace(result, stale_plan=True)
        if not plan.has_changes:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.NO_CHANGES,
                "Немає файлів, які потрібно змінити.",
                0,
                len(plan.skipped),
                0,
                RollbackStatus.NOT_REQUIRED,
                False,
                operation_logs,
                logger=active_logger,
            )
        if not confirmed:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.REJECTED,
                "Операцію не підтверджено.",
                0,
                len(plan.skipped),
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                logger=active_logger,
            )

        directory = Path(typed.license_directory)
        operation_id = str(uuid4())
        timestamp = datetime.now(UTC)
        try:
            self._create_backup(
                backup_root=Path(backup_root),
                product_id=product_id,
                operation_id=operation_id,
                timestamp=timestamp,
                action_identifier=action_identifier,
                directory=directory,
                changes=plan.changes,
            )
        except OSError:
            return self._finish(
                product_id,
                action_identifier,
                OperationStatus.FAILED,
                "Не вдалося створити backup. Файли не змінено.",
                0,
                len(plan.skipped),
                1,
                RollbackStatus.NOT_ATTEMPTED,
                False,
                operation_logs,
                operation_id,
                timestamp,
                logger=active_logger,
            )

        completed: list[tuple[Path, Path]] = []
        try:
            for change in plan.changes:
                source = self._safe_child(directory, change.source_name)
                target = self._safe_child(directory, change.target_name)
                move_without_overwrite(source, target)
                completed.append((source, target))
        except OSError as move_error:
            rolled_back = 0
            for source, target in reversed(completed):
                try:
                    move_without_overwrite(target, source)
                    rolled_back += 1
                except MoveWithoutOverwriteError:
                    continue
            partial_move_count = (
                1
                if isinstance(move_error, MoveWithoutOverwriteError)
                and move_error.destination_created
                else 0
            )
            rollback_status = (
                RollbackStatus.COMPLETE
                if rolled_back == len(completed) and not partial_move_count
                else RollbackStatus.PARTIAL
            )
            remaining_changes = len(completed) - rolled_back + partial_move_count
            status = (
                OperationStatus.FAILED
                if rollback_status is RollbackStatus.COMPLETE
                else OperationStatus.PARTIAL
            )
            return self._finish(
                product_id,
                action_identifier,
                status,
                (
                    "Створено destination, але source не вдалося прибрати; "
                    "утворився конфлікт файлів."
                    if isinstance(move_error, MoveWithoutOverwriteError)
                    and move_error.destination_created
                    else "Файлова операція завершилася помилкою; виконано rollback."
                ),
                remaining_changes,
                len(plan.skipped),
                1,
                rollback_status,
                True,
                operation_logs,
                operation_id,
                timestamp,
                logger=active_logger,
            )

        return self._finish(
            product_id,
            action_identifier,
            OperationStatus.SUCCESS,
            "Операцію успішно завершено.",
            len(completed),
            len(plan.skipped),
            0,
            RollbackStatus.NOT_REQUIRED,
            True,
            operation_logs,
            operation_id,
            timestamp,
            logger=active_logger,
        )

    def inspect_backup(
        self,
        product_id: str,
        backup_root: str | Path,
    ) -> BackupInspectionResult:
        canonical_root = Path(backup_root).resolve()
        product_root = self._product_backup_root(canonical_root, product_id)
        try:
            self._assert_contained_directory(product_root, canonical_root)
            candidates = list(product_root.iterdir())
        except FileNotFoundError:
            return BackupInspectionResult(False, 0, None, None, 0, False)
        except OSError:
            return BackupInspectionResult(
                False,
                0,
                None,
                None,
                0,
                False,
                "Не вдалося прочитати каталог backup.",
            )

        operation_directories: list[Path] = []
        invalid_manifest = False
        for path in candidates:
            try:
                self._assert_contained_directory(path, canonical_root)
            except OSError:
                invalid_manifest = True
                continue
            operation_directories.append(path)

        manifests: list[tuple[datetime, str, int]] = []
        for directory in operation_directories:
            try:
                data = json.loads(
                    (directory / "manifest.json").read_text(encoding="utf-8")
                )
                manifests.append(self._parse_backup_manifest(data))
            except (OSError, ValueError, KeyError, TypeError):
                invalid_manifest = True

        if not manifests:
            return BackupInspectionResult(
                bool(candidates),
                len(candidates),
                None,
                None,
                0,
                False,
                (
                    "Backup знайдено, але manifest недоступний або пошкоджений."
                    if candidates
                    else None
                ),
            )

        latest_timestamp, latest_action, file_count = max(
            manifests, key=lambda item: item[0]
        )
        return BackupInspectionResult(
            True,
            len(candidates),
            latest_timestamp,
            latest_action,
            file_count,
            not invalid_manifest,
            (
                "Один або кілька manifest недоступні або пошкоджені."
                if invalid_manifest
                else None
            ),
        )

    def _inspect_file(self, directory: Path, filename: str) -> LicenseFileState:
        original = _probe(directory / filename)
        hidden = _probe(directory / f"{filename}.hidden")
        if original.exists is None or hidden.exists is None:
            return LicenseFileState(
                filename,
                LicenseFileStatus.ERROR,
                "Не вдалося визначити стан файла.",
            )
        if (original.exists and not original.is_file) or (
            hidden.exists and not hidden.is_file
        ):
            return LicenseFileState(
                filename,
                LicenseFileStatus.ERROR,
                "Ліцензійний шлях не є звичайним файлом.",
            )
        if original.exists and hidden.exists:
            status = LicenseFileStatus.CONFLICT
            message = "Одночасно існують активний і прихований файли."
        elif original.exists:
            status = LicenseFileStatus.ACTIVE
            message = "Активний файл знайдено."
        elif hidden.exists:
            status = LicenseFileStatus.HIDDEN
            message = "Файл уже прихований."
        else:
            status = LicenseFileStatus.MISSING
            message = "Файл не знайдено."
        return LicenseFileState(
            filename,
            status,
            message,
            original.size,
            original.mtime_ns,
            hidden.size,
            hidden.mtime_ns,
        )

    def _append_plan_item(
        self,
        action_identifier: str,
        state: LicenseFileState,
        changes: list[PlannedChange],
        skipped: list[SkippedChange],
    ) -> None:
        if (
            action_identifier == HIDE_ACTION
            and state.status is LicenseFileStatus.ACTIVE
        ):
            changes.append(PlannedChange(state.filename, f"{state.filename}.hidden"))
            return
        if (
            action_identifier == RESTORE_ACTION
            and state.status is LicenseFileStatus.HIDDEN
        ):
            changes.append(PlannedChange(f"{state.filename}.hidden", state.filename))
            return
        reasons = {
            LicenseFileStatus.ACTIVE: "Файл уже активний.",
            LicenseFileStatus.HIDDEN: "Файл уже прихований.",
            LicenseFileStatus.MISSING: "Файл відсутній.",
            LicenseFileStatus.CONFLICT: "Виявлено конфлікт файлів.",
            LicenseFileStatus.ERROR: "Стан файла не вдалося визначити.",
        }
        skipped.append(SkippedChange(state.filename, reasons[state.status]))

    def _fingerprint(
        self,
        product_id: str,
        action_identifier: str,
        inspection: LicenseInspectionResult,
    ) -> str:
        payload = {
            "product_id": product_id,
            "action": action_identifier,
            "status": inspection.status.value,
            "directory": inspection.directory,
            "files": [asdict(item) for item in inspection.files],
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _create_backup(
        self,
        *,
        backup_root: Path,
        product_id: str,
        operation_id: str,
        timestamp: datetime,
        action_identifier: str,
        directory: Path,
        changes: tuple[PlannedChange, ...],
    ) -> Path:
        resolved_directory = directory.resolve(strict=True)
        canonical_root = backup_root.resolve()
        canonical_root.mkdir(parents=True, exist_ok=True)
        canonical_root = canonical_root.resolve(strict=True)
        self._assert_contained_directory(canonical_root, canonical_root)
        if canonical_root.is_relative_to(resolved_directory):
            raise OSError("Backup root must be outside license directory")

        plugin_root = canonical_root / PLUGIN_IDENTIFIER
        self._create_or_validate_directory(plugin_root, canonical_root)
        product_root = self._product_backup_root(canonical_root, product_id)
        self._create_or_validate_directory(product_root, canonical_root)
        operation_root = product_root / operation_id
        operation_root.mkdir(exist_ok=False)
        self._assert_contained_directory(operation_root, canonical_root)
        files_root = operation_root / "files"
        files_root.mkdir(exist_ok=False)
        self._assert_contained_directory(files_root, canonical_root)

        manifest_files: list[dict[str, object]] = []
        try:
            for change in changes:
                source = self._safe_child(resolved_directory, change.source_name)
                probe = _probe(source)
                if probe.exists is not True or probe.is_file is not True:
                    raise OSError("Backup source is unavailable")
                backup_file = self._safe_child(files_root, change.source_name)
                self._assert_contained_directory(
                    backup_file.parent, canonical_root
                )
                self._copy_file_exclusive(
                    source, backup_file, canonical_root
                )
                manifest_files.append(
                    {
                        "source_name": change.source_name,
                        "backup_name": str(backup_file.relative_to(operation_root)),
                        "size": probe.size,
                        "modified_time_ns": probe.mtime_ns,
                    }
                )

            manifest = {
                "timestamp": timestamp.isoformat(),
                "product_id": product_id,
                "plugin_identifier": PLUGIN_IDENTIFIER,
                "action_identifier": action_identifier,
                "files": manifest_files,
            }
            self._write_manifest_exclusive(
                operation_root / "manifest.json",
                manifest,
                canonical_root,
            )
        except (OSError, ValueError):
            self._cleanup_partial_backup(operation_root, canonical_root)
            raise
        return operation_root

    def _copy_file_exclusive(
        self,
        source: Path,
        destination: Path,
        canonical_root: Path,
    ) -> None:
        self._assert_contained_directory(destination.parent, canonical_root)
        try:
            with source.open("rb") as source_file, destination.open("xb") as target:
                shutil.copyfileobj(source_file, target)
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError:
            raise

    def _write_manifest_exclusive(
        self,
        destination: Path,
        manifest: dict[str, object],
        canonical_root: Path,
    ) -> None:
        self._assert_contained_directory(destination.parent, canonical_root)
        with destination.open("x", encoding="utf-8") as manifest_file:
            json.dump(manifest, manifest_file, ensure_ascii=False, indent=2)
            manifest_file.write("\n")

    def _create_or_validate_directory(
        self,
        directory: Path,
        canonical_root: Path,
    ) -> None:
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError:
            pass
        self._assert_contained_directory(directory, canonical_root)

    def _assert_contained_directory(
        self,
        directory: Path,
        canonical_root: Path,
    ) -> None:
        resolved_root = canonical_root.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
        if not resolved_directory.is_relative_to(resolved_root):
            raise OSError("Backup path leaves configured root")
        relative = directory.absolute().relative_to(canonical_root.absolute())
        current = canonical_root
        for part in relative.parts:
            current /= part
            if _is_link_or_junction(current):
                raise OSError("Backup subtree contains a link or junction")
        if not directory.is_dir():
            raise OSError("Backup subtree component is not a directory")

    def _cleanup_partial_backup(
        self,
        operation_root: Path,
        canonical_root: Path,
    ) -> None:
        try:
            self._assert_contained_directory(operation_root, canonical_root)
        except OSError:
            return
        shutil.rmtree(operation_root, ignore_errors=True)

    def _parse_backup_manifest(
        self, data: object
    ) -> tuple[datetime, str, int]:
        if not isinstance(data, dict):
            raise ValueError("Manifest root must be an object")
        timestamp_value = data.get("timestamp")
        action_identifier = data.get("action_identifier")
        files = data.get("files")
        if not isinstance(timestamp_value, str) or not timestamp_value.strip():
            raise ValueError("Invalid manifest timestamp")
        if not isinstance(action_identifier, str):
            raise ValueError("Invalid manifest action")
        if not isinstance(files, list):
            raise ValueError("Invalid manifest files")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("Invalid manifest file entry")
            if not isinstance(item.get("source_name"), str) or not isinstance(
                item.get("backup_name"), str
            ):
                raise ValueError("Invalid manifest file names")
        timestamp = datetime.fromisoformat(timestamp_value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        return timestamp, action_identifier, len(files)

    def _safe_child(self, root: Path, name: str) -> Path:
        child = root / name
        resolved_root = root.resolve()
        resolved_child = child.resolve()
        if not resolved_child.is_relative_to(resolved_root):
            raise OSError("Path leaves configured root")
        return child

    def _product_backup_root(self, backup_root: Path, product_id: str) -> Path:
        product_key = uuid5(NAMESPACE_URL, product_id).hex
        return backup_root / PLUGIN_IDENTIFIER / product_key

    def _finish(
        self,
        product_id: str,
        action_identifier: str,
        status: OperationStatus,
        summary: str,
        changed_count: int,
        skipped_count: int,
        error_count: int,
        rollback_status: RollbackStatus | None,
        backup_created: bool,
        repository: OperationLogRepository,
        operation_id: str | None = None,
        timestamp: datetime | None = None,
        logger: Logger | None = None,
    ) -> LicenseOperationResult:
        result = LicenseOperationResult(
            status,
            summary,
            changed_count,
            skipped_count,
            error_count,
            rollback_status,
            backup_created,
        )
        operation_log = OperationLog(
            id=operation_id or str(uuid4()),
            timestamp=timestamp or datetime.now(UTC),
            product_id=product_id,
            plugin_identifier=PLUGIN_IDENTIFIER,
            action_identifier=action_identifier,
            status=status,
            summary=summary,
            changed_count=changed_count,
            skipped_count=skipped_count,
            error_count=error_count,
            rollback_status=rollback_status,
        )
        try:
            repository.append(operation_log)
        except Exception:
            (logger or _LOGGER).exception("Could not append License Manager log")
            warning = (
                "Операцію виконано, але запис журналу не вдалося зберегти."
                if changed_count > 0
                else "Результат операції не вдалося зберегти в журналі."
            )
            return replace(
                result,
                operation_log_saved=False,
                warnings=(warning,),
            )
        return result


def _probe(path: Path) -> _FileProbe:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _FileProbe(False, False)
    except OSError:
        return _FileProbe(None, None)
    return _FileProbe(
        True,
        S_ISREG(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _is_link_or_junction(path: Path) -> bool:
    """Detect symlinks and Windows reparse-point directories."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        metadata = path.lstat()
    except OSError:
        raise
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)
