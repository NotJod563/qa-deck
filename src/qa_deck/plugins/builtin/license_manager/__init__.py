"""Built-in License Manager plugin."""

from qa_deck.plugins.builtin.license_manager.models import (
    BackupInspectionResult,
    ChangePlan,
    LicenseFileState,
    LicenseFileStatus,
    LicenseInspectionResult,
    LicenseInspectionStatus,
    LicenseManagerConfiguration,
    LicenseOperationResult,
    PlannedChange,
    SkippedChange,
)
from qa_deck.plugins.builtin.license_manager.plugin import (
    LicenseManager,
    create_license_manager,
)

__all__ = [
    "BackupInspectionResult",
    "ChangePlan",
    "LicenseFileState",
    "LicenseFileStatus",
    "LicenseInspectionResult",
    "LicenseInspectionStatus",
    "LicenseManager",
    "LicenseManagerConfiguration",
    "LicenseOperationResult",
    "PlannedChange",
    "SkippedChange",
    "create_license_manager",
]
