"""Confirmed execution for configured Registry presets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from secrets import token_urlsafe
from threading import Lock

from qa_deck.plugins.builtin.windows_registry.models import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryBranchVisibility,
    RegistryDataType,
    RegistryPresetBranch,
    RegistryPresetValue,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
    WindowsRegistryConfiguration,
)
from qa_deck.plugins.builtin.windows_registry.planner import (
    RegistryBranchState,
    RegistryChangePlan,
    RegistryPlanEntry,
    RegistryPlanner,
    RegistryPlanOperation,
    RegistryPlanStatus,
    RegistryTargetType,
    RegistryValueState,
)
from qa_deck.plugins.builtin.windows_registry.reader import RegistryReader
from qa_deck.plugins.builtin.windows_registry.writer import RegistryWriter


class RegistryExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    NO_CHANGE = "no_change"
    SKIPPED = "skipped"


class RegistryRollbackStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RegistryExecutionExpectation:
    target_id: str
    target_type: RegistryTargetType
    display_name: str
    current_state: RegistryValueState | RegistryBranchState
    desired_state: RegistryPresetValue | RegistryPresetBranch
    preview_status: RegistryPlanStatus
    expected_fingerprint: str


@dataclass(frozen=True, slots=True)
class RegistryExecutionIntent:
    token: str
    product_id: str
    preset_id: str
    preset_name: str
    plan_fingerprint: str
    eligible_target_ids: tuple[str, ...]
    entries: tuple[RegistryExecutionExpectation, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RegistryExecutionEntryResult:
    target_id: str
    target_type: RegistryTargetType
    display_name: str
    current_state: RegistryValueState | RegistryBranchState
    desired_state: RegistryPresetValue | RegistryPresetBranch
    status: RegistryExecutionStatus
    message: str
    rollback_status: RegistryRollbackStatus = RegistryRollbackStatus.NOT_REQUIRED


@dataclass(frozen=True, slots=True)
class RegistryExecutionResult:
    product_id: str
    preset_id: str
    preset_name: str
    created_at: datetime
    entries: tuple[RegistryExecutionEntryResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    operation_log_saved: bool = True

    def count(self, status: RegistryExecutionStatus) -> int:
        return sum(entry.status is status for entry in self.entries)

    @property
    def succeeded_count(self) -> int:
        return self.count(RegistryExecutionStatus.SUCCEEDED)

    @property
    def failed_count(self) -> int:
        return self.count(RegistryExecutionStatus.FAILED)

    @property
    def stale_count(self) -> int:
        return self.count(RegistryExecutionStatus.STALE)

    @property
    def unsupported_count(self) -> int:
        return self.count(RegistryExecutionStatus.UNSUPPORTED)

    @property
    def blocked_count(self) -> int:
        return self.count(RegistryExecutionStatus.BLOCKED)


class RegistryExecutionStateStore:
    """Keep opaque one-time intents and PRG results in process memory."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._intents: dict[str, RegistryExecutionIntent] = {}
        self._results: dict[str, RegistryExecutionResult] = {}
        self._lock = Lock()

    def create_intent(
        self,
        product_id: str,
        configuration: WindowsRegistryConfiguration,
        plan: RegistryChangePlan,
    ) -> RegistryExecutionIntent:
        eligible = tuple(
            entry.target_id
            for entry in plan.entries
            if entry.status is RegistryPlanStatus.READY
            and entry.operation
            in {
                RegistryPlanOperation.SET_VALUE,
                RegistryPlanOperation.HIDE_BRANCH,
                RegistryPlanOperation.RESTORE_BRANCH,
            }
        )
        token = token_urlsafe(24)
        intent = RegistryExecutionIntent(
            token,
            product_id,
            plan.identifier,
            plan.display_name,
            registry_authority_fingerprint(configuration, plan.identifier),
            eligible,
            tuple(_expectation(entry) for entry in plan.entries),
            datetime.now(UTC),
        )
        with self._lock:
            self._trim(self._intents)
            self._intents[token] = intent
        return intent

    def take_intent(
        self,
        token: str,
        product_id: str,
        preset_id: str,
    ) -> RegistryExecutionIntent | None:
        with self._lock:
            intent = self._intents.pop(token, None)
        if (
            intent is None
            or intent.product_id != product_id
            or intent.preset_id != preset_id
        ):
            return None
        return intent

    def save_result(self, result: RegistryExecutionResult) -> str:
        result_id = token_urlsafe(18)
        with self._lock:
            self._trim(self._results)
            self._results[result_id] = result
        return result_id

    def get_result(
        self,
        result_id: str,
        product_id: str,
    ) -> RegistryExecutionResult | None:
        with self._lock:
            result = self._results.get(result_id)
        if result is None or result.product_id != product_id:
            return None
        return result

    def _trim(self, collection: dict[str, object]) -> None:
        while len(collection) >= self._limit:
            collection.pop(next(iter(collection)))


