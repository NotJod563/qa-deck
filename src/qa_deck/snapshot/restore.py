"""Read-only planning for restoring current state toward a snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from logging import Logger
from pathlib import Path
from secrets import token_urlsafe
from threading import Lock
from uuid import uuid4

from qa_deck.domain import (
    OperationLog,
    OperationStatus,
    Product,
    RollbackStatus,
    Snapshot,
    SnapshotResource,
)
from qa_deck.plugins.api import (
    RiskLevel,
    SnapshotRestoreExecution,
    SnapshotRestoreExecutionStatus,
    SnapshotRestorePreparation,
)
from qa_deck.plugins.manager import PluginManager
from qa_deck.snapshot.builder import SnapshotBuilder
from qa_deck.snapshot.diff import SnapshotDiffer, SnapshotDiffStatus
from qa_deck.storage import OperationLogRepository, PluginConfigurationRepository


class RestorePlanStatus(StrEnum):
    """Outcome of planning restoration for one snapshot resource."""

    READY = "ready"
    NO_CHANGE = "no_change"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RestorePlanEntry:
    source: str
    resource_type: str
    identifier: str
    current_resource: SnapshotResource | None
    desired_resource: SnapshotResource | None
    action_description: str
    risk_level: RiskLevel | None
    status: RestorePlanStatus
    fingerprint: str | None = None
    warnings: tuple[str, ...] = ()
    blocking_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRestorePlan:
    product_id: str
    snapshot_id: str
    created_at: datetime
    entries: tuple[RestorePlanEntry, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def count(self, status: RestorePlanStatus) -> int:
        return sum(1 for entry in self.entries if entry.status is status)

    @property
    def ready_count(self) -> int:
        return self.count(RestorePlanStatus.READY)

    @property
    def unsupported_count(self) -> int:
        return self.count(RestorePlanStatus.UNSUPPORTED)

    @property
    def blocked_count(self) -> int:
        return self.count(RestorePlanStatus.BLOCKED)

    @property
    def no_change_count(self) -> int:
        return self.count(RestorePlanStatus.NO_CHANGE)

    @property
    def error_count(self) -> int:
        return self.count(RestorePlanStatus.ERROR)

    @property
    def blocked_entries(self) -> tuple[RestorePlanEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.status in {RestorePlanStatus.BLOCKED, RestorePlanStatus.ERROR}
        )

    @property
    def summary(self) -> dict[str, int]:
        return {status.value: self.count(status) for status in RestorePlanStatus}


@dataclass(frozen=True, slots=True)
class RestoreEntryExpectation:
    source: str
    resource_type: str
    identifier: str
    status: RestorePlanStatus
    fingerprint: str | None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.source, self.resource_type, self.identifier)


@dataclass(frozen=True, slots=True)
class SnapshotRestoreIntent:
    token: str
    product_id: str
    snapshot_id: str
    entries: tuple[RestoreEntryExpectation, ...]


@dataclass(frozen=True, slots=True)
class SnapshotRestoreEntryResult:
    source: str
    resource_type: str
    identifier: str
    requested_transition: str
    status: SnapshotRestoreExecutionStatus
    message: str
    changed_count: int = 0
    rollback_status: RollbackStatus | None = None
    backup_created: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotRestoreResult:
    product_id: str
    snapshot_id: str
    created_at: datetime
    entries: tuple[SnapshotRestoreEntryResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    operation_log_saved: bool = True

    def count(self, status: SnapshotRestoreExecutionStatus) -> int:
        return sum(1 for entry in self.entries if entry.status is status)

    @property
    def succeeded_count(self) -> int:
        return self.count(SnapshotRestoreExecutionStatus.SUCCESS)

    @property
    def failed_count(self) -> int:
        return self.count(SnapshotRestoreExecutionStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        return self.count(SnapshotRestoreExecutionStatus.SKIPPED)

    @property
    def stale_count(self) -> int:
        return self.count(SnapshotRestoreExecutionStatus.STALE)

    @property
    def unsupported_count(self) -> int:
        return self.count(SnapshotRestoreExecutionStatus.UNSUPPORTED)

    @property
    def summary(self) -> dict[str, int]:
        return {
            status.value: self.count(status)
            for status in SnapshotRestoreExecutionStatus
        }


class SnapshotRestoreStateStore:
    """Keep one-time restore intents and PRG results in process memory."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._intents: dict[str, SnapshotRestoreIntent] = {}
        self._results: dict[str, SnapshotRestoreResult] = {}
        self._lock = Lock()

    def create_intent(self, plan: SnapshotRestorePlan) -> SnapshotRestoreIntent:
        token = token_urlsafe(24)
        intent = SnapshotRestoreIntent(
            token=token,
            product_id=plan.product_id,
            snapshot_id=plan.snapshot_id,
            entries=tuple(
                RestoreEntryExpectation(
                    entry.source,
                    entry.resource_type,
                    entry.identifier,
                    entry.status,
                    entry.fingerprint,
                )
                for entry in plan.entries
            ),
        )
        with self._lock:
            self._trim(self._intents)
            self._intents[token] = intent
        return intent

    def take_intent(
        self,
        token: str,
        product_id: str,
        snapshot_id: str,
    ) -> SnapshotRestoreIntent | None:
        with self._lock:
            intent = self._intents.pop(token, None)
        if (
            intent is None
            or intent.product_id != product_id
            or intent.snapshot_id != snapshot_id
        ):
            return None
        return intent

    def save_result(self, result: SnapshotRestoreResult) -> str:
        result_id = token_urlsafe(18)
        with self._lock:
            self._trim(self._results)
            self._results[result_id] = result
        return result_id

    def get_result(
        self,
        result_id: str,
        product_id: str,
        snapshot_id: str,
    ) -> SnapshotRestoreResult | None:
        with self._lock:
            result = self._results.get(result_id)
        if (
            result is None
            or result.product_id != product_id
            or result.snapshot_id != snapshot_id
        ):
            return None
        return result

    def _trim(self, values: dict[str, object]) -> None:
        while len(values) >= self._limit:
            values.pop(next(iter(values)))


