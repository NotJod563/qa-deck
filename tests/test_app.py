"""Tests for the QA Deck web application."""

from pathlib import Path
from typing import cast

import pytest
from flask import Flask
from flask.testing import FlaskClient

from qa_deck import create_app
from qa_deck.storage import ProductRepository


@pytest.fixture()
def data_path(tmp_path: Path) -> Path:
    return tmp_path / "products.json"


@pytest.fixture()
def app(data_path: Path) -> Flask:
    """Create an application with isolated product storage."""
    return create_app(
        {
            "TESTING": True,
            "PRODUCT_DATA_PATH": data_path,
        }
    )


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Create a test client for the application."""
    return app.test_client()


def test_index_redirects_to_product_list(client: FlaskClient) -> None:
    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/products")


def test_health(client: FlaskClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_empty_product_list(client: FlaskClient) -> None:
    response = client.get("/products")

    assert response.status_code == 200
    assert "Ще немає збережених продуктів." in response.get_data(as_text=True)


def test_open_new_product_form(client: FlaskClient) -> None:
    response = client.get("/products/new")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="name"' in page
    assert 'name="launch_arguments"' in page
    assert 'name="id"' not in page


def test_create_product(client: FlaskClient, app: Flask) -> None:
    response = client.post(
        "/products/new",
        data={
            "name": "Sample Product",
            "description": "Test application",
            "executable_path": "C:/Apps/Sample/app.exe",
            "working_directory": "C:/Apps/Sample",
            "launch_arguments": "--profile\ntrial\n",
        },
    )

    assert response.status_code == 302
    assert "/products/" in response.headers["Location"]

    repository = cast(
        ProductRepository,
        app.extensions["product_repository"],
    )
    products = repository.list_all()
    assert len(products) == 1
    assert products[0].name == "Sample Product"
    assert products[0].launch_arguments == ["--profile", "trial"]


def test_created_product_appears_in_list(client: FlaskClient) -> None:
    client.post("/products/new", data={"name": "Sample Product"})

    response = client.get("/products")

    assert response.status_code == 200
    assert "Sample Product" in response.get_data(as_text=True)


def test_view_product_detail(client: FlaskClient) -> None:
    create_response = client.post(
        "/products/new",
        data={
            "name": "Sample Product",
            "description": "Desktop application",
            "executable_path": "Z:/missing/app.exe",
            "working_directory": "Z:/missing",
            "launch_arguments": "--safe-mode",
        },
    )

    response = client.get(create_response.headers["Location"])
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sample Product" in page
    assert "Desktop application" in page
    assert "Z:/missing/app.exe" in page
    assert "--safe-mode" in page


def test_product_is_available_to_new_app_request(data_path: Path) -> None:
    first_app = create_app(
        {"TESTING": True, "PRODUCT_DATA_PATH": data_path}
    )
    first_app.test_client().post(
        "/products/new",
        data={"name": "Persistent Product"},
    )

    second_app = create_app(
        {"TESTING": True, "PRODUCT_DATA_PATH": data_path}
    )
    response = second_app.test_client().get("/products")

    assert response.status_code == 200
    assert "Persistent Product" in response.get_data(as_text=True)


def test_unknown_product_returns_404(client: FlaskClient) -> None:
    response = client.get("/products/unknown")

    assert response.status_code == 404


def test_empty_product_name_shows_error(client: FlaskClient, app: Flask) -> None:
    response = client.post(
        "/products/new",
        data={"name": "   ", "description": "Not saved"},
    )

    assert response.status_code == 400
    assert "Вкажіть назву продукту." in response.get_data(as_text=True)

    repository = cast(
        ProductRepository,
        app.extensions["product_repository"],
    )
    assert repository.list_all() == []
