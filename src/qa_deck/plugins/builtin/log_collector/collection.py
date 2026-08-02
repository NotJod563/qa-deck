"""Bounded and path-safe ZIP collection for configured product logs."""

import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from logging import Logger, getLogger
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from qa_deck.domain import (
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    Product,
)
from qa_deck.plugins.builtin.log_collector.models import (
    LogCollectorConfiguration,
)
from qa_deck.storage import OperationLogRepository

PLUGIN_IDENTIFIER = "log-collector"
COLLECT_ACTION = "collect-logs"
_LOGGER = getLogger(__name__)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class SkippedLogFile:
    """A source item omitted from an archive with a safe explanation."""

    source_prefix: str
    relative_path: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class LogCollectionResult:
    """Result of one bounded log collection operation."""

    status: OperationStatus
    message: str
    archive_path: Path | None = None
    temporary_directory: Path | None = None
    download_name: str | None = None
    file_count: int = 0
    total_size: int = 0
    skipped: tuple[SkippedLogFile, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    operation_log_saved: bool = True

    @property
    def has_archive(self) -> bool:
        return self.archive_path is not None


@dataclass(slots=True)
class _CollectionState:
    file_count: int = 0
    total_size: int = 0
    entries_seen: int = 0
    truncated: bool = False
    stop: bool = False


class LogCollectionService:
    """Create a temporary ZIP without modifying configured source files."""

    def __init__(
        self,
        *,
        max_files: int,
        max_total_bytes: int,
        max_entries: int,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> None:
        if min(max_files, max_total_bytes, max_entries) < 1:
            raise ValueError("Log collection limits must be positive")
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self.operation_logs = operation_logs
        self.logger = logger or _LOGGER

    def collect(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> LogCollectionResult:
        """Collect configured logs and return a downloadable temporary ZIP."""
        validation_error = self._validate(product, configuration)
        if validation_error is not None:
            return self._finish(
                product.id,
                LogCollectionResult(OperationStatus.FAILED, validation_error),
            )
        assert configuration is not None
        typed = LogCollectorConfiguration.from_plugin_configuration(configuration)

        temporary_directory = Path(tempfile.mkdtemp(prefix="qa-deck-logs-"))
        timestamp = datetime.now(UTC)
        download_name = _download_name(product.name, timestamp)
        archive_path = temporary_directory / download_name
        skipped: list[SkippedLogFile] = []
        state = _CollectionState()
        try:
            with ZipFile(
                archive_path,
                mode="x",
                compression=ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                for source_number, configured_path in enumerate(
                    typed.log_directories, start=1
                ):
                    if state.stop:
                        break
                    self._collect_source(
                        archive,
                        source_number,
                        configured_path,
                        state,
                        skipped,
                    )
                if state.file_count:
                    archive.writestr(
                        "manifest.json",
                        json.dumps(
                            self._manifest(
                                timestamp,
                                product,
                                typed,
                                state,
                                skipped,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
        except (OSError, ValueError):
            self.logger.exception("Could not create Log Collector archive")
            shutil.rmtree(temporary_directory, ignore_errors=True)
            return self._finish(
                product.id,
                LogCollectionResult(
                    OperationStatus.FAILED,
                    "Не вдалося безпечно створити ZIP-архів логів.",
                    skipped=tuple(skipped),
                    truncated=state.truncated,
                ),
            )

        if not state.file_count:
            shutil.rmtree(temporary_directory, ignore_errors=True)
            return self._finish(
                product.id,
                LogCollectionResult(
                    OperationStatus.FAILED,
                    "Не знайдено жодного безпечного файла для архіву.",
                    skipped=tuple(skipped),
                    truncated=state.truncated,
                ),
            )

        partial = bool(skipped) or state.truncated
        result = LogCollectionResult(
            OperationStatus.PARTIAL if partial else OperationStatus.SUCCESS,
            (
                "ZIP-архів зібрано частково. Перелік пропусків є в manifest.json."
                if partial
                else "ZIP-архів логів готовий до завантаження."
            ),
            archive_path=archive_path,
            temporary_directory=temporary_directory,
            download_name=download_name,
            file_count=state.file_count,
            total_size=state.total_size,
            skipped=tuple(skipped),
            truncated=state.truncated,
            warnings=(
                ("Збір обмежено налаштованими лімітами.",)
                if state.truncated
                else ()
            ),
        )
        return self._finish(product.id, result)

    def _validate(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> str | None:
        if configuration is None:
            return "Спочатку налаштуйте Log Collector для цього продукту."
        try:
            typed = LogCollectorConfiguration.from_plugin_configuration(
                configuration
            )
        except ValueError:
            return "Збережена конфігурація Log Collector некоректна."
        if configuration.product_id != product.id:
            return "Конфігурація не належить вибраному продукту."
        if not configuration.enabled:
            return "Log Collector вимкнений для цього продукту."
        if not typed.log_directories:
            return "Не налаштовано жодного каталогу логів продукту."
        return None

    def _collect_source(
        self,
        archive: ZipFile,
        source_number: int,
        configured_path: str,
        state: _CollectionState,
        skipped: list[SkippedLogFile],
    ) -> None:
        prefix = f"source-{source_number:02d}"
        root = Path(configured_path)
        try:
            root_metadata = root.lstat()
            if _is_link_like(root, root_metadata):
                raise _UnsafePathError
            if not stat.S_ISDIR(root_metadata.st_mode):
                skipped.append(
                    SkippedLogFile(prefix, None, "Налаштований шлях не є каталогом.")
                )
                return
            canonical_root = root.resolve(strict=True)
        except FileNotFoundError:
            skipped.append(SkippedLogFile(prefix, None, "Каталог не знайдено."))
            return
        except _UnsafePathError:
            skipped.append(
                SkippedLogFile(
                    prefix,
                    None,
                    "Symlink або junction не сканується як джерело логів.",
                )
            )
            return
        except OSError:
            skipped.append(
                SkippedLogFile(prefix, None, "Каталог недоступний для читання.")
            )
            return

        pending: list[tuple[Path, Path]] = [(root, Path())]
        while pending and not state.stop:
            directory, relative_directory = pending.pop()
            try:
                metadata = directory.lstat()
                if (
                    _is_link_like(directory, metadata)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or not directory.resolve(strict=True).is_relative_to(
                        canonical_root
                    )
                ):
                    raise _UnsafePathError
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if state.entries_seen >= self.max_entries:
                            state.truncated = True
                            state.stop = True
                            break
                        state.entries_seen += 1
                        self._consider_entry(
                            archive,
                            root,
                            canonical_root,
                            prefix,
                            Path(entry.path),
                            relative_directory / entry.name,
                            pending,
                            state,
                            skipped,
                        )
                        if state.stop:
                            break
            except (_UnsafePathError, OSError):
                skipped.append(
                    SkippedLogFile(
                        prefix,
                        _display_relative(relative_directory),
                        "Каталог пропущено через небезпечний або недоступний шлях.",
                    )
                )

    def _consider_entry(
        self,
        archive: ZipFile,
        root: Path,
        canonical_root: Path,
        prefix: str,
        path: Path,
        relative_path: Path,
        pending: list[tuple[Path, Path]],
        state: _CollectionState,
        skipped: list[SkippedLogFile],
    ) -> None:
        try:
            metadata = path.lstat()
            if _is_link_like(path, metadata):
                skipped.append(
                    SkippedLogFile(
                        prefix,
                        relative_path.as_posix(),
                        "Symlink або junction пропущено.",
                    )
                )
                return
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((path, relative_path))
                return
            if not stat.S_ISREG(metadata.st_mode):
                skipped.append(
                    SkippedLogFile(
                        prefix,
                        relative_path.as_posix(),
                        "Об’єкт не є звичайним файлом.",
                    )
                )
                return
            if state.file_count >= self.max_files:
                state.truncated = True
                state.stop = True
                skipped.append(
                    SkippedLogFile(
                        prefix,
                        relative_path.as_posix(),
                        "Досягнуто ліміт кількості файлів.",
                    )
                )
                return
            if state.total_size + metadata.st_size > self.max_total_bytes:
                state.truncated = True
                state.stop = True
                skipped.append(
                    SkippedLogFile(
                        prefix,
                        relative_path.as_posix(),
                        "Досягнуто ліміт загального розміру.",
                    )
                )
                return
            archive_name = _safe_archive_name(prefix, relative_path)
            with self._stable_copy(root, canonical_root, path, metadata) as staged:
                with archive.open(archive_name, "w") as destination:
                    shutil.copyfileobj(staged, destination)
        except (FileNotFoundError, PermissionError):
            skipped.append(
                SkippedLogFile(
                    prefix,
                    relative_path.as_posix(),
                    "Файл зник або недоступний під час збору.",
                )
            )
            return
        except (_UnsafePathError, OSError):
            skipped.append(
                SkippedLogFile(
                    prefix,
                    relative_path.as_posix(),
                    "Файл змінився або не пройшов перевірку безпеки.",
                )
            )
            return
        state.file_count += 1
        state.total_size += metadata.st_size

    def _stable_copy(
        self,
        root: Path,
        canonical_root: Path,
        path: Path,
        scanned: os.stat_result,
    ) -> BinaryIO:
        """Copy one unchanged regular file to staging before ZIP writing."""
        if not _safe_parent_chain(root, canonical_root, path.parent):
            raise _UnsafePathError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        source: BinaryIO | None = None
        try:
            source = os.fdopen(descriptor, "rb")
            opened = os.fstat(source.fileno())
            current = path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or _signature(scanned) != _signature(current)
                or _signature(current) != _signature(opened)
                or not _safe_parent_chain(root, canonical_root, path.parent)
            ):
                raise _UnsafePathError
            staged = tempfile.TemporaryFile(mode="w+b")
            try:
                shutil.copyfileobj(source, staged)
                after = os.fstat(source.fileno())
                if _signature(opened) != _signature(after):
                    raise _UnsafePathError
                staged.seek(0)
                return staged
            except Exception:
                staged.close()
                raise
        finally:
            if source is not None:
                source.close()
            else:
                os.close(descriptor)

    def _manifest(
        self,
        timestamp: datetime,
        product: Product,
        configuration: LogCollectorConfiguration,
        state: _CollectionState,
        skipped: list[SkippedLogFile],
    ) -> dict[str, object]:
        return {
            "timestamp": timestamp.isoformat(),
            "product_id": product.id,
            "product_name": product.name,
            "plugin_identifier": PLUGIN_IDENTIFIER,
            "configured_source_directories": list(
                configuration.log_directories
            ),
            "added_file_count": state.file_count,
            "total_size_bytes": state.total_size,
            "skipped_files": [
                {
                    "source": item.source_prefix,
                    "relative_path": item.relative_path,
                    "reason": item.reason,
                }
                for item in skipped
            ],
            "truncated": state.truncated,
        }

    def _finish(
        self,
        product_id: str,
        result: LogCollectionResult,
    ) -> LogCollectionResult:
        operation_log = OperationLog(
            id=str(uuid4()),
            timestamp=datetime.now(UTC),
            product_id=product_id,
            plugin_identifier=PLUGIN_IDENTIFIER,
            action_identifier=COLLECT_ACTION,
            status=result.status,
            summary=(
                f"{result.message} Файлів: {result.file_count}; "
                f"розмір: {result.total_size} байт; "
                f"пропущено: {len(result.skipped)}; "
                f"обмежено: {'так' if result.truncated else 'ні'}."
            ),
            changed_count=result.file_count,
            skipped_count=len(result.skipped),
            error_count=(
                1 if result.status is OperationStatus.FAILED else 0
            ),
        )
        try:
            self.operation_logs.append(operation_log)
        except Exception:
            self.logger.exception("Could not append Log Collector operation log")
            return replace(
                result,
                operation_log_saved=False,
                warnings=(
                    *result.warnings,
                    "Архів підготовлено, але запис історії операцій "
                    "не вдалося зберегти."
                    if result.has_archive
                    else "Результат збору не вдалося зберегти в історії операцій.",
                ),
            )
        return result


class _UnsafePathError(OSError):
    pass


def _is_link_like(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode) or path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _safe_parent_chain(root: Path, canonical_root: Path, parent: Path) -> bool:
    try:
        relative = parent.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            metadata = current.lstat()
            if _is_link_like(current, metadata) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                return False
        return parent.resolve(strict=True).is_relative_to(canonical_root)
    except (OSError, ValueError):
        return False


def _signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _safe_archive_name(prefix: str, relative_path: Path) -> str:
    candidate = PurePosixPath(prefix, *relative_path.parts)
    if (
        not relative_path.parts
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any("\\" in part or ":" in part for part in candidate.parts)
    ):
        raise _UnsafePathError
    return candidate.as_posix()


def _download_name(product_name: str, timestamp: datetime) -> str:
    normalized = unicodedata.normalize("NFKD", product_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", ascii_name).strip(" .-_")
    safe_name = safe_name[:60].rstrip(" .-_") or "product"
    if safe_name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        safe_name = f"product-{safe_name}"
    stamp = timestamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"QA-Deck-Logs-{safe_name}-{stamp}.zip"


def _display_relative(path: Path) -> str | None:
    return path.as_posix() if path.parts else None