class SnapshotRestorePlanner:
    """Coordinate plugin restore previews without changing or persisting state."""

    def __init__(
        self,
        snapshot_builder: SnapshotBuilder,
        snapshot_differ: SnapshotDiffer,
        plugin_manager: PluginManager,
        configuration_repository: PluginConfigurationRepository,
        logger: Logger | None = None,
    ) -> None:
        self._snapshot_builder = snapshot_builder
        self._snapshot_differ = snapshot_differ
        self._plugin_manager = plugin_manager
        self._configuration_repository = configuration_repository
        self._logger = logger

    def prepare(self, product: Product, snapshot: Snapshot) -> SnapshotRestorePlan:
        if snapshot.product_id != product.id:
            raise ValueError("Snapshot belongs to a different product")

        current_snapshot = self._snapshot_builder.build_snapshot(product)
        snapshot_diff = self._snapshot_differ.diff(current_snapshot, snapshot)
        current_resources = self._resource_map(current_snapshot)
        desired_resources = self._resource_map(snapshot)
        entries: list[RestorePlanEntry] = []
        warnings = self._snapshot_warnings(current_snapshot)

        for diff_entry in snapshot_diff.entries:
            identity = (
                diff_entry.source,
                diff_entry.resource_type,
                diff_entry.identifier,
            )
            current_resource = current_resources.get(identity)
            desired_resource = desired_resources.get(identity)
            if diff_entry.status is SnapshotDiffStatus.UNCHANGED:
                entries.append(
                    self._entry(
                        current_resource,
                        desired_resource,
                        RestorePlanStatus.NO_CHANGE,
                        "Current resource already matches the snapshot.",
                    )
                )
                continue

            entries.append(
                self._prepare_changed_entry(
                    product,
                    current_resource,
                    desired_resource,
                )
            )

        return SnapshotRestorePlan(
            product_id=product.id,
            snapshot_id=snapshot.id,
            created_at=datetime.now(UTC),
            entries=tuple(entries),
            warnings=warnings,
        )

    def _prepare_changed_entry(
        self,
        product: Product,
        current_resource: SnapshotResource | None,
        desired_resource: SnapshotResource | None,
    ) -> RestorePlanEntry:
        resource = desired_resource or current_resource
        if resource is None:  # pragma: no cover - protected by SnapshotDiffer
            raise ValueError("Restore diff entry has no resource")

        try:
            plugin = self._plugin_manager.get(resource.source)
            if plugin is None:
                return self._entry(
                    current_resource,
                    desired_resource,
                    RestorePlanStatus.UNSUPPORTED,
                    "Restore is unavailable for this resource.",
                )
            can_restore = getattr(plugin, "can_restore", None)
            prepare_restore = getattr(plugin, "prepare_restore", None)
            execute_restore = getattr(plugin, "execute_restore", None)
            if (
                not callable(can_restore)
                or not callable(prepare_restore)
                or not callable(execute_restore)
            ):
                return self._entry(
                    current_resource,
                    desired_resource,
                    RestorePlanStatus.UNSUPPORTED,
                    (
                        f"{plugin.display_name} не підтримує автоматичне "
                        "відновлення цього ресурсу."
                    ),
                )
            supported = can_restore(resource)
            if type(supported) is not bool:
                raise TypeError(
                    "Restore provider returned an invalid capability result"
                )
            if not supported:
                return self._entry(
                    current_resource,
                    desired_resource,
                    RestorePlanStatus.UNSUPPORTED,
                    (
                        "Тип або версія цього snapshot ресурсу не підтримує "
                        "автоматичне відновлення."
                    ),
                )
            configuration = self._configuration_repository.get(
                product.id,
                plugin.identifier,
            )
            preparation = prepare_restore(
                product,
                desired_resource,
                current_resource,
                configuration,
            )
            self._validate_preparation(preparation)
        except Exception:
            if self._logger is not None:
                self._logger.exception(
                    "Snapshot restore provider failed for %s",
                    resource.source,
                )
            return self._entry(
                current_resource,
                desired_resource,
                RestorePlanStatus.ERROR,
                "Не вдалося підготувати безпечний план відновлення ресурсу.",
                warnings=("The restore provider could not prepare this entry.",),
                blocking_reason=(
                    "Перевірте поточну конфігурацію provider перед відновленням."
                ),
            )

        if preparation.blocking_error is not None:
            status = RestorePlanStatus.BLOCKED
            blocking_reason = preparation.blocking_error
        elif not preparation.changes_required:
            status = RestorePlanStatus.NO_CHANGE
            blocking_reason = None
        elif preparation.fingerprint is None:
            status = RestorePlanStatus.BLOCKED
            blocking_reason = (
                "Provider не надав fingerprint поточного стану; безпечне "
                "автоматичне відновлення неможливе."
            )
        else:
            status = RestorePlanStatus.READY
            blocking_reason = None
        return self._entry(
            current_resource,
            desired_resource,
            status,
            preparation.action_description,
            risk_level=preparation.risk_level,
            fingerprint=preparation.fingerprint,
            warnings=preparation.warnings,
            blocking_reason=blocking_reason,
        )

    @staticmethod
    def _validate_preparation(preparation: object) -> None:
        if not isinstance(preparation, SnapshotRestorePreparation):
            raise TypeError("Restore provider returned an invalid preparation")
        if (
            not isinstance(preparation.action_description, str)
            or not preparation.action_description.strip()
            or not isinstance(preparation.risk_level, RiskLevel)
            or type(preparation.changes_required) is not bool
            or (
                preparation.fingerprint is not None
                and not isinstance(preparation.fingerprint, str)
            )
            or not isinstance(preparation.warnings, tuple)
            or not all(isinstance(item, str) for item in preparation.warnings)
            or (
                preparation.blocking_error is not None
                and not isinstance(preparation.blocking_error, str)
            )
        ):
            raise TypeError("Restore provider returned invalid preparation fields")

    @staticmethod
    def _entry(
        current_resource: SnapshotResource | None,
        desired_resource: SnapshotResource | None,
        status: RestorePlanStatus,
        action_description: str,
        *,
        risk_level: RiskLevel | None = None,
        fingerprint: str | None = None,
        warnings: tuple[str, ...] = (),
        blocking_reason: str | None = None,
    ) -> RestorePlanEntry:
        resource = desired_resource or current_resource
        if resource is None:  # pragma: no cover - protected by caller
            raise ValueError("Restore plan entry has no resource")
        return RestorePlanEntry(
            source=resource.source,
            resource_type=resource.resource_type,
            identifier=resource.identifier,
            current_resource=current_resource,
            desired_resource=desired_resource,
            action_description=action_description,
            risk_level=risk_level,
            status=status,
            fingerprint=fingerprint,
            warnings=warnings,
            blocking_reason=blocking_reason,
        )

    @staticmethod
    def _resource_map(
        snapshot: Snapshot,
    ) -> dict[tuple[str, str, str], SnapshotResource]:
        return {
            (resource.source, resource.resource_type, resource.identifier): resource
            for resource in snapshot.resources
        }

    @staticmethod
    def _snapshot_warnings(snapshot: Snapshot) -> tuple[str, ...]:
        warnings = snapshot.metadata.get("warnings", ())
        if not isinstance(warnings, tuple):
            return ()
        return tuple(item for item in warnings if isinstance(item, str))


