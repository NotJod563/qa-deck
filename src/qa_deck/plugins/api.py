"""Public contracts for QA Deck plugins."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RiskLevel(StrEnum):
    """Risk associated with a plugin action."""

    SAFE = "safe"
    CAUTION = "caution"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class PluginAction:
    """Describe an action exposed by a plugin."""

    identifier: str
    display_name: str
    description: str
    risk_level: RiskLevel


class Plugin(Protocol):
    """Contract implemented by QA Deck plugins."""

    identifier: str
    display_name: str
    description: str
    version: str

    def get_actions(self) -> list[PluginAction]:
        """Return actions exposed by the plugin."""
        ...
