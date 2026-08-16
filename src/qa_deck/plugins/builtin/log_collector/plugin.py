"""Inspection and bounded ZIP collection of configured product logs."""

import os
import stat
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path

from qa_deck.domain import (
    PluginConfiguration,
    PluginSetupSection,
    PortablePath,
    Product,
    ProductSetupProduct,
)
from qa_deck.domain.snapshot import SnapshotCaptureResult, SnapshotResource
from qa_deck.plugins.api import Plugin, PluginAction, RiskLevel
from qa_deck.plugins.builtin.log_collector.collection import (
    LogCollectionResult,
    LogCollectionService,
)
from qa_deck.plugins.builtin.log_collector.models import (
    LogCollectorConfiguration,
    LogInspectionResult,
    LogSourceInspection,
)
from qa_deck.product_setup import (
    SetupPluginImportPreparation,
    SetupPluginPreview,
    import_path_field,
    make_portable_path,
    preview_path,
    validate_import_path,
)
from qa_deck.storage import OperationLogRepository


class LogCollector:
    """Inspect configured product logs and collect them into a safe ZIP."""

    identifier = "log-collector"
    display_name = "Log Collector"
    description = (
        "Перевіряє джерела логів продукту та збирає їх у тимчасовий ZIP."
    )
    version = "0.1.0"

    def __init__(self, max_entries: int = 1_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries

    def get_actions(self) -> list[PluginAction]:
        return [
            PluginAction(
                identifier="inspect-log-sources",
                display_name="Перевірити джерела логів",
                description="Зібрати обмежену статистику без читання вмісту логів.",
                risk_level=RiskLevel.SAFE,
            ),
            PluginAction(
                identifier="collect-logs",
                display_name="Зібрати логи в ZIP",
                description=(
                    "Створити тимчасовий ZIP-архів із налаштованих "
                    "логів продукту."
                ),
                risk_level=RiskLevel.SAFE,
            ),
        ]

    def create_configuration(
        self,
        product_id: str,
        enabled: bool,
        log_directories: list[str],
    ) -> PluginConfiguration:
        typed = LogCollectorConfiguration.from_values(log_directories)
        if enabled and not typed.log_directories:
            raise ValueError("Додайте хоча б один каталог логів.")
        return PluginConfiguration(
            product_id=product_id,
            plugin_identifier=self.identifier,
            enabled=enabled,
            settings=typed.to_settings(),
        )

    def typed_configuration(
        self, configuration: PluginConfiguration
    ) -> LogCollectorConfiguration:
        if configuration.plugin_identifier != self.identifier:
            raise ValueError("Конфігурація належить іншому плагіну.")
        return LogCollectorConfiguration.from_plugin_configuration(configuration)

    def export_product_setup(
        self,
        product: ProductSetupProduct,
        configuration: PluginConfiguration,
    ) -> PluginSetupSection:
        typed = self.typed_configuration(configuration)
        directories = [
            path.to_dict()
            for value in typed.log_directories
            if (path := make_portable_path(value, product.install_directory_hint))
            is not None
        ]
        return PluginSetupSection(
            self.identifier,
            1,
            {"enabled": configuration.enabled, "log_directories": directories},
        )

    def preview_product_setup(
        self,
        product: ProductSetupProduct,
        section: PluginSetupSection,
    ) -> SetupPluginPreview:
        del product
        if section.schema_version != 1 or set(section.data) != {
            "enabled",
            "log_directories",
        }:
            raise ValueError("Unsupported Log Collector setup section")
        enabled = section.data["enabled"]
        raw_directories = section.data["log_directories"]
        if type(enabled) is not bool or not isinstance(raw_directories, list):
            raise ValueError("Invalid Log Collector setup data")
        directories = tuple(PortablePath.from_dict(item) for item in raw_directories)
        LogCollectorConfiguration.from_values([path.original for path in directories])
        if enabled and not directories:
            raise ValueError("Enabled Log Collector setup requires a directory")
        paths = tuple(
            preview
            for index, path in enumerate(directories, start=1)
            if (preview := preview_path(f"Каталог логів {index}", path)) is not None
        )
        return SetupPluginPreview(
            self.identifier,
            self.display_name,
            "supported",
            (f"Каталогів логів: {len(directories)}",),
            paths,
        )

    def prepare_product_setup_import(
        self,
        product: ProductSetupProduct,
        section: PluginSetupSection,
    ) -> SetupPluginImportPreparation:
        preview = self.preview_product_setup(product, section)
        raw_directories = section.data["log_directories"]
        if not isinstance(raw_directories, list):  # pragma: no cover
            raise ValueError("Invalid Log Collector setup data")
        directories = tuple(
            PortablePath.from_dict(item) for item in raw_directories
        )
        return SetupPluginImportPreparation(
            self.identifier,
            self.display_name,
            "supported",
            tuple(
                import_path_field(
                    f"log_directory_{index}",
                    f"Каталог логів {index + 1}",
                    path.original,
                )
                for index, path in enumerate(directories)
            ),
            preview.details,
        )

    def build_product_setup_configuration(
        self,
        product_id: str,
        product: ProductSetupProduct,
        section: PluginSetupSection,
        adapted_values: dict[str, str],
    ) -> PluginConfiguration:
        self.preview_product_setup(product, section)
        raw_directories = section.data["log_directories"]
        if not isinstance(raw_directories, list):  # pragma: no cover
            raise ValueError("Invalid Log Collector setup data")
        expected = {f"log_directory_{index}" for index in range(len(raw_directories))}
        if set(adapted_values) != expected:
            raise ValueError("Log Collector adaptation fields are invalid")
        enabled = section.data["enabled"]
        if type(enabled) is not bool:  # pragma: no cover
            raise ValueError("Invalid Log Collector setup data")
        return self.create_configuration(
            product_id,
            enabled,
            [
                validate_import_path(
                    adapted_values[f"log_directory_{index}"],
                    f"Каталог логів {index + 1}",
                )
                for index in range(len(raw_directories))
            ],
        )

    def inspect(self, configuration: PluginConfiguration) -> LogInspectionResult:
        typed = self.typed_configuration(configuration)
        if not configuration.enabled:
            return LogInspectionResult(
                enabled=False,
                sources=(),
                message="Log Collector вимкнений для цього продукту.",
            )
        return LogInspectionResult(
            enabled=True,
            sources=tuple(self._inspect_source(path) for path in typed.log_directories),
        )

    def collect(
        self,
        *,
        product: Product,
        configuration: PluginConfiguration | None,
        max_files: int,
        max_total_bytes: int,
        operation_logs: OperationLogRepository,
        logger: Logger,
    ) -> LogCollectionResult:
        """Build a temporary bounded ZIP for one product configuration."""
        service = LogCollectionService(
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            max_entries=self.max_entries,
            operation_logs=operation_logs,
            logger=logger,
        )
        return service.collect(product, configuration)

    def _inspect_source(self, configured_path: str) -> LogSourceInspection:
        root = Path(configured_path)
        try:
            root_stat = root.lstat()
            root_is_link = _is_link_like(root, root_stat)
        except FileNotFoundError:
            return LogSourceInspection(
                configured_path=configured_path,
                exists=False,
                is_directory=False,
                message="Каталог не знайдено.",
            )
        except OSError:
            return LogSourceInspection(
                configured_path=configured_path,
                exists=None,
                is_directory=None,
                message="Не вдалося перевірити каталог логів.",
            )

        if root_is_link:
            return LogSourceInspection(
                configured_path=configured_path,
                exists=True,
                is_directory=False,
                message=(
                    "Символічні посилання та junction не скануються як "
                    "кореневі каталоги логів."
                ),
            )

        if not stat.S_ISDIR(root_stat.st_mode):
            return LogSourceInspection(
                configured_path=configured_path,
                exists=True,
                is_directory=False,
                message="Вказаний шлях не є каталогом.",
            )

        file_count = 0
        total_size = 0
        latest_timestamp: float | None = None
        entries_seen = 0
        pending = [root]
        try:
            canonical_root = root.resolve(strict=True)
            while pending and entries_seen < self.max_entries:
                directory = pending.pop()
                directory_metadata = directory.lstat()
                if (
                    _is_link_like(directory, directory_metadata)
                    or not stat.S_ISDIR(directory_metadata.st_mode)
                    or not directory.resolve(strict=True).is_relative_to(
                        canonical_root
                    )
                ):
                    raise OSError("Unsafe log directory")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entries_seen >= self.max_entries:
                            break
                        entries_seen += 1
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            metadata = entry.stat(follow_symlinks=False)
                            file_count += 1
                            total_size += metadata.st_size
                            if (
                                latest_timestamp is None
                                or metadata.st_mtime > latest_timestamp
                            ):
                                latest_timestamp = metadata.st_mtime
        except OSError:
            return LogSourceInspection(
                configured_path=configured_path,
                exists=True,
                is_directory=True,
                file_count=file_count,
                total_size=total_size,
                latest_modified=self._as_datetime(latest_timestamp),
                truncated=bool(pending),
                message="Сканування каталогу не вдалося завершити.",
            )

        truncated = bool(pending) or entries_seen >= self.max_entries
        return LogSourceInspection(
            configured_path=configured_path,
            exists=True,
            is_directory=True,
            file_count=file_count,
            total_size=total_size,
            latest_modified=self._as_datetime(latest_timestamp),
            truncated=truncated,
            message=(
                "Досягнуто ліміт сканування; показано частковий результат."
                if truncated
                else None
            ),
        )

    @staticmethod
    def _as_datetime(timestamp: float | None) -> datetime | None:
        if timestamp is None:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None


    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        if configuration is None or not configuration.enabled:
            return SnapshotCaptureResult(
                warnings=(
                    "Log Collector snapshot provider is disabled or not configured.",
                ),
            )

        inspection = self.inspect(configuration)
        if inspection.enabled and inspection.sources:
            resources: list[SnapshotResource] = []
            for source in inspection.sources:
                resources.append(
                    SnapshotResource(
                        source=self.identifier,
                        resource_type="log-source",
                        identifier=source.configured_path,
                        state={
                            "exists": source.exists,
                            "is_directory": source.is_directory,
                            "file_count": source.file_count,
                            "total_size": source.total_size,
                            "latest_modified": source.latest_modified.isoformat()
                            if source.latest_modified is not None
                            else None,
                            "truncated": source.truncated,
                            "message": source.message,
                        },
                    )
                )
            return SnapshotCaptureResult(resources=tuple(resources))

        warnings: tuple[str, ...] = ()
        if not inspection.enabled:
            warnings = ("Log Collector is disabled for this product.",)
        elif inspection.enabled:
            warnings = ("Log Collector inspection returned no sources.",)

        return SnapshotCaptureResult(
            resources=(
                SnapshotResource(
                    source=self.identifier,
                    resource_type="log-collector",
                    identifier="log-collector",
                    state={
                        "enabled": inspection.enabled,
                        "message": inspection.message,
                    },
                ),
            ),
            warnings=warnings,
        )


def create_log_collector() -> Plugin:
    """Create the built-in Log Collector plugin."""
    return LogCollector()


def _is_link_like(path: Path, metadata: os.stat_result) -> bool:
    """Detect symlinks and Windows reparse-point directories."""
    if stat.S_ISLNK(metadata.st_mode) or path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)
