"""Plugin-neutral Environment Profile comparison and execution orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from uuid import uuid4

from qa_deck.domain import EnvironmentProfile, OperationLog, OperationStatus, Product
from qa_deck.plugins.api import (
    EnvironmentProfileComparisonEntry,
    EnvironmentProfileComparisonSection,
    EnvironmentProfileComparisonStatus,
    EnvironmentProfileExecutionEntry,
    EnvironmentProfileExecutionPreparation,
    EnvironmentProfileExecutionStatus,
    EnvironmentProfileProviderExecution,
)
from qa_deck.plugins.manager import PluginManager
from qa_deck.storage import (
    EnvironmentProfileRepository,
    OperationLogRepository,
    PluginConfigurationRepository,
)


@dataclass(frozen=True, slots=True)
class EnvironmentProfileComparison:
    profile: EnvironmentProfile
    sections: tuple[EnvironmentProfileComparisonSection, ...]

    def count(self, status: EnvironmentProfileComparisonStatus) -> int:
        return sum(
            entry.status is status
            for section in self.sections
            for entry in section.entries
        )

    @property
    def change_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.CHANGE)

    @property
    def no_change_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.NO_CHANGE)

    @property
    def blocked_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.BLOCKED)

    @property
    def error_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.ERROR)


class EnvironmentProfileComparator:
    """Inspect each capable provider once, then compare all Profiles."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        configurations: PluginConfigurationRepository,
        logger: Logger | None = None,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._configurations = configurations
        self._logger = logger

    def compare_all(
        self,
        product: Product,
        profiles: list[EnvironmentProfile],
        current_by_provider: dict[str, object] | None = None,
    ) -> tuple[EnvironmentProfileComparison, ...]:
        if any(profile.product_id != product.id for profile in profiles):
            raise ValueError("Environment Profile belongs to another Product")
        comparisons: dict[str, list[EnvironmentProfileComparisonSection]] = {
            profile.id: [] for profile in profiles
        }
        for plugin in self._plugin_manager.list_all():
            identifier, display_name = _plugin_identity(plugin)
            try:
                uses_profile = getattr(plugin, "uses_environment_profile", None)
                inspect_current = getattr(
                    plugin, "inspect_environment_profile_current", None
                )
                compare_profile = getattr(
                    plugin, "compare_environment_profile", None
                )
            except Exception:
                self._log_failure(
                    "Environment Profile capability discovery failed", identifier
                )
                for profile in profiles:
                    comparisons[profile.id].append(
                        self._error_section(identifier, display_name)
                    )
                continue
            if (
                not callable(uses_profile)
                or not callable(inspect_current)
                or not callable(compare_profile)
            ):
                continue
            relevant: list[EnvironmentProfile] = []
            for profile in profiles:
                try:
                    participates = uses_profile(profile)
                    if type(participates) is not bool:
                        raise TypeError(
                            "Profile provider participation must return bool"
                        )
                except Exception:
                    self._log_failure(
                        "Environment Profile participation failed", identifier
                    )
                    comparisons[profile.id].append(
                        self._error_section(identifier, display_name)
                    )
                    continue
                if participates:
                    relevant.append(profile)
            if not relevant:
                continue
            try:
                configuration = self._configurations.get(product.id, identifier)
                current = (
                    current_by_provider[identifier]
                    if current_by_provider is not None
                    and identifier in current_by_provider
                    else inspect_current(product, configuration)
                )
            except Exception:
                self._log_failure(
                    "Environment Profile inspection failed", identifier
                )
                for profile in relevant:
                    comparisons[profile.id].append(
                        self._error_section(identifier, display_name)
                    )
                continue
            for profile in relevant:
                try:
                    section = compare_profile(profile, configuration, current)
                    self._validate_section(section)
                except Exception:
                    self._log_failure(
                        "Environment Profile comparison failed", identifier
                    )
                    section = self._error_section(identifier, display_name)
                if section is not None:
                    comparisons[profile.id].append(section)
        return tuple(
            EnvironmentProfileComparison(
                profile,
                tuple(comparisons[profile.id]),
            )
            for profile in profiles
        )

    def _log_failure(self, message: str, identifier: str) -> None:
        if self._logger is not None:
            self._logger.exception("%s for %s", message, identifier)

    @staticmethod
    def _validate_section(section: object) -> None:
        if section is None:
            return
        if not isinstance(section, EnvironmentProfileComparisonSection) or not all(
            isinstance(entry, EnvironmentProfileComparisonEntry)
            for entry in section.entries
        ):
            raise TypeError("Profile provider returned an invalid comparison")

    @staticmethod
    def _error_section(
        identifier: str, display_name: str
    ) -> EnvironmentProfileComparisonSection:
        return EnvironmentProfileComparisonSection(
            identifier,
            display_name,
            None,
            (
                EnvironmentProfileComparisonEntry(
                    "provider",
                    display_name,
                    "Недоступно",
                    "Налаштовано у profile",
                    EnvironmentProfileComparisonStatus.ERROR,
                    "Не вдалося перевірити поточний стан.",
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class EnvironmentProfileExecutionPlan:
    product_id: str
    profile_id: str
    profile_name: str
    created_at: datetime
    sections: tuple[EnvironmentProfileExecutionPreparation, ...] = ()

    def count(self, status: EnvironmentProfileComparisonStatus) -> int:
        return sum(
            entry.status is status
            for section in self.sections
            for entry in section.entries
        )

    @property
    def ready_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.CHANGE)

    @property
    def no_change_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.NO_CHANGE)

    @property
    def blocked_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.BLOCKED)

    @property
    def error_count(self) -> int:
        return self.count(EnvironmentProfileComparisonStatus.ERROR)


