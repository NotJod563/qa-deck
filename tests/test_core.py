"""Core models and JSON repositories."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qa_deck.domain import (
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    Product,
)
from qa_deck.storage import (
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
)


def test_product_validation_and_repository_round_trip(tmp_path: Path) -> None:
    for invalid_id, invalid_name in [("", "Name"), ("id", "  ")]:
        with pytest.raises(ValueError):
            Product(invalid_id, invalid_name)
    product = Product(
        " product-id ",
        " Тестовий продукт ",
        executable_path=' "C:\\Apps\\Sample.exe" ',
        launch_arguments=["--trial"],
    )
    repository = ProductRepository(tmp_path / "data" / "products.json")
    repository.add(product)

    assert ProductRepository(tmp_path / "data" / "products.json").get(
        "product-id"
    ) == Product(
        "product-id",
        "Тестовий продукт",
        executable_path="C:\\Apps\\Sample.exe",
        launch_arguments=["--trial"],
    )


def test_plugin_configuration_upsert_and_isolation(tmp_path: Path) -> None:
    repository = PluginConfigurationRepository(tmp_path / "configurations.json")
    first = PluginConfiguration("one", "plugin-a", True, {"value": 1})
    repository.upsert(first)
    repository.upsert(
        PluginConfiguration("one", "plugin-a", False, {"value": 2})
    )
    repository.upsert(PluginConfiguration("one", "plugin-b", True, {}))
    repository.upsert(PluginConfiguration("two", "plugin-a", True, {}))

    saved = repository.get("one", "plugin-a")
    assert saved == PluginConfiguration.from_dict(saved.to_dict())  # type: ignore[union-attr]
    assert saved is not None and saved.settings == {"value": 2}
    assert {item.plugin_identifier for item in repository.list_for_product("one")} == {
        "plugin-a",
        "plugin-b",
    }
    assert len(repository.list_for_product("two")) == 1


def test_corrupted_json_is_reported_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "configurations.json"
    original = "{broken private data"
    path.write_text(original, encoding="utf-8")
    repository = PluginConfigurationRepository(path)

    with pytest.raises(ValueError):
        repository.get("sample", "plugin")
    with pytest.raises(ValueError):
        repository.upsert(PluginConfiguration("sample", "plugin", True, {}))
    assert path.read_text(encoding="utf-8") == original


def test_operation_log_append_and_product_list(tmp_path: Path) -> None:
    repository = OperationLogRepository(tmp_path / "operations.json")
    for product_id, summary in [("one", "Done"), ("two", "Other")]:
        repository.append(
            OperationLog(
                product_id,
                datetime.now(UTC),
                product_id,
                "plugin",
                "action",
                OperationStatus.SUCCESS,
                summary,
                1,
                0,
                0,
            )
        )

    logs = repository.list_for_product("one")
    assert len(logs) == 1
    assert logs[0].summary == "Done"
