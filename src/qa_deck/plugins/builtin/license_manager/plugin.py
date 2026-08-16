"""Plugin facade for License Manager."""

import hashlib
import json
from dataclasses import dataclass
from logging import Logger
from pathlib import Path

from qa_deck.domain import (
    EnvironmentProfile,
    OperationStatus,
    PluginConfiguration,
    PluginSetupSection,
    PortablePath,
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
from qa_deck.plugins.builtin.license_manager.models import (
    BackupInspectionResult,
    ChangePlan,
    LicenseFileState,
    LicenseFileStatus,
    LicenseInspectionResult,
    LicenseInspectionStatus,
    LicenseManagerConfiguration,
    LicenseOperationResult,
)
from qa_deck.plugins.builtin.license_manager.service import (
    HIDE_ACTION,
    RESTORE_ACTION,
    LicenseManagerService,
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


@dataclass(frozen=True, slots=True)
class _ProfileLicenseExpectation:
    resource_id: str
    action_identifier: str
    fingerprint: str
    preview_status: EnvironmentProfileComparisonStatus


class LicenseManager:
    identifier = "license-manager"
    display_name = "License Manager"
    description = "Перевіряє та безпечно приховує ліцензійні файли продукту."
    version = "0.1.0"

    def __init__(self) -> None:
        self._service = LicenseManagerService()

    def get_actions(self) -> list[PluginAction]:
        return [
            PluginAction(
                "inspect-licenses",
                "Перевірити ліцензії",
                "Показати поточний стан ліцензійних файлів.",
                RiskLevel.SAFE,
            ),
            PluginAction(
                HIDE_ACTION,
                "Приховати ліцензії",
                "Створити backup і тимчасово приховати активні файли.",
                RiskLevel.CAUTION,
            ),
            PluginAction(
                RESTORE_ACTION,
                "Відновити ліцензії",
                "Створити backup і повернути приховані файли.",
                RiskLevel.CAUTION,
            ),
            PluginAction(
                "inspect-license-backup",
                "Перевірити backup",
                "Показати інформацію про останній backup.",
                RiskLevel.SAFE,
            ),
        ]

    def create_configuration(
        self,
        *,
        product_id: str,
        enabled: bool,
        license_directory: str,
        license_files_text: str,
    ) -> PluginConfiguration:
        typed = LicenseManagerConfiguration.create(
            license_directory,
            license_files_text.splitlines(),
            enabled=enabled,
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
    ) -> LicenseManagerConfiguration | None:
        if configuration is None:
            return None
        return LicenseManagerConfiguration.from_plugin_configuration(configuration)

    def export_product_setup(
        self,
        product: ProductSetupProduct,
        configuration: PluginConfiguration,
    ) -> PluginSetupSection:
        typed = self.typed_configuration(configuration)
        if typed is None:  # pragma: no cover
            raise ValueError("License Manager configuration is missing")
        directory = make_portable_path(
            typed.license_directory, product.install_directory_hint
        )
        return PluginSetupSection(
            self.identifier,
            1,
            {
                "enabled": configuration.enabled,
                "license_directory": directory.to_dict() if directory else None,
                "license_files": list(typed.license_files),
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
            "license_directory",
            "license_files",
        }:
            raise ValueError("Unsupported License Manager setup section")
        enabled = section.data["enabled"]
        raw_directory = section.data["license_directory"]
        raw_files = section.data["license_files"]
        if type(enabled) is not bool or not isinstance(raw_files, list):
            raise ValueError("Invalid License Manager setup data")
        directory = (
            PortablePath.from_dict(raw_directory)
            if raw_directory is not None
            else None
        )
        LicenseManagerConfiguration.create(
            directory.original if directory else "", raw_files, enabled=enabled
        )
        path = preview_path("Каталог ліцензій", directory)
        return SetupPluginPreview(
            self.identifier,
            self.display_name,
            "supported",
            (f"Ліцензійних файлів: {len(raw_files)}",),
            (path,) if path else (),
        )

    def prepare_product_setup_import(
        self,
        product: ProductSetupProduct,
        section: PluginSetupSection,
    ) -> SetupPluginImportPreparation:
        preview = self.preview_product_setup(product, section)
        raw_directory = section.data["license_directory"]
        directory = (
            PortablePath.from_dict(raw_directory)
            if raw_directory is not None
            else None
        )
        return SetupPluginImportPreparation(
            self.identifier,
            self.display_name,
            "supported",
            (
                import_path_field(
                    "license_directory",
                    "Каталог ліцензій",
                    directory.original if directory else "",
                ),
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
        if set(adapted_values) != {"license_directory"}:
            raise ValueError("License Manager adaptation fields are invalid")
        enabled = section.data["enabled"]
        raw_files = section.data["license_files"]
        if type(enabled) is not bool or not isinstance(raw_files, list):
            raise ValueError("Invalid License Manager setup data")
        return self.create_configuration(
            product_id=product_id,
            enabled=enabled,
            license_directory=validate_import_path(
                adapted_values["license_directory"], "Каталог ліцензій"
            ),
            license_files_text="\n".join(str(item) for item in raw_files),
        )

    def inspect(
        self,
        configuration: PluginConfiguration | None,
    ) -> LicenseInspectionResult:
        return self._service.inspect(configuration)

    def build_plan(
        self,
        product_id: str,
        configuration: PluginConfiguration | None,
        action_identifier: str,
        *,
        inspection: LicenseInspectionResult | None = None,
    ) -> ChangePlan:
        return self._service.build_plan(
            product_id,
            configuration,
            action_identifier,
            inspection=inspection,
        )

    def execute(
        self,
        *,
        product_id: str,
        configuration: PluginConfiguration | None,
        action_identifier: str,
        expected_fingerprint: str,
        confirmed: bool,
        backup_root: str | Path,
        operation_logs: OperationLogRepository,
        logger: Logger | None = None,
    ) -> LicenseOperationResult:
        return self._service.execute(
            product_id=product_id,
            configuration=configuration,
            action_identifier=action_identifier,
            expected_fingerprint=expected_fingerprint,
            confirmed=confirmed,
            backup_root=backup_root,
            operation_logs=operation_logs,
            logger=logger,
        )

    def inspect_backup(
        self,
        product_id: str,
        backup_root: str | Path,
    ) -> BackupInspectionResult:
        return self._service.inspect_backup(product_id, backup_root)

    @staticmethod
    def uses_environment_profile(profile: EnvironmentProfile) -> bool:
        return bool(profile.license_states)

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
        if not profile.license_states:
            return None
        typed = self.typed_configuration(configuration)
        if not isinstance(current, LicenseInspectionResult):
            raise TypeError("License Profile current state is invalid")
        configured = set(typed.license_files) if typed is not None else set()
        current_files = {item.filename: item for item in current.files}
        labels = {
            LicenseFileStatus.ACTIVE: "Активна",
            LicenseFileStatus.HIDDEN: "Прихована",
            LicenseFileStatus.MISSING: "Відсутня",
            LicenseFileStatus.CONFLICT: "Конфлікт",
            LicenseFileStatus.ERROR: "Помилка",
        }
        entries: list[EnvironmentProfileComparisonEntry] = []
        for desired in profile.license_states:
            if desired.resource_id not in configured:
                entries.append(
                    EnvironmentProfileComparisonEntry(
                        desired.resource_id,
                        desired.resource_id,
                        "Налаштування відсутнє",
                        (
                            "Активна"
                            if desired.desired_state.value == "active"
                            else "Прихована"
                        ),
                        EnvironmentProfileComparisonStatus.BLOCKED,
                        "Ліцензійний ресурс більше не налаштований.",
                    )
                )
                continue
            inspected = current_files.get(desired.resource_id)
            desired_label = (
                "Активна"
                if desired.desired_state.value == "active"
                else "Прихована"
            )
            if inspected is None:
                entries.append(
                    EnvironmentProfileComparisonEntry(
                        desired.resource_id,
                        desired.resource_id,
                        "Недоступно",
                        desired_label,
                        EnvironmentProfileComparisonStatus.ERROR,
                        "Не вдалося перевірити ліцензійний ресурс.",
                    )
                )
                continue
            actionable = inspected.status in {
                LicenseFileStatus.ACTIVE,
                LicenseFileStatus.HIDDEN,
            }
            matches = inspected.status.value == desired.desired_state.value
            entries.append(
                EnvironmentProfileComparisonEntry(
                    desired.resource_id,
                    desired.resource_id,
                    labels[inspected.status],
                    desired_label,
                    (
                        EnvironmentProfileComparisonStatus.NO_CHANGE
                        if matches
                        else EnvironmentProfileComparisonStatus.CHANGE
                        if actionable
                        else EnvironmentProfileComparisonStatus.BLOCKED
                    ),
                    (
                        "Поточний стан відповідає profile."
                        if matches
                        else "Ліцензійний стан потребує зміни."
                        if actionable
                        else "Поточний стан не можна безпечно змінити."
                    ),
                )
            )
        return EnvironmentProfileComparisonSection(
            self.identifier,
            self.display_name,
            None,
            tuple(entries),
        )

    def prepare_environment_profile_execution(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: PluginConfiguration | None,
        current: object,
    ) -> EnvironmentProfileExecutionPreparation:
        if not isinstance(current, LicenseInspectionResult):
            raise TypeError("License Profile current state is invalid")
        inspection = current
        section = self.compare_environment_profile(profile, configuration, inspection)
        if section is None:
            raise ValueError("Profile has no License Manager resources")
        comparisons = {entry.resource_id: entry for entry in section.entries}
        expectations: list[_ProfileLicenseExpectation] = []
        for desired in profile.license_states:
            comparison = comparisons[desired.resource_id]
            action = self._restore_action(desired.desired_state.value)
            try:
                scoped = self._scoped_configuration(
                    configuration, desired.resource_id
                )
                plan = self.build_plan(
                    product.id,
                    scoped,
                    action,
                    inspection=self._scoped_inspection(
                        inspection, desired.resource_id
                    ),
                )
                fingerprint = plan.fingerprint
            except (TypeError, ValueError):
                fingerprint = "license-resource-unavailable"
            expectations.append(
                _ProfileLicenseExpectation(
                    desired.resource_id,
                    action,
                    fingerprint,
                    comparison.status,
                )
            )
        authority = self._profile_license_authority(configuration, profile)
        return EnvironmentProfileExecutionPreparation(
            self.identifier,
            self.display_name,
            None,
            section.entries,
            authority,
            tuple(expectations),
        )

    @staticmethod
    def _scoped_inspection(
        inspection: LicenseInspectionResult,
        resource_id: str,
    ) -> LicenseInspectionResult:
        return LicenseInspectionResult(
            inspection.status,
            inspection.directory,
            tuple(
                item for item in inspection.files if item.filename == resource_id
            ),
            inspection.message,
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
        context = expected.provider_context
        if not isinstance(context, tuple) or not all(
            isinstance(item, _ProfileLicenseExpectation) for item in context
        ):
            raise TypeError("License Profile execution context is invalid")
        if self._profile_license_authority(configuration, profile) != (
            expected.authority_fingerprint
        ):
            return EnvironmentProfileProviderExecution(
                tuple(
                    self._profile_license_result(
                        entry,
                        EnvironmentProfileExecutionStatus.STALE,
                        "License configuration змінилася; old path не використано.",
                    )
                    for entry in expected.entries
                )
            )
        expected_entries = {entry.resource_id: entry for entry in expected.entries}
        results: list[EnvironmentProfileExecutionEntry] = []
        for item in context:
            preview = expected_entries[item.resource_id]
            try:
                scoped = self._scoped_configuration(configuration, item.resource_id)
                current_plan = self.build_plan(
                    product.id, scoped, item.action_identifier
                )
            except (TypeError, ValueError):
                results.append(
                    self._profile_license_result(
                        preview,
                        EnvironmentProfileExecutionStatus.STALE,
                        "Ліцензійний ресурс більше не налаштований.",
                    )
                )
                continue
            if current_plan.fingerprint != item.fingerprint:
                results.append(
                    self._profile_license_result(
                        preview,
                        EnvironmentProfileExecutionStatus.STALE,
                        "Поточний стан ліцензії змінився після preview.",
                    )
                )
                continue
            if item.preview_status is EnvironmentProfileComparisonStatus.NO_CHANGE:
                results.append(
                    self._profile_license_result(
                        preview,
                        EnvironmentProfileExecutionStatus.NO_CHANGE,
                        "Поточний стан уже відповідає profile.",
                    )
                )
                continue
            if item.preview_status is not EnvironmentProfileComparisonStatus.CHANGE:
                results.append(
                    self._profile_license_result(
                        preview,
                        EnvironmentProfileExecutionStatus.BLOCKED,
                        preview.message,
                    )
                )
                continue
            operation = self.execute(
                product_id=product.id,
                configuration=scoped,
                action_identifier=item.action_identifier,
                expected_fingerprint=item.fingerprint,
                confirmed=True,
                backup_root=backup_root,
                operation_logs=operation_logs,
                logger=logger,
            )
            if operation.stale_plan:
                status = EnvironmentProfileExecutionStatus.STALE
            elif operation.status is OperationStatus.SUCCESS:
                status = EnvironmentProfileExecutionStatus.SUCCESS
            elif operation.status is OperationStatus.NO_CHANGES:
                status = EnvironmentProfileExecutionStatus.NO_CHANGE
            elif operation.status is OperationStatus.BLOCKED:
                status = EnvironmentProfileExecutionStatus.BLOCKED
            else:
                status = EnvironmentProfileExecutionStatus.FAILED
            results.append(
                self._profile_license_result(
                    preview,
                    status,
                    operation.summary,
                    changed_count=operation.changed_count,
                    rollback_status=operation.rollback_status,
                    backup_created=operation.backup_created,
                    warnings=operation.warnings,
                )
            )
        return EnvironmentProfileProviderExecution(tuple(results))

    @staticmethod
    def _profile_license_result(
        preview: EnvironmentProfileComparisonEntry,
        status: EnvironmentProfileExecutionStatus,
        message: str,
        *,
        changed_count: int = 0,
        rollback_status: RollbackStatus | None = None,
        backup_created: bool = False,
        warnings: tuple[str, ...] = (),
    ) -> EnvironmentProfileExecutionEntry:
        return EnvironmentProfileExecutionEntry(
            preview.resource_id,
            preview.display_name,
            preview.current_state,
            preview.desired_state,
            status,
            message,
            changed_count,
            rollback_status,
            backup_created,
            warnings,
        )

    def _profile_license_authority(
        self,
        configuration: PluginConfiguration | None,
        profile: EnvironmentProfile,
    ) -> str:
        try:
            typed = self.typed_configuration(configuration)
            config_payload: object = (
                None
                if typed is None
                else {
                    "enabled": configuration.enabled if configuration else False,
                    "directory": typed.license_directory,
                    "files": typed.license_files,
                }
            )
        except ValueError:
            config_payload = "invalid"
        payload = {
            "configuration": config_payload,
            "desired": [
                (item.resource_id, item.desired_state.value)
                for item in profile.license_states
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        if configuration is None or not configuration.enabled:
            return SnapshotCaptureResult(
                warnings=(
                    "License Manager snapshot provider is disabled or not configured.",
                ),
            )

        inspection = self.inspect(configuration)
        if inspection.status == LicenseInspectionStatus.READY and inspection.files:
            resources: list[SnapshotResource] = []
            for file_state in inspection.files:
                resources.append(
                    SnapshotResource(
                        source=self.identifier,
                        resource_type="license",
                        identifier=file_state.filename,
                        state={
                            "status": file_state.status.value,
                            "message": file_state.message,
                            "original_size": file_state.original_size,
                            "original_mtime_ns": file_state.original_mtime_ns,
                            "hidden_size": file_state.hidden_size,
                            "hidden_mtime_ns": file_state.hidden_mtime_ns,
                        },
                    )
                )
            return SnapshotCaptureResult(resources=tuple(resources))

        warnings = (
            f"License Manager inspection returned {inspection.status.value}.",
        )

        return SnapshotCaptureResult(
            resources=(
                SnapshotResource(
                    source=self.identifier,
                    resource_type="license-manager",
                    identifier="license-manager",
                    state={
                        "status": inspection.status.value,
                        "directory": inspection.directory,
                        "message": inspection.message,
                    },
                ),
            ),
            warnings=warnings,
        )

    def can_restore(self, resource: SnapshotResource) -> bool:
        return (
            resource.source == self.identifier
            and resource.resource_type == "license"
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
            raise ValueError("Unsupported License Manager snapshot resource")
        if snapshot_resource is None:
            return SnapshotRestorePreparation(
                action_description=(
                    "License Manager не може безпечно визначити видалення "
                    "ресурсу, якого немає у snapshot."
                ),
                risk_level=RiskLevel.CAUTION,
                blocking_error=(
                    "Автоматичне видалення ліцензійного ресурсу не підтримується."
                ),
            )

        if configuration is None or not configuration.enabled:
            return SnapshotRestorePreparation(
                action_description=f"{resource.identifier}: restore недоступний",
                risk_level=RiskLevel.CAUTION,
                blocking_error=(
                    "Поточна конфігурація каталогу ліцензій недоступна; "
                    "автоматичне відновлення неможливе."
                ),
            )
        try:
            typed_configuration = self.typed_configuration(configuration)
        except ValueError:
            return SnapshotRestorePreparation(
                action_description=f"{resource.identifier}: restore недоступний",
                risk_level=RiskLevel.CAUTION,
                blocking_error=(
                    "Поточна конфігурація License Manager недійсна; "
                    "автоматичне відновлення неможливе."
                ),
            )
        if (
            typed_configuration is None
            or resource.identifier not in typed_configuration.license_files
        ):
            return SnapshotRestorePreparation(
                action_description=f"{resource.identifier}: restore недоступний",
                risk_level=RiskLevel.CAUTION,
                blocking_error=(
                    "Ліцензійний ресурс зі snapshot більше не налаштований "
                    "для цього Product."
                ),
            )
        if current_resource is None:
            return SnapshotRestorePreparation(
                action_description=f"{resource.identifier}: restore недоступний",
                risk_level=RiskLevel.CAUTION,
                blocking_error=(
                    "Поточний стан ліцензійного ресурсу не вдалося перевірити; "
                    "автоматичне відновлення заблоковано."
                ),
            )

        desired_status = snapshot_resource.state.get("status")
        current_status = current_resource.state.get("status")
        if desired_status not in {
            LicenseFileStatus.ACTIVE.value,
            LicenseFileStatus.HIDDEN.value,
        }:
            return SnapshotRestorePreparation(
                action_description=(
                    f"{resource.identifier}: the desired license state "
                    "cannot be restored automatically."
                ),
                risk_level=RiskLevel.CAUTION,
                blocking_error="The desired license state is not actionable.",
            )
        if current_status == desired_status:
            return SnapshotRestorePreparation(
                action_description=(
                    f"{resource.identifier}: {current_status} already matches "
                    "the actionable snapshot state."
                ),
                risk_level=RiskLevel.SAFE,
                changes_required=False,
            )

        description = (
            f"{resource.identifier}: {current_status or 'absent'} "
            f"-> {desired_status}"
        )
        action_identifier = self._restore_action(desired_status)
        scoped_configuration = self._scoped_configuration(
            configuration,
            resource.identifier,
        )
        change_plan = self.build_plan(
            product.id,
            scoped_configuration,
            action_identifier,
            inspection=self._snapshot_license_inspection(
                current_resource, scoped_configuration
            ),
        )
        if change_plan.blocking_error is not None:
            return SnapshotRestorePreparation(
                action_description=description,
                risk_level=change_plan.risk_level,
                fingerprint=change_plan.fingerprint,
                warnings=change_plan.warnings,
                blocking_error=change_plan.blocking_error,
            )

        expected_change = any(
            change.source_name == resource.identifier
            or change.target_name == resource.identifier
            for change in change_plan.changes
        )
        if not expected_change:
            return SnapshotRestorePreparation(
                action_description=description,
                risk_level=change_plan.risk_level,
                fingerprint=change_plan.fingerprint,
                warnings=change_plan.warnings,
                blocking_error=(
                    "Current state no longer supports the requested transition."
                ),
            )
        return SnapshotRestorePreparation(
            action_description=description,
            risk_level=change_plan.risk_level,
            fingerprint=change_plan.fingerprint,
            warnings=change_plan.warnings,
        )

    def _snapshot_license_inspection(
        self,
        current: SnapshotResource,
        configuration: PluginConfiguration | None,
    ) -> LicenseInspectionResult:
        typed = self.typed_configuration(configuration)
        if typed is None:
            raise ValueError("License Manager configuration is missing")
        try:
            status = LicenseFileStatus(current.state.get("status"))
        except ValueError as error:
            raise ValueError("Current license snapshot status is invalid") from error

        def optional_int(name: str) -> int | None:
            value = current.state.get(name)
            if value is None or type(value) is int:
                return value
            raise ValueError("Current license snapshot metadata is invalid")

        state = LicenseFileState(
            current.identifier,
            status,
            str(current.state.get("message", "Captured Current state.")),
            optional_int("original_size"),
            optional_int("original_mtime_ns"),
            optional_int("hidden_size"),
            optional_int("hidden_mtime_ns"),
        )
        return LicenseInspectionResult(
            LicenseInspectionStatus.READY,
            typed.license_directory,
            (state,),
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
        resource = snapshot_resource or current_resource
        if (
            resource is None
            or snapshot_resource is None
            or current_resource is None
            or not self.can_restore(resource)
        ):
            return SnapshotRestoreExecution(
                SnapshotRestoreExecutionStatus.UNSUPPORTED,
                "License Manager cannot execute this restore resource.",
            )
        desired_status = snapshot_resource.state.get("status")
        if desired_status not in {
            LicenseFileStatus.ACTIVE.value,
            LicenseFileStatus.HIDDEN.value,
        }:
            return SnapshotRestoreExecution(
                SnapshotRestoreExecutionStatus.UNSUPPORTED,
                "The desired license state is not actionable.",
            )

        scoped_configuration = self._scoped_configuration(
            configuration,
            resource.identifier,
        )
        operation = self.execute(
            product_id=product.id,
            configuration=scoped_configuration,
            action_identifier=self._restore_action(desired_status),
            expected_fingerprint=expected_fingerprint,
            confirmed=True,
            backup_root=backup_root,
            operation_logs=operation_logs,
            logger=logger,
        )
        if operation.stale_plan:
            status = SnapshotRestoreExecutionStatus.STALE
        elif operation.status is OperationStatus.SUCCESS:
            status = SnapshotRestoreExecutionStatus.SUCCESS
        elif operation.status is OperationStatus.NO_CHANGES:
            status = SnapshotRestoreExecutionStatus.SKIPPED
        else:
            status = SnapshotRestoreExecutionStatus.FAILED
        return SnapshotRestoreExecution(
            status=status,
            message=operation.summary,
            changed_count=operation.changed_count,
            rollback_status=operation.rollback_status,
            backup_created=operation.backup_created,
            warnings=operation.warnings,
        )

    @staticmethod
    def _restore_action(desired_status: object) -> str:
        return (
            HIDE_ACTION
            if desired_status == LicenseFileStatus.HIDDEN.value
            else RESTORE_ACTION
        )

    def _scoped_configuration(
        self,
        configuration: PluginConfiguration | None,
        identifier: str,
    ) -> PluginConfiguration | None:
        if configuration is None:
            return None
        typed = LicenseManagerConfiguration.from_plugin_configuration(configuration)
        if identifier not in typed.license_files:
            raise ValueError("License resource is not in the current configuration")
        return PluginConfiguration(
            product_id=configuration.product_id,
            plugin_identifier=configuration.plugin_identifier,
            enabled=configuration.enabled,
            settings={
                "license_directory": typed.license_directory,
                "license_files": [identifier],
            },
        )


def create_license_manager() -> Plugin:
    return LicenseManager()
