"""Tests for the JSON product repository."""

import json
from pathlib import Path

import pytest

from qa_deck.domain import Product
from qa_deck.storage import ProductRepository


def test_missing_file_is_an_empty_repository(tmp_path: Path) -> None:
    file_path = tmp_path / "data" / "products.json"
    repository = ProductRepository(file_path)

    assert repository.list_all() == []
    assert not file_path.exists()


def test_add_creates_parent_directory_and_stores_product(tmp_path: Path) -> None:
    file_path = tmp_path / "nested" / "data" / "products.json"
    repository = ProductRepository(file_path)
    product = Product(id="sample", name="Sample Product")

    repository.add(product)

    assert file_path.exists()
    assert repository.list_all() == [product]


def test_new_repository_instance_reads_saved_products(tmp_path: Path) -> None:
    file_path = tmp_path / "products.json"
    product = Product(
        id="desktop-app",
        name="Desktop Application",
        executable_path="C:/Apps/Sample/app.exe",
        launch_arguments=["--profile", "trial"],
    )
    ProductRepository(file_path).add(product)

    restored = ProductRepository(file_path).list_all()

    assert restored == [product]


def test_repository_stores_multiple_products(tmp_path: Path) -> None:
    repository = ProductRepository(tmp_path / "products.json")
    first = Product(id="first", name="First Product")
    second = Product(id="second", name="Second Product")

    repository.add(first)
    repository.add(second)

    assert repository.list_all() == [first, second]


def test_get_returns_existing_product_and_none_for_missing_id(
    tmp_path: Path,
) -> None:
    repository = ProductRepository(tmp_path / "products.json")
    product = Product(id="Sample", name="Sample Product")
    repository.add(product)

    assert repository.get("Sample") == product
    assert repository.get("sample") is None
    assert repository.get("missing") is None


def test_add_rejects_duplicate_id(tmp_path: Path) -> None:
    repository = ProductRepository(tmp_path / "products.json")
    original = Product(id="sample", name="Original Product")
    repository.add(original)

    with pytest.raises(ValueError, match="already exists"):
        repository.add(Product(id="sample", name="Duplicate Product"))

    assert repository.list_all() == [original]


def test_unicode_is_saved_as_utf8_text(tmp_path: Path) -> None:
    file_path = tmp_path / "products.json"
    repository = ProductRepository(file_path)
    product = Product(
        id="ukrainian-app",
        name="Тестовий застосунок",
        description="Перевірка ліцензії та налаштувань",
        launch_arguments=["--профіль", "тестовий"],
    )

    repository.add(product)

    saved_text = file_path.read_text(encoding="utf-8")
    saved_data = json.loads(saved_text)
    assert "Тестовий застосунок" in saved_text
    assert saved_data == [product.to_dict()]
