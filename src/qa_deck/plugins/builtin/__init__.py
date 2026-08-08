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
from qa_deck.plugins.builtin.windows_registry import (
    WindowsRegistry,
    create_windows_registry,
)

__all__ = [
    "ExecutableInspectionResult",
    "ExecutableInspectionStatus",
    "ExecutableInspector",
    "LicenseManager",
    "LogCollector",
    "WindowsRegistry",
    "create_executable_inspector",
    "create_license_manager",
    "create_log_collector",
    "create_windows_registry",
]
