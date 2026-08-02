"""Built-in Log Collector plugin."""

from qa_deck.plugins.builtin.log_collector.collection import (
    LogCollectionResult,
    LogCollectionService,
    SkippedLogFile,
)
from qa_deck.plugins.builtin.log_collector.models import (
    LogCollectorConfiguration,
    LogInspectionResult,
    LogSourceInspection,
)
from qa_deck.plugins.builtin.log_collector.plugin import (
    LogCollector,
    create_log_collector,
)

__all__ = [
    "LogCollector",
    "LogCollectorConfiguration",
    "LogCollectionResult",
    "LogCollectionService",
    "LogInspectionResult",
    "LogSourceInspection",
    "SkippedLogFile",
    "create_log_collector",
]
