"""Plugins bundled with QA Deck."""

from qa_deck.plugins.builtin.executable_inspector import (
    ExecutableInspectionResult,
    ExecutableInspectionStatus,
    ExecutableInspector,
    create_executable_inspector,
)
from qa_deck.plugins.builtin.license_manager import (
    LicenseManager,
    create_license_manager,
)
from qa_deck.plugins.builtin.log_collector import (
    LogCollector,
    create_log_collector,
)

__all__ = [
    "ExecutableInspectionResult",
    "ExecutableInspectionStatus",
    "ExecutableInspector",
    "LicenseManager",
    "LogCollector",
    "create_executable_inspector",
    "create_license_manager",
    "create_log_collector",
]