class RegistryPresetExecutor:
    """Rebuild plans and independently execute eligible configured entries."""

    def __init__(
        self,
        planner: RegistryPlanner,
        reader: RegistryReader,
        writer: RegistryWriter,
    ) -> None:
        self._planner = planner
        self._reader = reader
        self._writer = writer

    def execute(
        self,
        configuration: WindowsRegistryConfiguration,
        intent: RegistryExecutionIntent,
    ) -> RegistryExecutionResult:
        try:
            plan = self._planner.plan_preset(configuration, intent.preset_id)
            authority = registry_authority_fingerprint(
                configuration, intent.preset_id
            )
        except (KeyError, ValueError):
            return self._authority_stale(intent)
        if authority != intent.plan_fingerprint:
            return self._authority_stale(intent)

        expectations = {entry.target_id: entry for entry in intent.entries}
        value_targets = {item.id: item for item in configuration.value_targets}
        branch_targets = {item.id: item for item in configuration.branch_targets}
        results: list[RegistryExecutionEntryResult] = []
        for entry in plan.entries:
            expectation = expectations.get(entry.target_id)
            if (
                entry.target_type is RegistryTargetType.BRANCH
                and entry.status is RegistryPlanStatus.BLOCKED
            ):
                results.append(self._blocked_branch(entry))
                continue
            if entry.target_id not in intent.eligible_target_ids:
                results.append(self._not_eligible(entry))
                continue
            if entry.target_type is RegistryTargetType.BRANCH:
                if (
                    expectation is None
                    or entry.expected_fingerprint != expectation.expected_fingerprint
                    or entry.status is not RegistryPlanStatus.READY
                    or entry.operation
                    not in {
                        RegistryPlanOperation.HIDE_BRANCH,
                        RegistryPlanOperation.RESTORE_BRANCH,
                    }
                ):
                    results.append(self._stale(entry))
                    continue
                target = branch_targets.get(entry.target_id)
                if target is None or not target.enabled:
                    results.append(self._stale(entry))
                    continue
                results.append(
                    self.execute_entry(
                        target,
                        entry,
                        expectation.expected_fingerprint,
                    )
                )
                continue
            if (
                expectation is None
                or entry.expected_fingerprint != expectation.expected_fingerprint
                or entry.status is not RegistryPlanStatus.READY
                or entry.operation is not RegistryPlanOperation.SET_VALUE
            ):
                results.append(self._stale(entry))
                continue
            target = value_targets.get(entry.target_id)
            if target is None or not target.enabled:
                results.append(self._stale(entry))
                continue
            results.append(
                self.execute_entry(
                    target,
                    entry,
                    expectation.expected_fingerprint,
                )
            )
        return RegistryExecutionResult(
            intent.product_id,
            intent.preset_id,
            intent.preset_name,
            datetime.now(UTC),
            tuple(results),
        )

    def execute_entry(
        self,
        target: RegistryValueTarget | RegistryBranchTarget,
        entry: RegistryPlanEntry,
        expected_fingerprint: str,
    ) -> RegistryExecutionEntryResult:
        """Execute one freshly planned configured entry with stale protection."""
        if (
            entry.expected_fingerprint != expected_fingerprint
            or entry.status is not RegistryPlanStatus.READY
        ):
            return self._stale(entry)
        if isinstance(target, RegistryBranchTarget):
            if (
                entry.target_type is not RegistryTargetType.BRANCH
                or entry.operation
                not in {
                    RegistryPlanOperation.HIDE_BRANCH,
                    RegistryPlanOperation.RESTORE_BRANCH,
                }
            ):
                return self._stale(entry)
            return self._execute_branch(target, entry)
        if (
            entry.target_type is not RegistryTargetType.VALUE
            or entry.operation is not RegistryPlanOperation.SET_VALUE
        ):
            return self._stale(entry)
        return self._execute_value(target, entry)

    def _execute_branch(
        self,
        target: RegistryBranchTarget,
        entry: RegistryPlanEntry,
    ) -> RegistryExecutionEntryResult:
        current = entry.current_state
        desired = entry.desired_state
        if not isinstance(current, RegistryBranchState) or not isinstance(
            desired, RegistryPresetBranch
        ):
            return self._failed(entry, "Registry branch plan is invalid.")
        renamed = False
        try:
            self._writer.rename_branch(target, desired.visibility)
            renamed = True
            readback = self._reader.inspect_branch(target)
            if self._branch_matches(readback, desired.visibility):
                return _entry_result(
                    entry,
                    RegistryExecutionStatus.SUCCEEDED,
                    "Configured Registry branch was renamed and verified.",
                )
            failure = "Registry branch rename could not be verified."
        except OSError as error:
            failure = self._branch_failure_message(error)
        except Exception:
            failure = "Registry branch rename failed (native adapter error)."
        rollback = (
            self._rollback_branch(target, desired.visibility)
            if renamed
            else RegistryRollbackStatus.NOT_REQUIRED
        )
        return self._failed(entry, failure, rollback)

    @staticmethod
    def _branch_failure_message(error: OSError) -> str:
        error_code = getattr(error, "winerror", None)
        if error_code is None:
            error_code = error.errno
        if error_code is None:
            return "Registry branch rename failed (unknown Windows error)."
        description = error.strerror or "Unknown Windows error."
        return (
            f"Registry branch rename failed (Windows error {error_code}: "
            f"{description})."
        )

    def _rollback_branch(
        self,
        target: RegistryBranchTarget,
        desired_visibility: RegistryBranchVisibility,
    ) -> RegistryRollbackStatus:
        previous_visibility = (
            RegistryBranchVisibility.VISIBLE
            if desired_visibility is RegistryBranchVisibility.HIDDEN
            else RegistryBranchVisibility.HIDDEN
        )
        try:
            current = self._reader.inspect_branch(target)
            if not self._branch_matches(current, desired_visibility):
                return RegistryRollbackStatus.FAILED
            self._writer.rename_branch(target, previous_visibility)
            readback = self._reader.inspect_branch(target)
            return (
                RegistryRollbackStatus.SUCCEEDED
                if self._branch_matches(readback, previous_visibility)
                else RegistryRollbackStatus.FAILED
            )
        except Exception:
            return RegistryRollbackStatus.FAILED

    @staticmethod
    def _branch_matches(
        inspection: RegistryBranchInspection,
        visibility: RegistryBranchVisibility,
    ) -> bool:
        expected = (
            (True, False, RegistryBranchStatus.VISIBLE)
            if visibility is RegistryBranchVisibility.VISIBLE
            else (False, True, RegistryBranchStatus.HIDDEN)
        )
        return (
            inspection.original_exists,
            inspection.hidden_exists,
            inspection.status,
        ) == expected

    def _execute_value(
        self,
        target: RegistryValueTarget,
        entry: RegistryPlanEntry,
    ) -> RegistryExecutionEntryResult:
        current = entry.current_state
        desired = entry.desired_state
        if not isinstance(current, RegistryValueState) or not isinstance(
            desired, RegistryPresetValue
        ):
            return self._failed(entry, "Registry value plan is invalid.")
        try:
            previous_type = RegistryDataType(current.registry_type)
            previous = RegistryPresetValue.from_dict(
                {
                    "target_id": target.id,
                    "registry_type": previous_type.value,
                    "value": current.value,
                }
            )
        except (TypeError, ValueError):
            return self._failed(entry, "Previous Registry value cannot be restored.")

        try:
            self._writer.set_value(target, desired.registry_type, desired.value)
            readback = self._reader.inspect_value(target)
            if self._matches(readback, desired):
                return RegistryExecutionEntryResult(
                    entry.target_id,
                    entry.target_type,
                    entry.display_name,
                    current,
                    desired,
                    RegistryExecutionStatus.SUCCEEDED,
                    "Configured Registry value was changed and verified.",
                )
            failure = "Registry write could not be verified."
        except Exception:
            failure = "Registry value write failed."
        rollback = self._rollback(target, previous)
        return self._failed(entry, failure, rollback)

    def _rollback(
        self,
        target: RegistryValueTarget,
        previous: RegistryPresetValue,
    ) -> RegistryRollbackStatus:
        try:
            self._writer.set_value(target, previous.registry_type, previous.value)
            readback = self._reader.inspect_value(target)
            return (
                RegistryRollbackStatus.SUCCEEDED
                if self._matches(readback, previous)
                else RegistryRollbackStatus.FAILED
            )
        except Exception:
            return RegistryRollbackStatus.FAILED

    @staticmethod
    def _matches(
        inspection: RegistryValueInspection,
        desired: RegistryPresetValue,
    ) -> bool:
        desired_value = (
            list(desired.value) if isinstance(desired.value, tuple) else desired.value
        )
        return (
            inspection.status is RegistryValueStatus.AVAILABLE
            and inspection.exists
            and inspection.registry_type == desired.registry_type.value
            and inspection.value == desired_value
        )

    @staticmethod
    def _not_eligible(entry: RegistryPlanEntry) -> RegistryExecutionEntryResult:
        status = (
            RegistryExecutionStatus.NO_CHANGE
            if entry.status is RegistryPlanStatus.NO_CHANGE
            else RegistryExecutionStatus.SKIPPED
        )
        return _entry_result(entry, status, entry.message)

    @staticmethod
    def _stale(entry: RegistryPlanEntry) -> RegistryExecutionEntryResult:
        return _entry_result(
            entry,
            RegistryExecutionStatus.STALE,
            "Registry state changed after preview. Review changes again.",
        )

    @staticmethod
    def _blocked_branch(entry: RegistryPlanEntry) -> RegistryExecutionEntryResult:
        current = entry.current_state
        message = (
            "Both visible and hidden Registry branches exist."
            if isinstance(current, RegistryBranchState)
            and current.status == RegistryBranchStatus.CONFLICT.value
            else "Registry branch source is missing. Review changes again."
        )
        return _entry_result(entry, RegistryExecutionStatus.BLOCKED, message)

    @staticmethod
    def _failed(
        entry: RegistryPlanEntry,
        message: str,
        rollback: RegistryRollbackStatus = RegistryRollbackStatus.NOT_REQUIRED,
    ) -> RegistryExecutionEntryResult:
        return _entry_result(entry, RegistryExecutionStatus.FAILED, message, rollback)

    @staticmethod
    def _authority_stale(
        intent: RegistryExecutionIntent,
    ) -> RegistryExecutionResult:
        results = []
        for expected in intent.entries:
            status = RegistryExecutionStatus.STALE
            message = (
                "Registry configuration changed after preview. Review changes again."
            )
            results.append(
                RegistryExecutionEntryResult(
                    expected.target_id,
                    expected.target_type,
                    expected.display_name,
                    expected.current_state,
                    expected.desired_state,
                    status,
                    message,
                )
            )
        return RegistryExecutionResult(
            intent.product_id,
            intent.preset_id,
            intent.preset_name,
            datetime.now(UTC),
            tuple(results),
        )


