"""Read-only Windows Registry plugin facade."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from logging import Logger
from pathlib import Path
from uuid import uuid4

from qa_deck.domain import (
    EnvironmentProfile,
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    PluginSetupSection,
    Product,
    ProductSetupProduct,
    RollbackStatus,
)
from qa_deck.domain.snapshot import SnapshotCaptureResult, SnapshotResource
from qa_deck.plugins import Plugin, PluginAction, RiskLevel
from qa_deck.plugins.api import (
    EnvironmentProfileComparisonEntry,
    EnvironmentProfileComparisonSection,
    EnvironmentProfileComparisonStatus,
    EnvironmentProfileExecutionEntry,
    EnvironmentProfileExecutionPreparation,
    EnvironmentProfileExecutionStatus,
    EnvironmentProfileProviderExecution,
    SnapshotRestoreExecution,
    SnapshotRestoreExecutionStatus,
    SnapshotRestorePreparation,
)
from qa_deck.plugins.builtin.windows_registry.execution import (
    RegistryExecutionEntryResult,
    RegistryExecutionIntent,
    RegistryExecutionResult,
    RegistryExecutionStateStore,
    RegistryExecutionStatus,
    RegistryPresetExecutor,
    RegistryRollbackStatus,
)
from qa_deck.plugins.builtin.windows_registry.models import (
    PLUGIN_IDENTIFIER,
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
from qa_deck.plugins.builtin.windows_registry.planner import (
    RegistryBranchState,
    RegistryChangePlan,
    RegistryPlanEntry,
    RegistryPlanner,
    RegistryPlanStatus,
    RegistryValueState,
)
from qa_deck.plugins.builtin.windows_registry.reader import (
    RegistryReader,
    WindowsRegistryReader,
)
from qa_deck.plugins.builtin.windows_registry.writer import (
    RegistryWriter,
    WindowsRegistryWriter,
)
from qa_deck.product_setup import SetupPluginImportPreparation, SetupPluginPreview
from qa_deck.storage import OperationLogRepository


class WindowsRegistry:
    identifier = PLUGIN_IDENTIFIER
    display_name = "Windows Registry"
    description = "Inspects explicitly configured Registry values and branches."
    version = "0.1.0"

    def __init__(
        self,
        reader: RegistryReader | None = None,
        writer: RegistryWriter | None = None,
    ) -> None:
        self._reader = reader or WindowsRegistryReader()
        self._planner = RegistryPlanner(self._reader)
        self._executor = RegistryPresetExecutor(
            self._planner,
            self._reader,
            writer or WindowsRegistryWriter(),
        )

    def get_actions(self) -> list[PluginAction]:
        return [
            PluginAction(
                "inspect-registry",
                "Inspect Registry",
                "Read configured Registry targets without changing them.",
                RiskLevel.SAFE,
            ),
            PluginAction(
                "preview-registry-preset",
                "Preview Registry preset",
                "Show desired configured states without applying them.",
                RiskLevel.SAFE,
            ),
            PluginAction(
                "apply-registry-preset-values",
                "Apply Registry preset values",
                "Change only confirmed values at configured Registry targets.",
                RiskLevel.CAUTION,
            ),
        ]

    def create_configuration(
        self,
        *,
        product_id: str,
        enabled: bool,
        value_targets_json: str,
        branch_targets_json: str,
        presets_json: str,
    ) -> PluginConfiguration:
        typed = WindowsRegistryConfiguration.create(
            enabled=enabled,
            value_targets=self._json_array(value_targets_json, "Value targets"),
            branch_targets=self._json_array(
                branch_targets_json,
                "Branch targets",
            ),
            presets=self._json_array(presets_json, "Registry presets"),
        )
        return PluginConfiguration(
            product_id=product_id,
            plugin_identifier=self.identifier,
            enabled=enabled,
            settings=typed.to_settings(),
        )

    def typed_configuration(
        self,
        configuration: PluginConfiguration | None,
    ) -> WindowsRegistryConfiguration | None:
        if configuration is None:
            return None
        return WindowsRegistryConfiguration.from_plugin_configuration(configuration)

    def export_product_setup(
        self,
        product: ProductSetupProduct,
        configuration: PluginConfiguration,
    ) -> PluginSetupSection:
        del product
        typed = self.typed_configuration(configuration)
        if typed is None:  # pragma: no cover
            raise ValueError("Windows Registry configuration is missing")
        return PluginSetupSection(
            self.identifier,
            1,
            {
                "enabled": configuration.enabled,
                "value_targets": [item.to_dict() for item in typed.value_targets],
                "branch_targets": [item.to_dict() for item in typed.branch_targets],
                "presets_omitted": len(typed.presets),
            },
        )

    def preview_product_setup(
        self,
        product: ProductSetupProduct,
        section: PluginSetupSection,
    ) -> SetupPluginPreview:
        del product
        if section.schema_version != 1 or set(section.data) != {
            "enabled",
            "value_targets",
            "branch_targets",
            "presets_omitted",
        }:
            raise ValueError("Unsupported Windows Registry setup section")
        enabled = section.data["enabled"]
        values = section.data["value_targets"]
        branches = section.data["branch_targets"]
        presets_omitted = section.data["presets_omitted"]
        if (
            type(enabled) is not bool
            or not isinstance(values, list)
            or not isinstance(branches, list)
            or type(presets_omitted) is not int
            or presets_omitted < 0
        ):
            raise ValueError("Invalid Windows Registry setup data")
        typed = WindowsRegistryConfiguration.create(
            enabled=enabled,
            value_targets=values,
            branch_targets=branches,
            presets=[],
        )
        return SetupPluginPreview(
            self.identifier,
            self.display_name,
            "supported",
            (
                f"Value targets: {len(typed.value_targets)}",
                f"Branch targets: {len(typed.branch_targets)}",
                f"Presets не включено: {presets_omitted}",
            ),
        )

    def prepare_product_setup_import(
        self,
        product: ProductSetupProduct,
        section: PluginSetupSection,
    ) -> SetupPluginImportPreparation:
        preview = self.preview_product_setup(product, section)
        return SetupPluginImportPreparation(
            self.identifier,
            self.display_name,
            "supported",
            details=preview.details,
        )

    def build_product_setup_configuration(
        self,
        product_id: str,
        product: ProductSetupProduct,
        section: PluginSetupSection,
        adapted_values: dict[str, str],
    ) -> PluginConfiguration:
        self.preview_product_setup(product, section)
        if adapted_values:
            raise ValueError("Windows Registry has no local path adaptations")
        enabled = section.data["enabled"]
        values = section.data["value_targets"]
        branches = section.data["branch_targets"]
        if (
            type(enabled) is not bool
            or not isinstance(values, list)
            or not isinstance(branches, list)
        ):
            raise ValueError("Invalid Windows Registry setup data")
        typed = WindowsRegistryConfiguration.create(
            enabled=enabled,
            value_targets=values,
            branch_targets=branches,
            presets=[],
        )
        return PluginConfiguration(
            product_id,
            self.identifier,
            enabled,
            typed.to_settings(),
        )

    def inspect(
        self,
        configuration: PluginConfiguration | None,
    ) -> RegistryInspectionResult:
        if configuration is None:
            return RegistryInspectionResult(
                warnings=("Windows Registry is not configured for this product.",)
            )
        typed = self.typed_configuration(configuration)
        if typed is None:  # pragma: no cover - handled above
            raise ValueError("Windows Registry configuration is missing")
        if not configuration.enabled:
            return RegistryInspectionResult(
                warnings=("Windows Registry is disabled for this product.",)
            )
        return RegistryInspectionResult(
            values=tuple(
                self._reader.inspect_value(target)
                for target in typed.value_targets
                if target.enabled
            ),
            branches=tuple(
                self._reader.inspect_branch(target)
                for target in typed.branch_targets
                if target.enabled
            ),
        )

    def preview_preset(
        self,
        configuration: PluginConfiguration | None,
        preset_id: str,
    ) -> RegistryChangePlan:
        if configuration is None:
            raise ValueError("Windows Registry is not configured")
        typed = self.typed_configuration(configuration)
        if typed is None:  # pragma: no cover - handled above
            raise ValueError("Windows Registry configuration is missing")
        return self._planner.plan_preset(typed, preset_id)

    def compare_presets(
        self,
        configuration: PluginConfiguration | None,
        inspection: RegistryInspectionResult,
    ) -> dict[str, RegistryChangePlan]:
        """Compare every preset against one request-scoped runtime inspection."""
        typed = self.typed_configuration(configuration)
        if typed is None:  # pragma: no cover - guarded by presentation route
            raise ValueError("Windows Registry configuration is missing")
        return {
            preset.id: self._planner.plan_preset(
                typed,
                preset.id,
                inspection=inspection,
            )
            for preset in typed.presets
        }

    def execute_preset(
        self,
        configuration: PluginConfiguration | None,
        intent: RegistryExecutionIntent,
    ) -> RegistryExecutionResult:
        typed = self.typed_configuration(configuration)
        if typed is None or not typed.enabled:
            raise ValueError("Windows Registry is not enabled")
        return self._executor.execute(typed, intent)

    @staticmethod
    def uses_environment_profile(profile: EnvironmentProfile) -> bool:
        return profile.registry_preset_id is not None

    def inspect_environment_profile_current(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> object:
        del product
        return self.inspect(configuration)

    def compare_environment_profile(
        self,
        profile: EnvironmentProfile,
        configuration: PluginConfiguration | None,
        current: object,
    ) -> EnvironmentProfileComparisonSection | None:
        preset_id = profile.registry_preset_id
        if preset_id is None:
            return None
        typed = self.typed_configuration(configuration)
        if typed is None or not typed.enabled:
            return self._profile_blocked_section(
                preset_id,
                "Windows Registry не налаштовано для цього Product.",
            )
        preset = next((item for item in typed.presets if item.id == preset_id), None)
        if preset is None:
            return self._profile_blocked_section(
                preset_id,
                "Registry preset більше не налаштований.",
            )
        if not isinstance(current, RegistryInspectionResult):
            raise TypeError("Registry Profile current state is invalid")
        plan = self._planner.plan_preset(
            typed,
            preset.id,
            inspection=current,
        )
        statuses = {
            RegistryPlanStatus.READY: EnvironmentProfileComparisonStatus.CHANGE,
            RegistryPlanStatus.NO_CHANGE: (
                EnvironmentProfileComparisonStatus.NO_CHANGE
            ),
            RegistryPlanStatus.BLOCKED: (
                EnvironmentProfileComparisonStatus.BLOCKED
            ),
            RegistryPlanStatus.ERROR: EnvironmentProfileComparisonStatus.ERROR,
        }
        return EnvironmentProfileComparisonSection(
            self.identifier,
            self.display_name,
            f"{preset.name} preset",
            tuple(
                EnvironmentProfileComparisonEntry(
                    entry.target_id,
                    entry.display_name,
                    self._profile_current_state(entry),
                    self._profile_desired_state(entry),
                    statuses[entry.status],
                    entry.message,
                )
                for entry in plan.entries
            ),
        )

    def prepare_environment_profile_execution(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: PluginConfiguration | None,
        current: object,
    ) -> EnvironmentProfileExecutionPreparation:
        del product
        if not isinstance(current, RegistryInspectionResult):
            raise TypeError("Registry Profile current state is invalid")
        section = self.compare_environment_profile(
            profile,
            configuration,
            current,
        )
        if section is None:
            raise ValueError("Profile does not reference a Registry preset")
        typed = self.typed_configuration(configuration)
        preset_id = profile.registry_preset_id
        if typed is None or not typed.enabled or preset_id is None:
            return EnvironmentProfileExecutionPreparation(
                self.identifier,
                self.display_name,
                section.reference_name,
                section.entries,
                "registry-unavailable",
            )
        try:
            plan = self._planner.plan_preset(
                typed, preset_id, inspection=current
            )
        except (KeyError, ValueError):
            return EnvironmentProfileExecutionPreparation(
                self.identifier,
                self.display_name,
                section.reference_name,
                section.entries,
                "registry-preset-missing",
            )
        registry_intent = RegistryExecutionStateStore().create_intent(
            profile.product_id, typed, plan
        )
        return EnvironmentProfileExecutionPreparation(
            self.identifier,
            self.display_name,
            section.reference_name,
            section.entries,
            registry_intent.plan_fingerprint,
            registry_intent,
        )

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
        del profile, product, backup_root, operation_logs, logger
        registry_intent = expected.provider_context
        if not isinstance(registry_intent, RegistryExecutionIntent):
            return EnvironmentProfileProviderExecution(
                self._profile_execution_entries(
                    expected,
                    EnvironmentProfileExecutionStatus.BLOCKED,
                    "Registry preset або configuration недоступні.",
                )
            )
        try:
            result = self.execute_preset(configuration, registry_intent)
        except ValueError:
            return EnvironmentProfileProviderExecution(
                self._profile_execution_entries(
                    expected,
                    EnvironmentProfileExecutionStatus.STALE,
                    "Registry configuration змінилася; запис не виконано.",
                )
            )
        status_map = {
            RegistryExecutionStatus.SUCCEEDED: (
                EnvironmentProfileExecutionStatus.SUCCESS
            ),
            RegistryExecutionStatus.FAILED: EnvironmentProfileExecutionStatus.FAILED,
            RegistryExecutionStatus.STALE: EnvironmentProfileExecutionStatus.STALE,
            RegistryExecutionStatus.BLOCKED: EnvironmentProfileExecutionStatus.BLOCKED,
            RegistryExecutionStatus.UNSUPPORTED: (
                EnvironmentProfileExecutionStatus.BLOCKED
            ),
            RegistryExecutionStatus.NO_CHANGE: (
                EnvironmentProfileExecutionStatus.NO_CHANGE
            ),
            RegistryExecutionStatus.SKIPPED: (
                EnvironmentProfileExecutionStatus.NO_CHANGE
            ),
        }
        return EnvironmentProfileProviderExecution(
            tuple(
                EnvironmentProfileExecutionEntry(
                    entry.target_id,
                    entry.display_name,
                    self._profile_current_state_from_execution(entry),
                    self._profile_desired_state_from_execution(entry),
                    status_map[entry.status],
                    entry.message,
                    changed_count=(
                        1 if entry.status is RegistryExecutionStatus.SUCCEEDED else 0
                    ),
                )
                for entry in result.entries
            ),
            result.warnings,
        )

    @staticmethod
    def _profile_execution_entries(
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

    @staticmethod
    def _profile_current_state_from_execution(
        entry: RegistryExecutionEntryResult,
    ) -> str:
        if isinstance(entry.current_state, RegistryValueState):
            return (
                f"{entry.current_state.registry_type or '—'}: "
                f"{entry.current_state.value}"
            )
        labels = {"visible": "Видима", "hidden": "Прихована"}
        return labels.get(
            entry.current_state.visibility, entry.current_state.visibility
        )

    @staticmethod
    def _profile_desired_state_from_execution(
        entry: RegistryExecutionEntryResult,
    ) -> str:
        if isinstance(entry.desired_state, RegistryPresetValue):
            return (
                f"{entry.desired_state.registry_type.value}: "
                f"{entry.desired_state.value}"
            )
        labels = {"visible": "Видима", "hidden": "Прихована"}
        return labels.get(
            entry.desired_state.visibility.value,
            entry.desired_state.visibility.value,
        )

    @staticmethod
    def _profile_blocked_section(
        preset_id: str,
        message: str,
    ) -> EnvironmentProfileComparisonSection:
        return EnvironmentProfileComparisonSection(
            PLUGIN_IDENTIFIER,
            "Windows Registry",
            preset_id,
            (
                EnvironmentProfileComparisonEntry(
                    preset_id,
                    preset_id,
                    "Налаштування відсутнє",
                    "Registry preset",
                    EnvironmentProfileComparisonStatus.BLOCKED,
                    message,
                ),
            ),
        )

    @staticmethod
    def _profile_current_state(entry: RegistryPlanEntry) -> str:
        current = entry.current_state
        if isinstance(current, RegistryValueState):
            return f"{current.registry_type or '—'}: {current.value}"
        labels = {"visible": "Видима", "hidden": "Прихована"}
        return labels.get(current.visibility, current.visibility)

    @staticmethod
    def _profile_desired_state(entry: RegistryPlanEntry) -> str:
        desired = entry.desired_state
        if isinstance(desired, RegistryPresetValue):
            return f"{desired.registry_type.value}: {desired.value}"
        labels = {"visible": "Видима", "hidden": "Прихована"}
        return labels.get(desired.visibility.value, desired.visibility.value)

    def can_restore(self, resource: SnapshotResource) -> bool:
        return (
            resource.source == self.identifier
            and resource.resource_type in {"registry-value", "registry-branch"}
            and resource.schema_version == 1
        )

    def prepare_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: PluginConfiguration | None,
    ) -> SnapshotRestorePreparation:
        resource = snapshot_resource or current_resource
        if resource is None or not self.can_restore(resource):
            raise ValueError("Unsupported Windows Registry snapshot resource")
        if snapshot_resource is None:
            return self._blocked_restore(
                "Автоматичне видалення Registry ресурсу, якого немає у "
                "snapshot, не підтримується."
            )
        typed = self.typed_configuration(configuration)
        if typed is None or not typed.enabled:
            return self._blocked_restore(
                "Поточна конфігурація Windows Registry недоступна; "
                "автоматичне відновлення неможливе."
            )

        target, desired, blocking_error = self._resolve_restore(
            typed,
            snapshot_resource,
        )
        if blocking_error is not None:
            return self._blocked_restore(blocking_error)
        if current_resource is None:
            return self._blocked_restore(
                "Поточний стан Registry ресурсу не вдалося перевірити; "
                "автоматичне відновлення заблоковано."
            )
        if isinstance(target, RegistryValueTarget) and isinstance(
            desired, RegistryPresetValue
        ):
            inspection = self._snapshot_value_inspection(
                target, current_resource
            )
            entry = self._planner.plan_inspected_value(
                target, desired, inspection
            )
        elif isinstance(target, RegistryBranchTarget) and isinstance(
            desired, RegistryPresetBranch
        ):
            inspection = self._snapshot_branch_inspection(
                target, current_resource
            )
            entry = self._planner.plan_inspected_branch(
                target, desired, inspection
            )
        else:  # pragma: no cover - protected by _resolve_restore
            raise TypeError("Registry restore target and desired state do not match")
        return self._restore_preparation(entry)

    @staticmethod
    def _snapshot_value_inspection(
        target: RegistryValueTarget,
        current: SnapshotResource,
    ) -> RegistryValueInspection:
        raw_status = current.state.get("status")
        if raw_status is None:
            raw_status = (
                RegistryValueStatus.AVAILABLE.value
                if current.state.get("exists") is True
                else RegistryValueStatus.UNAVAILABLE.value
            )
        try:
            status = RegistryValueStatus(raw_status)
        except ValueError as error:
            raise ValueError("Current Registry snapshot status is invalid") from error
        return RegistryValueInspection(
            target,
            current.state.get("exists") is True,
            current.state.get("registry_type")
            if isinstance(current.state.get("registry_type"), str)
            else None,
            current.state.get("value"),
            status,
            "Captured by the request-scoped Current inspection.",
        )

    @staticmethod
    def _snapshot_branch_inspection(
        target: RegistryBranchTarget,
        current: SnapshotResource,
    ) -> RegistryBranchInspection:
        raw_status = current.state.get("status", current.state.get("visibility"))
        try:
            status = RegistryBranchStatus(raw_status)
        except ValueError as error:
            raise ValueError("Current Registry snapshot status is invalid") from error
        existence = {
            RegistryBranchStatus.VISIBLE: (True, False),
            RegistryBranchStatus.HIDDEN: (False, True),
            RegistryBranchStatus.MISSING: (False, False),
            RegistryBranchStatus.CONFLICT: (True, True),
        }.get(status, (None, None))
        return RegistryBranchInspection(
            target,
            existence[0],
            existence[1],
            status,
            "Captured by the request-scoped Current inspection.",
        )

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
        del backup_root
        resource = snapshot_resource or current_resource
        if (
            resource is None
            or snapshot_resource is None
            or current_resource is None
            or not self.can_restore(resource)
        ):
            return SnapshotRestoreExecution(
                SnapshotRestoreExecutionStatus.UNSUPPORTED,
                "Windows Registry cannot execute this restore resource.",
            )
        typed = self.typed_configuration(configuration)
        if typed is None or not typed.enabled:
            return SnapshotRestoreExecution(
                SnapshotRestoreExecutionStatus.STALE,
                "Налаштування Windows Registry змінилися після підготовки плану.",
            )
        target, desired, blocking_error = self._resolve_restore(
            typed,
            snapshot_resource,
        )
        if blocking_error is not None:
            return SnapshotRestoreExecution(
                SnapshotRestoreExecutionStatus.STALE,
                "Поточні налаштування Registry більше не відповідають плану.",
            )
        if isinstance(target, RegistryValueTarget) and isinstance(
            desired, RegistryPresetValue
        ):
            entry = self._planner.plan_value(target, desired)
        elif isinstance(target, RegistryBranchTarget) and isinstance(
            desired, RegistryPresetBranch
        ):
            entry = self._planner.plan_branch(target, desired)
        else:  # pragma: no cover - protected by _resolve_restore
            raise TypeError("Registry restore target and desired state do not match")
        result = self._executor.execute_entry(
            target,
            entry,
            expected_fingerprint,
        )
        execution = self._snapshot_execution(result)
        log_warning = self._append_restore_log(
            product,
            resource,
            execution,
            operation_logs,
            logger,
        )
        if log_warning is None:
            return execution
        return SnapshotRestoreExecution(
            status=execution.status,
            message=execution.message,
            changed_count=execution.changed_count,
            rollback_status=execution.rollback_status,
            warnings=(*execution.warnings, log_warning),
        )

    def _resolve_restore(
        self,
        configuration: WindowsRegistryConfiguration,
        snapshot_resource: SnapshotResource,
    ) -> tuple[
        RegistryValueTarget | RegistryBranchTarget | None,
        RegistryPresetValue | RegistryPresetBranch | None,
        str | None,
    ]:
        if snapshot_resource.resource_type == "registry-value":
            target = next(
                (
                    item
                    for item in configuration.value_targets
                    if item.id == snapshot_resource.identifier and item.enabled
                ),
                None,
            )
            if target is None:
                return None, None, (
                    "Ресурс зі snapshot не вдалося безпечно зіставити з "
                    "поточною конфігурацією Windows Registry."
                )
            if not self._value_identity_matches(target, snapshot_resource):
                return None, None, (
                    "Налаштування ресурсу реєстру змінилися після створення Snapshot."
                )
            if snapshot_resource.state.get("exists") is not True:
                return None, None, (
                    "Snapshot Registry value state недоступний для restore preview."
                )
            try:
                desired = RegistryPresetValue.from_dict(
                    {
                        "target_id": target.id,
                        "registry_type": snapshot_resource.state.get(
                            "registry_type"
                        ),
                        "value": snapshot_resource.state.get("value"),
                    }
                )
            except ValueError:
                return None, None, (
                    "Snapshot не містить підтримуваний typed Registry value state."
                )
            return target, desired, None

        target = next(
            (
                item
                for item in configuration.branch_targets
                if item.id == snapshot_resource.identifier and item.enabled
            ),
            None,
        )
        if target is None:
            return None, None, (
                "Ресурс зі snapshot не вдалося безпечно зіставити з поточною "
                "конфігурацією Windows Registry."
            )
        if not self._branch_identity_matches(target, snapshot_resource):
            return None, None, (
                "Налаштування ресурсу реєстру змінилися після створення Snapshot."
            )
        try:
            desired = RegistryPresetBranch.from_dict(
                {
                    "target_id": target.id,
                    "visibility": snapshot_resource.state.get("visibility"),
                }
            )
        except ValueError:
            return None, None, (
                "Snapshot не містить actionable Registry branch visibility."
            )
        return target, desired, None

    @staticmethod
    def _snapshot_execution(
        result: RegistryExecutionEntryResult,
    ) -> SnapshotRestoreExecution:
        statuses = {
            RegistryExecutionStatus.SUCCEEDED: SnapshotRestoreExecutionStatus.SUCCESS,
            RegistryExecutionStatus.FAILED: SnapshotRestoreExecutionStatus.FAILED,
            RegistryExecutionStatus.STALE: SnapshotRestoreExecutionStatus.STALE,
            RegistryExecutionStatus.BLOCKED: SnapshotRestoreExecutionStatus.STALE,
            RegistryExecutionStatus.UNSUPPORTED: (
                SnapshotRestoreExecutionStatus.UNSUPPORTED
            ),
            RegistryExecutionStatus.NO_CHANGE: SnapshotRestoreExecutionStatus.SKIPPED,
            RegistryExecutionStatus.SKIPPED: SnapshotRestoreExecutionStatus.SKIPPED,
        }
        rollback = {
            RegistryRollbackStatus.NOT_REQUIRED: RollbackStatus.NOT_REQUIRED,
            RegistryRollbackStatus.SUCCEEDED: RollbackStatus.COMPLETE,
            RegistryRollbackStatus.FAILED: RollbackStatus.PARTIAL,
        }[result.rollback_status]
        return SnapshotRestoreExecution(
            status=statuses[result.status],
            message=result.message,
            changed_count=(
                1 if result.status is RegistryExecutionStatus.SUCCEEDED else 0
            ),
            rollback_status=rollback,
        )

    @staticmethod
    def _append_restore_log(
        product: Product,
        resource: SnapshotResource,
        execution: SnapshotRestoreExecution,
        operation_logs: OperationLogRepository,
        logger: Logger | None,
    ) -> str | None:
        try:
            operation_logs.append(
                OperationLog(
                    id=str(uuid4()),
                    timestamp=datetime.now(UTC),
                    product_id=product.id,
                    plugin_identifier=PLUGIN_IDENTIFIER,
                    action_identifier="restore-snapshot-resource",
                    status={
                        SnapshotRestoreExecutionStatus.SUCCESS: (
                            OperationStatus.SUCCESS
                        ),
                        SnapshotRestoreExecutionStatus.FAILED: (
                            OperationStatus.FAILED
                        ),
                        SnapshotRestoreExecutionStatus.STALE: (
                            OperationStatus.REJECTED
                        ),
                        SnapshotRestoreExecutionStatus.SKIPPED: (
                            OperationStatus.NO_CHANGES
                        ),
                        SnapshotRestoreExecutionStatus.UNSUPPORTED: (
                            OperationStatus.NO_CHANGES
                        ),
                    }[execution.status],
                    summary=(
                        "Registry Snapshot Restore: "
                        f"{resource.resource_type} {resource.identifier} "
                        f"{execution.status.value}."
                    ),
                    changed_count=execution.changed_count,
                    skipped_count=(
                        1
                        if execution.status
                        in {
                            SnapshotRestoreExecutionStatus.SKIPPED,
                            SnapshotRestoreExecutionStatus.UNSUPPORTED,
                        }
                        else 0
                    ),
                    error_count=(
                        1
                        if execution.status
                        in {
                            SnapshotRestoreExecutionStatus.FAILED,
                            SnapshotRestoreExecutionStatus.STALE,
                        }
                        else 0
                    ),
                    rollback_status=execution.rollback_status,
                )
            )
        except Exception:
            if logger is not None:
                logger.exception("Could not append Registry Snapshot Restore log")
            return "Registry Restore completed, but its operation log was not saved."
        return None

    @staticmethod
    def _restore_preparation(entry: RegistryPlanEntry) -> SnapshotRestorePreparation:
        blocking_error = (
            entry.message
            if entry.status in {RegistryPlanStatus.BLOCKED, RegistryPlanStatus.ERROR}
            else None
        )
        return SnapshotRestorePreparation(
            action_description=WindowsRegistry._entry_description(entry),
            risk_level=entry.risk_level,
            changes_required=entry.status is not RegistryPlanStatus.NO_CHANGE,
            fingerprint=entry.expected_fingerprint,
            blocking_error=blocking_error,
        )

    @staticmethod
    def _blocked_restore(message: str) -> SnapshotRestorePreparation:
        return SnapshotRestorePreparation(
            action_description=message,
            risk_level=RiskLevel.CAUTION,
            blocking_error=message,
        )

    @staticmethod
    def _entry_description(entry: RegistryPlanEntry) -> str:
        current = entry.current_state
        desired = entry.desired_state
        if isinstance(current, RegistryValueState) and isinstance(
            desired, RegistryPresetValue
        ):
            return (
                f"{entry.display_name}: {current.registry_type or 'missing'} "
                f"{current.value!r} -> {desired.registry_type.value} "
                f"{desired.value!r}"
            )
        if isinstance(current, RegistryBranchState) and isinstance(
            desired, RegistryPresetBranch
        ):
            labels = {"visible": "Видима", "hidden": "Прихована"}
            return (
                f"{entry.display_name}: "
                f"{labels.get(current.visibility, current.visibility)} -> "
                f"{labels.get(desired.visibility.value, desired.visibility.value)}"
            )
        return entry.message

    @staticmethod
    def _value_identity_matches(
        target: RegistryValueTarget,
        resource: SnapshotResource,
    ) -> bool:
        return (
            resource.state.get("hive") == target.hive.value
            and resource.state.get("key_path") == target.key_path
            and resource.state.get("value_name") == target.value_name
        )

    @staticmethod
    def _branch_identity_matches(
        target: RegistryBranchTarget,
        resource: SnapshotResource,
    ) -> bool:
        return (
            resource.state.get("hive") == target.hive.value
            and resource.state.get("key_path") == target.key_path
            and resource.state.get("hidden_name") == target.hidden_name
        )

    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        if configuration is None or not configuration.enabled:
            return SnapshotCaptureResult()
        result = self.inspect(configuration)
        resources: list[SnapshotResource] = []
        for inspection in result.values:
            resources.append(
                SnapshotResource(
                    source=self.identifier,
                    resource_type="registry-value",
                    identifier=inspection.target.id,
                    state={
                        "hive": inspection.target.hive.value,
                        "key_path": inspection.target.key_path,
                        "value_name": inspection.target.value_name,
                        "exists": inspection.exists,
                        "registry_type": inspection.registry_type,
                        "value": inspection.value,
                        "status": inspection.status.value,
                    },
                )
            )
        for inspection in result.branches:
            resources.append(
                SnapshotResource(
                    source=self.identifier,
                    resource_type="registry-branch",
                    identifier=inspection.target.id,
                    state={
                        "hive": inspection.target.hive.value,
                        "key_path": inspection.target.key_path,
                        "hidden_name": inspection.target.hidden_name,
                        "visibility": inspection.status.value,
                        "status": inspection.status.value,
                    },
                )
            )
        warnings = list(result.warnings)
        warnings.extend(
            inspection.message
            for inspection in result.values
            if inspection.status
            in {RegistryValueStatus.UNAVAILABLE, RegistryValueStatus.ERROR}
        )
        warnings.extend(
            inspection.message
            for inspection in result.branches
            if inspection.status
            in {RegistryBranchStatus.UNAVAILABLE, RegistryBranchStatus.ERROR}
        )
        return SnapshotCaptureResult(tuple(resources), tuple(warnings))

    @staticmethod
    def _json_array(value: str, name: str) -> list[object]:
        text = value.strip() or "[]"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"{name} must contain valid JSON") from error
        if not isinstance(parsed, list):
            raise ValueError(f"{name} must be a JSON array")
        return parsed


def create_windows_registry() -> Plugin:
    return WindowsRegistry()
