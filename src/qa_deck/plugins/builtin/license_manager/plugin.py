"""Plugin facade for License Manager."""

from logging import Logger
from pathlib import Path

from qa_deck.domain import PluginConfiguration
from qa_deck.plugins import Plugin, PluginAction, RiskLevel
from qa_deck.plugins.builtin.license_manager.models import (
    BackupInspectionResult,
    ChangePlan,
    LicenseInspectionResult,
    LicenseManagerConfiguration,
    LicenseOperationResult,
)
from qa_deck.plugins.builtin.license_manager.service import (
    HIDE_ACTION,
    RESTORE_ACTION,
    LicenseManagerService,
)
from qa_deck.storage import OperationLogRepository


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
    ) -> ChangePlan:
        return self._service.build_plan(
            product_id,
            configuration,
            action_identifier,
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


def create_license_manager() -> Plugin:
    return LicenseManager()
