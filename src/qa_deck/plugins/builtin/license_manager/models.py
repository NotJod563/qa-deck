"""Typed models used by License Manager."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath

from qa_deck.domain import OperationStatus, PluginConfiguration, RollbackStatus
from qa_deck.plugins import RiskLevel


def normalize_directory(value: str) -> str:
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {'"', "'"}
    ):
        return normalized[1:-1]
    return normalized


def normalize_file_names(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value[-1].isspace() and value.strip():
            raise ValueError("Ім’я файла не може завершуватися пробілом")
        name = value.strip()
        if not name:
            continue
        _validate_file_name(name)
        key = name.casefold()
        if key not in seen:
            normalized.append(name)
            seen.add(key)
    return tuple(normalized)


def _validate_file_name(name: str) -> None:
    invalid_characters = set('<>:"/\\|?*')
    base_name = name.split(".", 1)[0].casefold()
    reserved_names = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        PurePosixPath(name).is_absolute()
        or PureWindowsPath(name).is_absolute()
        or name in {".", ".."}
        or ".." in PurePosixPath(name).parts
        or ".." in PureWindowsPath(name).parts
        or any(character in invalid_characters for character in name)
        or any(ord(character) < 32 for character in name)
        or name.endswith((".", " "))
        or base_name in reserved_names
    ):
        raise ValueError(f"'{name}' має бути лише ім’ям файла")
    if name.casefold().endswith(".hidden"):
        raise ValueError("Не додавайте внутрішній suffix .hidden")


@dataclass(frozen=True, slots=True)
class LicenseManagerConfiguration:
    license_directory: str
    license_files: tuple[str, ...]

    @classmethod
    def create(
        cls,
        license_directory: str,
        license_files: list[str] | tuple[str, ...],
        *,
        enabled: bool,
    ) -> "LicenseManagerConfiguration":
        if not isinstance(license_directory, str):
            raise ValueError("license_directory повинен бути рядком")
        if not isinstance(license_files, (list, tuple)) or not all(
            isinstance(item, str) for item in license_files
        ):
            raise ValueError("license_files повинен бути списком рядків")
        if type(enabled) is not bool:
            raise ValueError("Поле enabled повинно мати тип bool")
        directory = normalize_directory(license_directory)
        files = normalize_file_names(license_files)
        if enabled and not directory:
            raise ValueError("Для активного License Manager вкажіть каталог")
        if enabled and not files:
            raise ValueError("Додайте принаймні один ліцензійний файл")
        return cls(directory, files)

    @classmethod
    def from_plugin_configuration(
        cls,
        configuration: PluginConfiguration,
    ) -> "LicenseManagerConfiguration":
        if not isinstance(configuration, PluginConfiguration):
            raise ValueError("Очікується PluginConfiguration")
        if configuration.plugin_identifier != "license-manager":
            raise ValueError("Конфігурація належить іншому плагіну")
        if (
            not isinstance(configuration.product_id, str)
            or not configuration.product_id.strip()
        ):
            raise ValueError("Некоректний product_id")
        if type(configuration.enabled) is not bool:
            raise ValueError("Поле enabled повинно мати тип bool")
        if not isinstance(configuration.settings, dict):
            raise ValueError("Поле settings повинно бути JSON object")
        directory = configuration.settings.get("license_directory", "")
        raw_files = configuration.settings.get("license_files", [])
        if not isinstance(directory, str):
            raise ValueError("license_directory повинен бути рядком")
        if not isinstance(raw_files, (list, tuple)) or not all(
            isinstance(item, str) for item in raw_files
        ):
            raise ValueError("license_files повинен бути списком рядків")
        return cls.create(
            directory,
            raw_files,
            enabled=configuration.enabled,
        )

    def to_settings(self) -> dict[str, object]:
        return {
            "license_directory": self.license_directory,
            "license_files": list(self.license_files),
        }


class LicenseInspectionStatus(StrEnum):
    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    DIRECTORY_MISSING = "directory_missing"
    NOT_A_DIRECTORY = "not_a_directory"
    ERROR = "error"


class LicenseFileStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    MISSING = "missing"
    CONFLICT = "conflict"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LicenseFileState:
    filename: str
    status: LicenseFileStatus
    message: str
    original_size: int | None = None
    original_mtime_ns: int | None = None
    hidden_size: int | None = None
    hidden_mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class LicenseInspectionResult:
    status: LicenseInspectionStatus
    directory: str | None
    files: tuple[LicenseFileState, ...]
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedChange:
    source_name: str
    target_name: str


@dataclass(frozen=True, slots=True)
class SkippedChange:
    filename: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChangePlan:
    product_id: str
    plugin_identifier: str
    action_identifier: str
    risk_level: RiskLevel
    requires_confirmation: bool
    changes: tuple[PlannedChange, ...]
    skipped: tuple[SkippedChange, ...]
    warnings: tuple[str, ...]
    fingerprint: str
    blocking_error: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


@dataclass(frozen=True, slots=True)
class LicenseOperationResult:
    status: OperationStatus
    summary: str
    changed_count: int
    skipped_count: int
    error_count: int
    rollback_status: RollbackStatus | None
    backup_created: bool
    stale_plan: bool = False
    operation_log_saved: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackupInspectionResult:
    has_backup: bool
    operation_count: int
    latest_timestamp: datetime | None
    latest_action: str | None
    file_count: int
    manifest_available: bool
    message: str | None = None
