"""Build snapshot objects from product state and persisted plugin configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from logging import Logger
from uuid import uuid4

from qa_deck.domain import (
    Product,
    Snapshot,
    SnapshotResource,
)
from qa_deck.domain.snapshot import SnapshotCaptureResult
from qa_deck.plugins.manager import PluginManager
from qa_deck.storage import PluginConfigurationRepository


class SnapshotBuilder:
    """Create read-only snapshots from available product inspection sources."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        configuration_repository: PluginConfigurationRepository,
        logger: Logger | None = None,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._configuration_repository = configuration_repository
        self._logger = logger

    def build_snapshot(
        self,
        product: Product,
        label: str | None = None,
    ) -> Snapshot:
        warnings: list[str] = []
        resources: list[SnapshotResource] = []
        resource_identities: set[tuple[str, str, str]] = set()

        for plugin in self._plugin_manager.list_all():
            try:
                plugin_identifier = plugin.identifier
                capture_snapshot = getattr(plugin, "capture_snapshot", None)
            except Exception:
                self._log_provider_failure("unknown", product.id)
                warnings.append("A snapshot provider could not be inspected.")
                continue

            if capture_snapshot is None:
                continue
            if not callable(capture_snapshot):
                if self._logger is not None:
                    self._logger.error(
                        "Snapshot capability for plugin %s is not callable",
                        plugin_identifier,
                    )
                warnings.append(
                    f"Snapshot provider {plugin_identifier} is unavailable."
                )
                continue

            try:
                configuration = self._configuration_repository.get(
                    product.id,
                    plugin_identifier,
                )
                result = capture_snapshot(product, configuration)
                self._validate_result(result)
                provider_resources = tuple(result.resources)
                provider_warnings = tuple(result.warnings)
                provider_identities = {
                    (resource.source, resource.resource_type, resource.identifier)
                    for resource in provider_resources
                }
                if len(provider_identities) != len(provider_resources) or (
                    provider_identities & resource_identities
                ):
                    raise ValueError(
                        "Snapshot provider returned duplicate resource identities"
                    )
            except Exception:
                self._log_provider_failure(plugin_identifier, product.id)
                warnings.append(
                    f"Snapshot provider {plugin_identifier} failed."
                )
                continue

            resources.extend(provider_resources)
            resource_identities.update(provider_identities)
            warnings.extend(provider_warnings)

        metadata: dict[str, object] = {}
        if warnings:
            metadata["warnings"] = warnings

        return Snapshot(
            id=str(uuid4()),
            product_id=product.id,
            created_at=datetime.now(UTC),
            label=label,
            resources=tuple(resources),
            metadata=metadata,
        )

    def _log_provider_failure(self, plugin_identifier: str, product_id: str) -> None:
        if self._logger is not None:
            self._logger.exception(
                "Snapshot provider %s failed for product %s",
                plugin_identifier,
                product_id,
            )

    @staticmethod
    def _validate_result(result: object) -> None:
        if not isinstance(result, SnapshotCaptureResult):
            raise TypeError("Snapshot provider returned an invalid result")
        if not isinstance(result.resources, tuple) or not all(
            isinstance(resource, SnapshotResource) for resource in result.resources
        ):
            raise TypeError("Snapshot provider returned invalid resources")
        if not isinstance(result.warnings, tuple) or not all(
            isinstance(warning, str) for warning in result.warnings
        ):
            raise TypeError("Snapshot provider returned invalid warnings")
