"""Per-product plugin configuration model."""

from copy import deepcopy
from dataclasses import dataclass
from typing import Self, cast


@dataclass(slots=True)
class PluginConfiguration:
    """Store settings for one plugin attached to one product."""

    product_id: str
    plugin_identifier: str
    enabled: bool
    settings: dict[str, object]

    def __post_init__(self) -> None:
        self.product_id = self.product_id.strip()
        self.plugin_identifier = self.plugin_identifier.strip()
        if not self.product_id:
            raise ValueError("Product id must not be empty")
        if not self.plugin_identifier:
            raise ValueError("Plugin identifier must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "plugin_identifier": self.plugin_identifier,
            "enabled": self.enabled,
            "settings": deepcopy(self.settings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        return cls(
            product_id=cast(str, data["product_id"]),
            plugin_identifier=cast(str, data["plugin_identifier"]),
            enabled=cast(bool, data["enabled"]),
            settings=deepcopy(cast(dict[str, object], data.get("settings", {}))),
        )