class SnapshotRestoreExecutor:
    """Rebuild and execute only unchanged, explicitly prepared restore entries."""

    def __init__(
        self,
        planner: SnapshotRestorePlanner,
        plugin_manager: PluginManager,
        configuration_repository: PluginConfigurationRepository,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> None:
        self._planner = planner
        self._plugin_manager = plugin_manager
        self._configuration_repository = configuration_repository
        self._backup_root = backup_root
        self._operation_logs = operation_logs
        self._logger = logger

    def execute(
        self,
        product: Product,
        snapshot: Snapshot,
        intent: SnapshotRestoreIntent,
    ) -> SnapshotRestoreResult:
        if (
            snapshot.product_id != product.id
            or intent.product_id != product.id
            or intent.snapshot_id != snapshot.id
        ):
            raise ValueError("Restore intent does not match product and snapshot")

        plan = self._planner.prepare(product, snapshot)
        expected = {entry.identity: entry for entry in intent.entries}
        results: list[SnapshotRestoreEntryResult] = []
        seen: set[tuple[str, str, str]] = set()

        for entry in plan.entries:
            identity = (entry.source, entry.resource_type, entry.identifier)
            seen.add(identity)
            expectation = expected.get(identity)
            results.append(
                self._execute_entry(product, entry, expectation)
            )

        for expectation in intent.entries:
            if expectation.identity in seen:
                continue
            results.append(
                SnapshotRestoreEntryResult(
                    source=expectation.source,
                    resource_type=expectation.resource_type,
                    identifier=expectation.identifier,
                    requested_transition=(
                        "The prepared resource is no longer available."
                    ),
                    status=SnapshotRestoreExecutionStatus.STALE,
                    message=(
                        "Current state changed after the Restore Plan was prepared."
                    ),
                )
            )

        result = SnapshotRestoreResult(
            product_id=product.id,
            snapshot_id=snapshot.id,
            created_at=datetime.now(UTC),
            entries=tuple(results),
            warnings=plan.warnings,
        )
        return self._append_operation_log(result)

    def _execute_entry(
        self,
        product: Product,
        entry: RestorePlanEntry,
        expectation: RestoreEntryExpectation | None,
    ) -> SnapshotRestoreEntryResult:
        if expectation is None:
            return self._result(
                entry,
                SnapshotRestoreExecutionStatus.STALE,
                "This resource was not part of the confirmed Restore Plan.",
            )
        if expectation.status is RestorePlanStatus.READY:
            if (
                entry.status is not RestorePlanStatus.READY
                or not expectation.fingerprint
                or expectation.fingerprint != entry.fingerprint
            ):
                return self._result(
                    entry,
                    SnapshotRestoreExecutionStatus.STALE,
                    "Current state changed after the Restore Plan was prepared.",
                )
            return self._execute_ready_entry(
                product,
                entry,
                expectation.fingerprint,
            )
        if entry.status is not expectation.status:
            return self._result(
                entry,
                SnapshotRestoreExecutionStatus.STALE,
                "Current state changed after the Restore Plan was prepared.",
            )
        if entry.status is RestorePlanStatus.UNSUPPORTED:
            return self._result(
                entry,
                SnapshotRestoreExecutionStatus.UNSUPPORTED,
                "Restore execution is unavailable for this resource.",
            )
        return self._result(
            entry,
            SnapshotRestoreExecutionStatus.SKIPPED,
            "This resource does not require an executable restore action.",
        )

    def _execute_ready_entry(
        self,
        product: Product,
        entry: RestorePlanEntry,
        expected_fingerprint: str,
    ) -> SnapshotRestoreEntryResult:
        try:
            plugin = self._plugin_manager.get(entry.source)
            execute_restore = getattr(plugin, "execute_restore", None)
            if plugin is None or not callable(execute_restore):
                return self._result(
                    entry,
                    SnapshotRestoreExecutionStatus.UNSUPPORTED,
                    "The current plugin does not support restore execution.",
                )
            configuration = self._configuration_repository.get(
                product.id,
                plugin.identifier,
            )
            execution = execute_restore(
                product,
                entry.desired_resource,
                entry.current_resource,
                configuration,
                expected_fingerprint=expected_fingerprint,
                backup_root=self._backup_root,
                operation_logs=self._operation_logs,
                logger=self._logger,
            )
            self._validate_execution(execution)
        except Exception:
            if self._logger is not None:
                self._logger.exception(
                    "Snapshot restore execution failed for %s",
                    entry.source,
                )
            return self._result(
                entry,
                SnapshotRestoreExecutionStatus.FAILED,
                "Restore execution failed for this resource.",
            )
        return self._result(
            entry,
            execution.status,
            execution.message,
            changed_count=execution.changed_count,
            rollback_status=execution.rollback_status,
            backup_created=execution.backup_created,
            warnings=execution.warnings,
        )

    @staticmethod
    def _validate_execution(execution: object) -> None:
        if not isinstance(execution, SnapshotRestoreExecution):
            raise TypeError("Restore provider returned an invalid execution result")
        if (
            not isinstance(execution.status, SnapshotRestoreExecutionStatus)
            or not isinstance(execution.message, str)
            or not execution.message.strip()
            or type(execution.changed_count) is not int
            or execution.changed_count < 0
            or (
                execution.rollback_status is not None
                and not isinstance(execution.rollback_status, RollbackStatus)
            )
            or type(execution.backup_created) is not bool
            or not isinstance(execution.warnings, tuple)
            or not all(isinstance(item, str) for item in execution.warnings)
        ):
            raise TypeError("Restore provider returned invalid execution fields")

    @staticmethod
    def _result(
        entry: RestorePlanEntry,
        status: SnapshotRestoreExecutionStatus,
        message: str,
        *,
        changed_count: int = 0,
        rollback_status: RollbackStatus | None = None,
        backup_created: bool = False,
        warnings: tuple[str, ...] = (),
    ) -> SnapshotRestoreEntryResult:
        return SnapshotRestoreEntryResult(
            source=entry.source,
            resource_type=entry.resource_type,
            identifier=entry.identifier,
            requested_transition=entry.action_description,
            status=status,
            message=message,
            changed_count=changed_count,
            rollback_status=rollback_status,
            backup_created=backup_created,
            warnings=warnings,
        )

    def _append_operation_log(
        self,
        result: SnapshotRestoreResult,
    ) -> SnapshotRestoreResult:
        try:
            self._operation_logs.append(
                OperationLog(
                    id=str(uuid4()),
                    timestamp=result.created_at,
                    product_id=result.product_id,
                    plugin_identifier="snapshot-restore",
                    action_identifier="restore-snapshot",
                    status=self._operation_status(result),
                    summary=(
                        "Snapshot restore completed: "
                        f"{result.succeeded_count} succeeded, "
                        f"{result.failed_count} failed, "
                        f"{result.stale_count} stale."
                    ),
                    changed_count=result.succeeded_count,
                    skipped_count=(
                        result.skipped_count + result.unsupported_count
                    ),
                    error_count=result.failed_count + result.stale_count,
                )
            )
        except Exception:
            if self._logger is not None:
                self._logger.exception("Could not append Snapshot Restore log")
            return replace(
                result,
                operation_log_saved=False,
                warnings=(
                    *result.warnings,
                    "Restore completed, but its operation log was not saved.",
                ),
            )
        return result

    @staticmethod
    def _operation_status(result: SnapshotRestoreResult) -> OperationStatus:
        errors = result.failed_count + result.stale_count
        if result.succeeded_count and errors:
            return OperationStatus.PARTIAL
        if result.succeeded_count:
            return OperationStatus.SUCCESS
        if result.stale_count:
            return OperationStatus.REJECTED
        if result.failed_count:
            return OperationStatus.FAILED
        return OperationStatus.NO_CHANGES
