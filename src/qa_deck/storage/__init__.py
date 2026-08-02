"""Local storage for QA Deck."""

from qa_deck.storage.operation_log_repository import OperationLogRepository
from qa_deck.storage.plugin_configuration_repository import (
    PluginConfigurationRepository,
)
from qa_deck.storage.product_repository import ProductRepository

__all__ = [
    "OperationLogRepository",
    "PluginConfigurationRepository",
    "ProductRepository",
]
