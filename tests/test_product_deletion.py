"""Focused Product deletion safety and cleanup behavior."""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from flask import Flask

from qa_deck.domain import (
    EnvironmentProfile,
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    Product,
    Snapshot,
)
from qa_deck.storage import SnapshotRepository
from tests.helpers import (
    configurations,
    environment_profiles,
    make_app,
    operation_logs,
    products,
)


def _snapshots(app: Flask) -> SnapshotRepository:
    return cast(SnapshotRepository, app.extensions["snapshot_repository"])


def test_product_delete_dialog_focuses_non_destructive_action(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    page = app.test_client().get("/products/sample").get_data(as_text=True)
    dialog = page[
        page.index('id="product-delete-dialog"') : page.index(
            "</dialog>", page.index('id="product-delete-dialog"')
        )
    ]

    assert 'data-dialog-close autofocus' in dialog
    assert 'class="button button-danger" type="submit" autofocus' not in dialog


def test_product_deletion_removes_owned_metadata_but_not_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)
    manager = app.extensions["plugin_manager"]

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Runtime operations must not run during deletion")

    monkeypatch.setattr(manager.get("license-manager"), "execute", forbidden)
    monkeypatch.setattr(manager.get("log-collector"), "collect", forbidden)
    monkeypatch.setattr(manager.get("windows-registry"), "execute_preset", forbidden)
    executable = tmp_path / "application.exe"
    license_file = tmp_path / "license.dat"
    source_log = tmp_path / "application.log"
    backup_file = tmp_path / "backups" / "sample" / "license.dat"
    for path, content in (
        (executable, b"executable"),
        (license_file, b"license"),
        (source_log, b"log"),
        (backup_file, b"backup"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    product = Product(
        "deletable",
        "Deletable Product",
        executable_path=str(executable),
        working_directory=str(tmp_path),
    )
    products(app).add(product)
    configurations(app).upsert(
        PluginConfiguration(
            product.id,
            "license-manager",
            True,
            {
                "license_directory": str(tmp_path),
                "license_files": [license_file.name],
            },
        )
    )
    other_configuration = PluginConfiguration("other", "test-plugin", True, {})
    configurations(app).upsert(other_configuration)
    configurations(app).upsert(
        PluginConfiguration(
            product.id,
            "log-collector",
            True,
            {"log_directories": [str(tmp_path)]},
        )
    )
    _snapshots(app).add(
        Snapshot("delete-snapshot", product.id, datetime.now(UTC), "Before", ())
    )
    environment_profiles(app).add(
        EnvironmentProfile("delete-profile", product.id, "Delete", "preset")
    )
    operation_logs(app).append(
        OperationLog(
            "audit-entry",
            datetime.now(UTC),
            product.id,
            "test",
            "read-only-check",
            OperationStatus.SUCCESS,
            "Historical audit entry",
            0,
            0,
            0,
        )
    )

    response = app.test_client().post(
        f"/products/{product.id}/delete", data={"confirm": "yes"}
    )
    result_page = app.test_client().get(response.headers["Location"])

    assert response.status_code == 302
    assert result_page.status_code == 200
    assert "пов’язані дані QA Deck видалено" in result_page.get_data(as_text=True)
    assert products(app).get(product.id) is None
    assert configurations(app).list_for_product(product.id) == []
    assert _snapshots(app).list_for_product(product.id) == []
    assert environment_profiles(app).list_for_product(product.id) == []
    assert operation_logs(app).list_for_product(product.id)
    assert products(app).get("other") is not None
    assert configurations(app).get("other", "test-plugin") == other_configuration
    assert executable.read_bytes() == b"executable"
    assert license_file.read_bytes() == b"license"
    assert source_log.read_bytes() == b"log"
    assert backup_file.read_bytes() == b"backup"
    assert app.test_client().get(response.headers["Location"]).status_code == 200


def test_product_deletion_requires_confirmation_and_missing_is_safe(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    rejected = client.post("/products/sample/delete", data={})
    missing = client.post("/products/missing/delete", data={"confirm": "yes"})

    assert rejected.status_code == 400
    assert "Підтвердження видалення" in rejected.get_data(as_text=True)
    assert products(app).get("sample") is not None
    assert missing.status_code == 404


def test_cleanup_failure_rolls_back_and_does_not_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)
    configuration = PluginConfiguration("sample", "test-plugin", True, {})
    configurations(app).upsert(configuration)

    def fail_snapshot_cleanup(product_id: str):
        del product_id
        raise OSError("private storage failure")

    monkeypatch.setattr(_snapshots(app), "delete_for_product", fail_snapshot_cleanup)
    response = app.test_client().post(
        "/products/sample/delete", data={"confirm": "yes"}
    )
    page = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "Не вдалося видалити Product" in page
    assert "пов’язані дані QA Deck видалено" not in page
    assert products(app).get("sample") is not None
    assert configurations(app).get("sample", "test-plugin") == configuration
