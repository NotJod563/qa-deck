"""Public contracts for QA Deck plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from logging import Logger
from pathlib import Path
from typing import Protocol

from qa_deck.domain import (
    EnvironmentProfile,
    PluginConfiguration,
    Product,
    RollbackStatus,
)
from qa_deck.domain.snapshot import (
    SnapshotCaptureResult,
    SnapshotResource,
)
from qa_deck.storage import OperationLogRepository


class RiskLevel(StrEnum):
    """Risk associated with a plugin action."""

    SAFE = "safe"
    CAUTION = "caution"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class PluginAction:
    """Describe an action exposed by a plugin."""

    identifier: str
    display_name: str
    description: str
    risk_level: RiskLevel


class Plugin(Protocol):
    """Contract implemented by QA Deck plugins."""

    identifier: str
    display_name: str
    description: str
    version: str

    def get_actions(self) -> list[PluginAction]:
        """Return actions exposed by the plugin."""
        ...


class SnapshotProvider(Protocol):
    """Optional contract for plugins that can capture snapshot resources."""

    identifier: str

    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        ...


class EnvironmentProfileComparisonStatus(StrEnum):
    """Read-only comparison outcome for one Profile resource."""

    CHANGE = "change"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EnvironmentProfileComparisonEntry:
    resource_id: str
    display_name: str
    current_state: str
    desired_state: str
    status: EnvironmentProfileComparisonStatus
    message: str


@dataclass(frozen=True, slots=True)
class EnvironmentProfileComparisonSection:
    provider_identifier: str
    display_name: str
    reference_name: str | None
    entries: tuple[EnvironmentProfileComparisonEntry, ...] = ()


class EnvironmentProfileProvider(Protocol):
    """Optional read-only capability for Environment Profile comparison."""

    identifier: str

    def uses_environment_profile(self, profile: EnvironmentProfile) -> bool:
        ...

    def inspect_environment_profile_current(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> object:
        ...

    def compare_environment_profile(
        self,
        profile: EnvironmentProfile,
        configuration: PluginConfiguration | None,
        current: object,
    ) -> EnvironmentProfileComparisonSection | None:
        ...


@dataclass(frozen=True, slots=True)
class EnvironmentProfileExecutionPreparation:
    """Server-side execution plan for one Profile provider."""

    provider_identifier: str
    display_name: str
    reference_name: str | None
    entries: tuple[EnvironmentProfileComparisonEntry, ...]
    authority_fingerprint: str
    provider_context: object | None = None


class EnvironmentProfileExecutionStatus(StrEnum):
    """Outcome of applying one Profile resource."""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    NO_CHANGE = "no_change"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class EnvironmentProfileExecutionEntry:
    resource_id: str
    display_name: str
    current_state: str
    desired_state: str
    status: EnvironmentProfileExecutionStatus
    message: str
    changed_count: int = 0
    rollback_status: RollbackStatus | None = None
    backup_created: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnvironmentProfileProviderExecution:
    """Provider-local result; no cross-provider atomicity is implied."""

    entries: tuple[EnvironmentProfileExecutionEntry, ...]
    warnings: tuple[str, ...] = ()


class EnvironmentProfileExecutionProvider(Protocol):
    """Optional capability for planning and applying Profile state."""

    identifier: str

    def prepare_environment_profile_execution(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: PluginConfiguration | None,
        current: object,
    ) -> EnvironmentProfileExecutionPreparation:
        ...

    def execute_environment_profile(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: PluginConfiguration | None,
        expected: EnvironmentProfileExecutionPreparation,
        *,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> EnvironmentProfileProviderExecution:
        ...


@dataclass(frozen=True, slots=True)
class SnapshotRestorePreparation:
    """Read-only result returned while preparing one restore entry."""

    action_description: str
    risk_level: RiskLevel
    changes_required: bool = True
    fingerprint: str | None = None
    warnings: tuple[str, ...] = ()
    blocking_error: str | None = None


class SnapshotRestoreProvider(Protocol):
    """Optional contract for plugins that can prepare snapshot restoration."""

    identifier: str

    def can_restore(self, resource: SnapshotResource) -> bool:
        ...

    def prepare_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: PluginConfiguration | None,
    ) -> SnapshotRestorePreparation:
        ...


class SnapshotRestoreExecutionStatus(StrEnum):
    """Safe outcome returned by a plugin restore execution."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SnapshotRestoreExecution:
    """Plugin-level result for executing one restore entry."""

    status: SnapshotRestoreExecutionStatus
    message: str
    changed_count: int = 0
    rollback_status: RollbackStatus | None = None
    backup_created: bool = False
    warnings: tuple[str, ...] = ()


class SnapshotRestoreExecutionProvider(Protocol):
    """Optional contract for plugins that can execute prepared restoration."""

    identifier: str

    def execute_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: PluginConfiguration | None,
        *,
        expected_fingerprint: str,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> SnapshotRestoreExecution:
        ...