def registry_authority_fingerprint(
    configuration: WindowsRegistryConfiguration,
    preset_id: str,
) -> str:
    preset = next(
        (item for item in configuration.presets if item.id == preset_id), None
    )
    if preset is None:
        raise ValueError("Registry preset does not exist")
    value_targets = {item.id: item for item in configuration.value_targets}
    branch_targets = {item.id: item for item in configuration.branch_targets}
    payload = {
        "enabled": configuration.enabled,
        "preset": preset.to_dict(),
        "value_targets": [
            value_targets[item.target_id].to_dict() for item in preset.values
        ],
        "branch_targets": [
            branch_targets[item.target_id].to_dict() for item in preset.branches
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _expectation(entry: RegistryPlanEntry) -> RegistryExecutionExpectation:
    return RegistryExecutionExpectation(
        entry.target_id,
        entry.target_type,
        entry.display_name,
        entry.current_state,
        entry.desired_state,
        entry.status,
        entry.expected_fingerprint,
    )


def _entry_result(
    entry: RegistryPlanEntry,
    status: RegistryExecutionStatus,
    message: str,
    rollback: RegistryRollbackStatus = RegistryRollbackStatus.NOT_REQUIRED,
) -> RegistryExecutionEntryResult:
    return RegistryExecutionEntryResult(
        entry.target_id,
        entry.target_type,
        entry.display_name,
        entry.current_state,
        entry.desired_state,
        status,
        message,
        rollback,
    )
