"""Typed configuration and results for Log Collector."""

from dataclasses import dataclass
from datetime import datetime
from typing import Self

from qa_deck.domain import PluginConfiguration


def normalize_directory_path(value: str) -> str:
    """Trim a path and one matching pair of surrounding quotes."""
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {'"', "'"}
    ):
        normalized = normalized[1:-1].strip()
    return normalized


@dataclass(frozen=True, slots=True)
class LogCollectorConfiguration:
    """Validated Log Collector settings for one product."""

    log_directories: tuple[str, ...]

    @classmethod
    def from_values(
        cls, log_directories: list[str] | tuple[str, ...]
    ) -> Self:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in log_directories:
            path = normalize_directory_path(value)
            if path and path not in seen:
                normalized.append(path)
                seen.add(path)
        return cls(tuple(normalized))

    @classmethod
    def from_plugin_configuration(
        cls, configuration: PluginConfiguration
    ) -> Self:
        if not isinstance(configuration, PluginConfiguration):
            raise ValueError("Очікується PluginConfiguration.")
        if configuration.plugin_identifier != "log-collector":
            raise ValueError("Конфігурація належить іншому плагіну.")
        if (
            not isinstance(configuration.product_id, str)
            or not configuration.product_id.strip()
        ):
            raise ValueError("Некоректний product_id.")
        if type(configuration.enabled) is not bool:
            raise ValueError("Поле enabled повинно мати тип bool.")
        if not isinstance(configuration.settings, dict):
            raise ValueError("Поле settings повинно бути JSON object.")
        raw_paths = configuration.settings.get("log_directories", [])
        if not isinstance(raw_paths, list) or not all(
            isinstance(path, str) for path in raw_paths
        ):
            raise ValueError("Некоректний список каталогів логів.")
        result = cls.from_values(raw_paths)
        if configuration.enabled and not result.log_directories:
            raise ValueError("Додайте хоча б один каталог логів.")
        return result

    def to_settings(self) -> dict[str, object]:
        return {"log_directories": list(self.log_directories)}


@dataclass(frozen=True, slots=True)
class LogSourceInspection:
    """Read-only summary for one configured log directory."""

    configured_path: str
    exists: bool | None
    is_directory: bool | None
    file_count: int = 0
    total_size: int = 0
    latest_modified: datetime | None = None
    truncated: bool = False
    message: str | None = None


@dataclass(frozen=True, slots=True)
class LogInspectionResult:
    """Inspection result for all log sources of one product."""

    enabled: bool
    sources: tuple[LogSourceInspection, ...]
    message: str | None = None
