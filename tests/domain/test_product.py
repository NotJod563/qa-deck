"""Tests for the Product domain model."""

import pytest

from qa_deck.domain import Product


def test_product_has_expected_defaults() -> None:
    product = Product(id="sample", name="Sample Product")

    assert product.description == ""
    assert product.executable_path is None
    assert product.working_directory is None
    assert product.launch_arguments == []


def test_product_strips_required_fields() -> None:
    product = Product(id="  sample  ", name="  Sample Product  ")

    assert product.id == "sample"
    assert product.name == "Sample Product"


@pytest.mark.parametrize("product_id", ["", "   ", "\t\n"])
def test_product_rejects_empty_id(product_id: str) -> None:
    with pytest.raises(ValueError, match="id must not be empty"):
        Product(id=product_id, name="Sample Product")


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_product_rejects_empty_name(name: str) -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        Product(id="sample", name=name)


def test_products_have_independent_launch_arguments() -> None:
    first = Product(id="first", name="First")
    second = Product(id="second", name="Second")

    first.launch_arguments.append("--safe-mode")

    assert first.launch_arguments == ["--safe-mode"]
    assert second.launch_arguments == []


def test_product_round_trip() -> None:
    product = Product(
        id="sample",
        name="Sample Product",
        description="Desktop application used for testing",
        executable_path="C:/Apps/Sample/sample.exe",
        working_directory="C:/Apps/Sample",
        launch_arguments=["--profile", "trial"],
    )

    restored = Product.from_dict(product.to_dict())

    assert restored == product
    assert restored.launch_arguments is not product.launch_arguments


def test_from_dict_uses_optional_defaults() -> None:
    product = Product.from_dict({"id": "sample", "name": "Sample Product"})

    assert product == Product(id="sample", name="Sample Product")


def test_paths_are_stored_without_file_system_validation() -> None:
    product = Product(
        id="sample",
        name="Sample Product",
        executable_path="Z:/path/that/does/not/exist.exe",
        working_directory="Z:/path/that/does/not/exist",
    )

    assert product.executable_path == "Z:/path/that/does/not/exist.exe"
    assert product.working_directory == "Z:/path/that/does/not/exist"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  C:/Apps/Sample/App.EXE  ", "C:/Apps/Sample/App.EXE"),
        (
            '  "C:\\Program Files\\Sample\\App.EXE"  ',
            "C:\\Program Files\\Sample\\App.EXE",
        ),
        ("  'D:/QA Data/Work'  ", "D:/QA Data/Work"),
        ('  "C:/Mixed\\Path/App.EXE\'  ', '"C:/Mixed\\Path/App.EXE\''),
        ('  ""C:/Apps/app.exe""  ', '"C:/Apps/app.exe"'),
        ("  ' C:/Path With Edge Spaces '  ", " C:/Path With Edge Spaces "),
    ],
)
def test_product_normalizes_paths(value: str, expected: str) -> None:
    product = Product(
        id="sample",
        name="Sample Product",
        executable_path=value,
        working_directory=value,
    )

    assert product.executable_path == expected
    assert product.working_directory == expected
