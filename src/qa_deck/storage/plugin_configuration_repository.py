"""JSON repository for per-product plugin configurations."""

from pathlib import Path

from qa_deck.domain import PluginConfiguration
from qa_deck.storage.json_file import read_json_list, write_json_list_atomic


class PluginConfigurationRepository:
    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def get(
        self,
        product_id: str,
        plugin_identifier: str,
    ) -> PluginConfiguration | None:
        return next(
            (
                item
                for item in self.list_for_product(product_id)
                if item.plugin_identifier == plugin_identifier
            ),
            None,
        )

    def list_for_product(self, product_id: str) -> list[PluginConfiguration]:
        return [
            configuration
            for configuration in self._list_all()
            if configuration.product_id == product_id
        ]

    def upsert(self, configuration: PluginConfiguration) -> None:
        configurations = self._list_all()
        updated: list[PluginConfiguration] = []
        replacement_added = False
        for existing in configurations:
            if (
                existing.product_id == configuration.product_id
                and existing.plugin_identifier == configuration.plugin_identifier
            ):
                if not replacement_added:
                    updated.append(configuration)
                    replacement_added = True
                continue
            updated.append(existing)
        if not replacement_added:
            updated.append(configuration)

        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in updated],
        )

    def delete(self, product_id: str, plugin_identifier: str) -> None:
        configurations = [
            item
            for item in self._list_all()
            if not (
                item.product_id == product_id
                and item.plugin_identifier == plugin_identifier
            )
        ]
        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in configurations],
        )

    def _list_all(self) -> list[PluginConfiguration]:
        return [
            PluginConfiguration.from_dict(item)
            for item in read_json_list(self._file_path)
        ]