@dataclass(frozen=True, slots=True)
class EnvironmentProfileExecutionIntent:
    token: str
    product_id: str
    profile_id: str
    profile_fingerprint: str
    sections: tuple[EnvironmentProfileExecutionPreparation, ...]


@dataclass(frozen=True, slots=True)
class EnvironmentProfileExecutionResult:
    product_id: str
    profile_id: str
    profile_name: str
    created_at: datetime
    sections: tuple[
        tuple[str, str, tuple[EnvironmentProfileExecutionEntry, ...]], ...
    ] = field(default_factory=tuple)
    warnings: tuple[str, ...] = ()
    operation_log_saved: bool = True

    def count(self, status: EnvironmentProfileExecutionStatus) -> int:
        return sum(
            entry.status is status
            for _, _, entries in self.sections
            for entry in entries
        )

    @property
    def succeeded_count(self) -> int:
        return self.count(EnvironmentProfileExecutionStatus.SUCCESS)

    @property
    def failed_count(self) -> int:
        return self.count(EnvironmentProfileExecutionStatus.FAILED)

    @property
    def blocked_count(self) -> int:
        return self.count(EnvironmentProfileExecutionStatus.BLOCKED) + self.count(
            EnvironmentProfileExecutionStatus.STALE
        )

    @property
    def unchanged_count(self) -> int:
        return self.count(EnvironmentProfileExecutionStatus.NO_CHANGE)


