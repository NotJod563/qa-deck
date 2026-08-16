"""Application service for deleting QA Deck-owned Product data."""

from dataclasses import dataclass
from logging import Logger

from qa_deck.domain import EnvironmentProfile, PluginConfiguration, Product, Snapshot
from qa_deck.storage import (
    EnvironmentProfileRepository,
    PluginConfigurationRepository,
    ProductRepository,
    SnapshotRepository,
)


@dataclass(frozen=True, slots=True)
class ProductDeletionResult:
    status: str
    product: Product | None
    message: str

    @property
    def succeeded(self) -> bool:
        return self.status == "deleted"


class ProductDeletionService:
    """Remove Product metadata without touching its runtime environment."""

    def __init__(
        self,
        products: ProductRepository,
        configurations: PluginConfigurationRepository,
        snapshots: SnapshotRepository,
        profiles: EnvironmentProfileRepository,
        logger: Logger | None = None,
    ) -> None:
        self._products = products
        self._configurations = configurations
        self._snapshots = snapshots
        self._profiles = profiles
        self._logger = logger

    def delete(self, product_id: str) -> ProductDeletionResult:
        try:
            product = self._products.get(product_id)
            if product is None:
                return ProductDeletionResult(
                    "not_found", None, "Product уже видалено або не існує."
                )
            self._configurations.list_for_product(product_id)
            self._snapshots.list_for_product(product_id)
            self._profiles.list_for_product(product_id)
        except Exception:
            self._log_failure("preflight", product_id)
            return ProductDeletionResult(
                "failed",
                None,
                "Не вдалося перевірити пов’язані дані Product. Нічого не видалено.",
            )

        removed_configurations: list[PluginConfiguration] = []
        removed_snapshots: list[Snapshot] = []
        removed_profiles: list[EnvironmentProfile] = []
        try:
            removed_configurations = self._configurations.delete_for_product(
                product_id
            )
            removed_snapshots = self._snapshots.delete_for_product(product_id)
            removed_profiles = self._profiles.delete_for_product(product_id)
            if self._products.remove(product_id) is None:
                raise ValueError("Product disappeared during deletion")
        except Exception:
            self._log_failure("cleanup", product_id)
            rollback_complete = self._restore_removed(
                removed_configurations,
                removed_snapshots,
                removed_profiles,
            )
            message = "Не вдалося видалити Product."
            message += (
                " Зміни сховища скасовано."
                if rollback_complete
                else " Частину даних не вдалося відновити автоматично."
            )
            return ProductDeletionResult("failed", product, message)

        return ProductDeletionResult(
            "deleted", product, "Product і пов’язані дані QA Deck видалено."
        )

    def _restore_removed(
        self,
        configurations: list[PluginConfiguration],
        snapshots: list[Snapshot],
        profiles: list[EnvironmentProfile],
    ) -> bool:
        complete = True
        for profile in profiles:
            try:
                self._profiles.add(profile)
            except Exception:
                complete = False
                self._log_failure("profile rollback", profile.product_id)
        for snapshot in snapshots:
            try:
                self._snapshots.add(snapshot)
            except Exception:
                complete = False
                self._log_failure("snapshot rollback", snapshot.product_id)
        for configuration in configurations:
            try:
                self._configurations.upsert(configuration)
            except Exception:
                complete = False
                self._log_failure(
                    "configuration rollback", configuration.product_id
                )
        return complete

    def _log_failure(self, stage: str, product_id: str) -> None:
        if self._logger is not None:
            self._logger.exception(
                "Product deletion %s failed for %s", stage, product_id
            )
