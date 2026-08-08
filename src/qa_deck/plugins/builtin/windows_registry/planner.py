"""Read-only change planning for configured Windows Registry targets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from qa_deck.plugins import RiskLevel
from qa_deck.plugins.builtin.windows_registry.models import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryInspectionResult,
    RegistryPresetBranch,
    RegistryPresetValue,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
    WindowsRegistryConfiguration,
)
from qa_deck.plugins.builtin.windows_registry.reader import RegistryReader


class RegistryPlanStatus(StrEnum):
    READY = "ready"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    ERROR = "error"


class RegistryPlanOperation(StrEnum):
    SET_VALUE = "set_value"
    HIDE_BRANCH = "hide_branch"
    RESTORE_BRANCH = "restore_branch"
    NONE = "none"


class RegistryTargetType(StrEnum):
    VALUE = "value"
    BRANCH = "branch"


@dataclass(frozen=True, slots=True)
class RegistryValueState:
    exists: bool
    registry_type: str | None
    value: object | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "registry_type": self.registry_type,
            "value": self.value,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class RegistryBranchState:
    visibility: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return {"visibility": self.visibility, "status": self.status}


@dataclass(frozen=True, slots=True)
class RegistryPlanEntry:
    target_id: str
    target_type: RegistryTargetType
    display_name: str
    current_state: RegistryValueState | RegistryBranchState
    desired_state: RegistryPresetValue | RegistryPresetBranch
    operation: RegistryPlanOperation
    risk_level: RiskLevel
    status: RegistryPlanStatus
    message: str
    expected_fingerprint: str


@dataclass(frozen=True, slots=True)
class RegistryChangePlan:
    identifier: str
    display_name: str
    entries: tuple[RegistryPlanEntry, ...]

    @property
    def ready_count(self) -> int:
        return self._count(RegistryPlanStatus.READY)

    @property
    def blocked_count(self) -> int:
        return self._count(RegistryPlanStatus.BLOCKED)

    @property
    def no_change_count(self) -> int:
        return self._count(RegistryPlanStatus.NO_CHANGE)

    @property
    def error_count(self) -> int:
        return self._count(RegistryPlanStatus.ERROR)

    def _count(self, status: RegistryPlanStatus) -> int:
        return sum(entry.status is status for entry in self.entries)


class RegistryPlanner:
    """Compare inspected current state with explicit configured desired state."""

    def __init__(self, reader: RegistryReader) -> None:
        self._reader = reader

    def plan_preset(
        self,
        configuration: WindowsRegistryConfiguration,
        preset_id: str,
        *,
        inspection: RegistryInspectionResult | None = None,
    ) -> RegistryChangePlan:
        preset = next(
            (item for item in configuration.presets if item.id == preset_id),
            None,
        )
        if preset is None:
            raise ValueError("Registry preset does not exist")
        value_targets = {item.id: item for item in configuration.value_targets}
        branch_targets = {item.id: item for item in configuration.branch_targets}
        inspected_values = (
            {item.target.id: item for item in inspection.values}
            if inspection is not None
            else None
        )
        inspected_branches = (
            {item.target.id: item for item in inspection.branches}
            if inspection is not None
            else None
        )
        entries: list[RegistryPlanEntry] = []
        for desired in preset.values:
            target = value_targets[desired.target_id]
            if inspected_values is None:
                entries.append(self.plan_value(target, desired))
                continue
            inspected = inspected_values.get(target.id)
            entries.append(
                self._value_entry(target, desired, inspected)
                if inspected is not None
                else self._missing_inspection_entry(target, desired)
            )
        for desired in preset.branches:
            target = branch_targets[desired.target_id]
            if inspected_branches is None:
                entries.append(self.plan_branch(target, desired))
                continue
            inspected = inspected_branches.get(target.id)
            entries.append(
                self._branch_entry(target, desired, inspected)
                if inspected is not None
                else self._missing_inspection_entry(target, desired)
            )
        return RegistryChangePlan(preset.id, preset.name, tuple(entries))

    def plan_value(
        self,
        target: RegistryValueTarget,
        desired: RegistryPresetValue,
    ) -> RegistryPlanEntry:
        try:
            inspection = self._reader.inspect_value(target)
            return self._value_entry(target, desired, inspection)
        except Exception:
            current = RegistryValueState(False, None, None, "error")
            return self._error_entry(target.id, target.display_name, current, desired)

    def plan_inspected_value(
        self,
        target: RegistryValueTarget,
        desired: RegistryPresetValue,
        inspection: RegistryValueInspection,
    ) -> RegistryPlanEntry:
        """Plan from an already captured Current value without rereading it."""
        return self._value_entry(target, desired, inspection)

    def plan_branch(
        self,
        target: RegistryBranchTarget,
        desired: RegistryPresetBranch,
    ) -> RegistryPlanEntry:
        try:
            inspection = self._reader.inspect_branch(target)
            return self._branch_entry(target, desired, inspection)
        except Exception:
            current = RegistryBranchState("error", "error")
            return self._error_entry(target.id, target.display_name, current, desired)

    def plan_inspected_branch(
        self,
        target: RegistryBranchTarget,
        desired: RegistryPresetBranch,
        inspection: RegistryBranchInspection,
    ) -> RegistryPlanEntry:
        """Plan from an already captured Current branch without rereading it."""
        return self._branch_entry(target, desired, inspection)

    def _value_entry(
        self,
        target: RegistryValueTarget,
        desired: RegistryPresetValue,
        inspection: RegistryValueInspection,
    ) -> RegistryPlanEntry:
        current = RegistryValueState(
            inspection.exists,
            inspection.registry_type,
            inspection.value,
            inspection.status.value,
        )
        fingerprint = self._fingerprint(target.id, current.to_dict())
        if inspection.status is RegistryValueStatus.ERROR:
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.ERROR,
                RegistryPlanOperation.NONE,
                "Не вдалося прочитати поточний стан Registry value.",
                fingerprint,
            )
        if inspection.status is not RegistryValueStatus.AVAILABLE:
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.BLOCKED,
                RegistryPlanOperation.NONE,
                "Поточний Registry value недоступний для безпечної зміни.",
                fingerprint,
            )
        desired_value = (
            list(desired.value) if isinstance(desired.value, tuple) else desired.value
        )
        if (
            inspection.registry_type == desired.registry_type.value
            and inspection.value == desired_value
        ):
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.NO_CHANGE,
                RegistryPlanOperation.NONE,
                "Registry value вже має бажаний тип і значення.",
                fingerprint,
            )
        return self._entry(
            target,
            current,
            desired,
            RegistryPlanStatus.READY,
            RegistryPlanOperation.SET_VALUE,
            "Registry value потребує зміни типу або значення.",
            fingerprint,
        )

    def _branch_entry(
        self,
        target: RegistryBranchTarget,
        desired: RegistryPresetBranch,
        inspection: RegistryBranchInspection,
    ) -> RegistryPlanEntry:
        current = RegistryBranchState(
            inspection.status.value,
            inspection.status.value,
        )
        fingerprint = self._fingerprint(target.id, current.to_dict())
        if inspection.status is RegistryBranchStatus.ERROR:
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.ERROR,
                RegistryPlanOperation.NONE,
                "Не вдалося прочитати поточний стан Registry branch.",
                fingerprint,
            )
        if inspection.status in {
            RegistryBranchStatus.CONFLICT,
            RegistryBranchStatus.MISSING,
            RegistryBranchStatus.UNAVAILABLE,
        }:
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.BLOCKED,
                RegistryPlanOperation.NONE,
                "Безпечна reversible rename operation зараз недоступна.",
                fingerprint,
            )
        if inspection.status.value == desired.visibility.value:
            return self._entry(
                target,
                current,
                desired,
                RegistryPlanStatus.NO_CHANGE,
                RegistryPlanOperation.NONE,
                "Registry branch вже має бажану видимість.",
                fingerprint,
            )
        operation = (
            RegistryPlanOperation.HIDE_BRANCH
            if desired.visibility.value == "hidden"
            else RegistryPlanOperation.RESTORE_BRANCH
        )
        return self._entry(
            target,
            current,
            desired,
            RegistryPlanStatus.READY,
            operation,
            "Зміна означатиме лише reversible rename без overwrite.",
            fingerprint,
        )

    @staticmethod
    def _entry(
        target: RegistryValueTarget | RegistryBranchTarget,
        current: RegistryValueState | RegistryBranchState,
        desired: RegistryPresetValue | RegistryPresetBranch,
        status: RegistryPlanStatus,
        operation: RegistryPlanOperation,
        message: str,
        fingerprint: str,
    ) -> RegistryPlanEntry:
        target_type = (
            RegistryTargetType.VALUE
            if isinstance(target, RegistryValueTarget)
            else RegistryTargetType.BRANCH
        )
        return RegistryPlanEntry(
            target.id,
            target_type,
            target.display_name or target.id,
            current,
            desired,
            operation,
            RiskLevel.SAFE
            if status is RegistryPlanStatus.NO_CHANGE
            else RiskLevel.CAUTION,
            status,
            message,
            fingerprint,
        )

    @classmethod
    def _error_entry(
        cls,
        target_id: str,
        display_name: str | None,
        current: RegistryValueState | RegistryBranchState,
        desired: RegistryPresetValue | RegistryPresetBranch,
    ) -> RegistryPlanEntry:
        target_type = (
            RegistryTargetType.VALUE
            if isinstance(desired, RegistryPresetValue)
            else RegistryTargetType.BRANCH
        )
        return RegistryPlanEntry(
            target_id,
            target_type,
            display_name or target_id,
            current,
            desired,
            RegistryPlanOperation.NONE,
            RiskLevel.CAUTION,
            RegistryPlanStatus.ERROR,
            "Не вдалося підготувати preview для цього Registry target.",
            cls._fingerprint(target_id, current.to_dict()),
        )

    @classmethod
    def _missing_inspection_entry(
        cls,
        target: RegistryValueTarget | RegistryBranchTarget,
        desired: RegistryPresetValue | RegistryPresetBranch,
    ) -> RegistryPlanEntry:
        current: RegistryValueState | RegistryBranchState
        if isinstance(target, RegistryValueTarget):
            current = RegistryValueState(False, None, None, "error")
        else:
            current = RegistryBranchState("error", "error")
        return cls._error_entry(
            target.id,
            target.display_name or target.id,
            current,
            desired,
        )

    @staticmethod
    def _fingerprint(target_id: str, state: dict[str, object]) -> str:
        payload = json.dumps(
            {"target_id": target_id, "state": state},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