class EnvironmentProfileExecutionStateStore:
    """Keep opaque one-time Profile intents and PRG results in process memory."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._intents: dict[str, EnvironmentProfileExecutionIntent] = {}
        self._results: dict[str, EnvironmentProfileExecutionResult] = {}
        self._lock = Lock()

    def create_intent(
        self,
        profile: EnvironmentProfile,
        plan: EnvironmentProfileExecutionPlan,
    ) -> EnvironmentProfileExecutionIntent:
        intent = EnvironmentProfileExecutionIntent(
            token_urlsafe(24),
            profile.product_id,
            profile.id,
            _profile_fingerprint(profile),
            plan.sections,
        )
        with self._lock:
            self._trim(self._intents)
            self._intents[intent.token] = intent
        return intent

    def take_intent(
        self, token: str, product_id: str, profile_id: str
    ) -> EnvironmentProfileExecutionIntent | None:
        with self._lock:
            intent = self._intents.pop(token, None)
        if (
            intent is None
            or intent.product_id != product_id
            or intent.profile_id != profile_id
        ):
            return None
        return intent

    def save_result(self, result: EnvironmentProfileExecutionResult) -> str:
        result_id = token_urlsafe(18)
        with self._lock:
            self._trim(self._results)
            self._results[result_id] = result
        return result_id

    def get_result(
        self, result_id: str, product_id: str, profile_id: str
    ) -> EnvironmentProfileExecutionResult | None:
        with self._lock:
            result = self._results.get(result_id)
        if (
            result is None
            or result.product_id != product_id
            or result.profile_id != profile_id
        ):
            return None
        return result

    def _trim(self, values: dict[str, object]) -> None:
        while len(values) >= self._limit:
            values.pop(next(iter(values)))


class EnvironmentProfileExecutionPlanner:
    """Ask optional providers for fresh, read-only Profile execution plans."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        configurations: PluginConfigurationRepository,
        logger: Logger | None = None,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._configurations = configurations
        self._logger = logger

    def prepare(
        self, product: Product, profile: EnvironmentProfile
    ) -> EnvironmentProfileExecutionPlan:
        if profile.product_id != product.id:
            raise ValueError("Environment Profile belongs to another Product")
        sections: list[EnvironmentProfileExecutionPreparation] = []
        for plugin in self._plugin_manager.list_all():
            identifier, _ = _plugin_identity(plugin)
            try:
                uses_profile = getattr(plugin, "uses_environment_profile", None)
                inspect_current = getattr(
                    plugin, "inspect_environment_profile_current", None
                )
                prepare = getattr(
                    plugin, "prepare_environment_profile_execution", None
                )
            except Exception:
                self._log_failure("capability discovery", identifier)
                sections.append(self._provider_error(plugin))
                continue
            if not callable(uses_profile):
                continue
            try:
                participates = uses_profile(profile)
                if type(participates) is not bool:
                    raise TypeError(
                        "Profile provider participation must return bool"
                    )
            except Exception:
                self._log_failure("participation", identifier)
                sections.append(self._provider_error(plugin))
                continue
            if not participates:
                continue
            if not callable(inspect_current) or not callable(prepare):
                sections.append(self._provider_error(plugin))
                continue
            try:
                configuration = self._configurations.get(product.id, identifier)
                current = inspect_current(product, configuration)
                section = prepare(profile, product, configuration, current)
                self._validate_preparation(section, identifier)
            except Exception:
                self._log_failure("planning", identifier)
                section = self._provider_error(plugin)
            sections.append(section)
        return EnvironmentProfileExecutionPlan(
            product.id, profile.id, profile.name, datetime.now(UTC), tuple(sections)
        )

    def _log_failure(self, stage: str, identifier: str) -> None:
        if self._logger is not None:
            self._logger.exception(
                "Environment Profile %s failed for %s", stage, identifier
            )

    @staticmethod
    def _validate_preparation(section: object, identifier: str) -> None:
        if (
            not isinstance(section, EnvironmentProfileExecutionPreparation)
            or section.provider_identifier != identifier
            or not section.authority_fingerprint
            or not all(
                isinstance(entry, EnvironmentProfileComparisonEntry)
                for entry in section.entries
            )
        ):
            raise TypeError("Profile provider returned an invalid execution plan")

    @staticmethod
    def _provider_error(plugin: object) -> EnvironmentProfileExecutionPreparation:
        identifier, display_name = _plugin_identity(plugin)
        return EnvironmentProfileExecutionPreparation(
            identifier,
            display_name,
            None,
            (
                EnvironmentProfileComparisonEntry(
                    "provider",
                    display_name,
                    "Недоступно",
                    "Стан із profile",
                    EnvironmentProfileComparisonStatus.ERROR,
                    "Не вдалося підготувати застосування цього provider.",
                ),
            ),
            "error",
        )


class EnvironmentProfileExecutor:
    """Best-effort coordination of independently validated provider execution."""

    def __init__(
        self,
        planner: EnvironmentProfileExecutionPlanner,
        plugin_manager: PluginManager,
        configurations: PluginConfigurationRepository,
        profiles: EnvironmentProfileRepository,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> None:
        self._planner = planner
        self._plugin_manager = plugin_manager
        self._configurations = configurations
        self._profiles = profiles
        self._backup_root = backup_root
        self._operation_logs = operation_logs
        self._logger = logger

    def execute(
        self, product: Product, intent: EnvironmentProfileExecutionIntent
    ) -> EnvironmentProfileExecutionResult:
        profile = self._profiles.get(product.id, intent.profile_id)
        if (
            profile is None
            or _profile_fingerprint(profile) != intent.profile_fingerprint
        ):
            return self._log_result(self._stale_result(product, intent, profile))

        current_plan = self._planner.prepare(product, profile)
        current_sections = {
            section.provider_identifier: section for section in current_plan.sections
        }
        sections: list[
            tuple[str, str, tuple[EnvironmentProfileExecutionEntry, ...]]
        ] = []
        for expected in intent.sections:
            current = current_sections.get(expected.provider_identifier)
            plugin = self._plugin_manager.get(expected.provider_identifier)
            try:
                execute = getattr(plugin, "execute_environment_profile", None)
            except Exception:
                execute = None
            if (
                current is None
                or current.authority_fingerprint
                != expected.authority_fingerprint
                or any(
                    entry.status is EnvironmentProfileComparisonStatus.ERROR
                    for entry in (*expected.entries, *current.entries)
                )
                or not callable(execute)
            ):
                sections.append(self._stale_section(expected))
                continue
            try:
                configuration = self._configurations.get(
                    product.id, expected.provider_identifier
                )
                execution = execute(
                    profile,
                    product,
                    configuration,
                    expected,
                    backup_root=self._backup_root,
                    operation_logs=self._operation_logs,
                    logger=self._logger,
                )
                self._validate_execution(execution)
                sections.append(
                    (
                        expected.provider_identifier,
                        expected.display_name,
                        execution.entries,
                    )
                )
            except Exception:
                if self._logger is not None:
                    self._logger.exception(
                        "Environment Profile execution failed for %s",
                        expected.provider_identifier,
                    )
                sections.append(self._failed_section(expected))
        result = EnvironmentProfileExecutionResult(
            product.id, profile.id, profile.name, datetime.now(UTC), tuple(sections)
        )
        return self._log_result(result)

    @staticmethod
    def _validate_execution(execution: object) -> None:
        if not isinstance(execution, EnvironmentProfileProviderExecution) or not all(
            isinstance(entry, EnvironmentProfileExecutionEntry)
            for entry in execution.entries
        ):
            raise TypeError("Profile provider returned an invalid execution result")

    @staticmethod
    def _entries_with_status(
        expected: EnvironmentProfileExecutionPreparation,
        status: EnvironmentProfileExecutionStatus,
        message: str,
    ) -> tuple[EnvironmentProfileExecutionEntry, ...]:
        return tuple(
            EnvironmentProfileExecutionEntry(
                entry.resource_id,
                entry.display_name,
                entry.current_state,
                entry.desired_state,
                status,
                message,
            )
            for entry in expected.entries
        )

    @classmethod
    def _stale_section(
        cls, expected: EnvironmentProfileExecutionPreparation
    ) -> tuple[str, str, tuple[EnvironmentProfileExecutionEntry, ...]]:
        return (
            expected.provider_identifier,
            expected.display_name,
            cls._entries_with_status(
                expected,
                EnvironmentProfileExecutionStatus.STALE,
                "Підготовлений план застарів; зміни не виконано.",
            ),
        )

    @classmethod
    def _failed_section(
        cls, expected: EnvironmentProfileExecutionPreparation
    ) -> tuple[str, str, tuple[EnvironmentProfileExecutionEntry, ...]]:
        return (
            expected.provider_identifier,
            expected.display_name,
            cls._entries_with_status(
                expected,
                EnvironmentProfileExecutionStatus.FAILED,
                "Provider не зміг виконати підготовлену операцію.",
            ),
        )

    def _stale_result(
        self,
        product: Product,
        intent: EnvironmentProfileExecutionIntent,
        profile: EnvironmentProfile | None,
    ) -> EnvironmentProfileExecutionResult:
        return EnvironmentProfileExecutionResult(
            product.id,
            intent.profile_id,
            profile.name if profile is not None else "Видалений profile",
            datetime.now(UTC),
            tuple(self._stale_section(section) for section in intent.sections),
            ("Profile змінився або був видалений. Жодних змін не виконано.",),
        )

    def _log_result(
        self, result: EnvironmentProfileExecutionResult
    ) -> EnvironmentProfileExecutionResult:
        try:
            self._operation_logs.append(
                OperationLog(
                    id=str(uuid4()),
                    timestamp=result.created_at,
                    product_id=result.product_id,
                    plugin_identifier="environment-profile",
                    action_identifier="apply-environment-profile",
                    status=self._operation_status(result),
                    summary=(
                        "Environment Profile applied: "
                        f"profile_id={result.profile_id}; "
                        f"success={result.succeeded_count}; "
                        f"failed={result.failed_count}; "
                        f"blocked={result.blocked_count}; "
                        f"unchanged={result.unchanged_count}."
                    ),
                    changed_count=result.succeeded_count,
                    skipped_count=result.unchanged_count,
                    error_count=result.failed_count + result.blocked_count,
                )
            )
        except Exception:
            if self._logger is not None:
                self._logger.exception("Could not append Environment Profile log")
            return replace(
                result,
                operation_log_saved=False,
                warnings=(
                    *result.warnings,
                    "Profile застосовано, але aggregate Operation Log не збережено.",
                ),
            )
        return result

    @staticmethod
    def _operation_status(result: EnvironmentProfileExecutionResult) -> OperationStatus:
        if result.succeeded_count and (result.failed_count or result.blocked_count):
            return OperationStatus.PARTIAL
        if result.succeeded_count:
            return OperationStatus.SUCCESS
        if result.failed_count:
            return OperationStatus.FAILED
        if result.blocked_count:
            return OperationStatus.REJECTED
        return OperationStatus.NO_CHANGES


def _profile_fingerprint(profile: EnvironmentProfile) -> str:
    payload = json.dumps(
        profile.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plugin_identity(plugin: object) -> tuple[str, str]:
    try:
        identifier = str(getattr(plugin, "identifier", "provider"))
    except Exception:
        identifier = "provider"
    try:
        display_name = str(getattr(plugin, "display_name", identifier))
    except Exception:
        display_name = identifier
    return identifier, display_name
