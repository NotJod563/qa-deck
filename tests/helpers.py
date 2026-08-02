"""Small shared builders for the compact test suite."""

from pathlib import Path
from typing import cast

from flask import Flask

from qa_deck import create_app
from qa_deck.domain import PluginConfiguration, Product
from qa_deck.storage import (
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
)


def make_app(tmp_path: Path) -> Flask:
    app = create_app(
        {
            "TESTING": True,
            "PRODUCT_DATA_PATH": tmp_path / "products.json",
            "PLUGIN_CONFIGURATION_DATA_PATH": tmp_path / "configurations.json",
            "OPERATION_LOG_DATA_PATH": tmp_path / "operations.json",
            "PLUGIN_BACKUP_ROOT": tmp_path / "backups",
            "LOG_COLLECTION_MAX_FILES": 100,
            "LOG_COLLECTION_MAX_TOTAL_BYTES": 10_000,
        }
    )
    products(app).add(Product("sample", "Sample Product"))
    products(app).add(Product("other", "Other Product"))
    return app


def license_configuration(
    directory: Path,
    filenames: list[str] | None = None,
    *,
    enabled: bool = True,
) -> PluginConfiguration:
    return PluginConfiguration(
        "sample",
        "license-manager",
        enabled,
        {
            "license_directory": str(directory),
            "license_files": filenames or ["license.dat"],
        },
    )


def log_configuration(
    *directories: Path,
    enabled: bool = True,
) -> PluginConfiguration:
    return PluginConfiguration(
        "sample",
        "log-collector",
        enabled,
        {"log_directories": [str(path) for path in directories]},
    )


def products(app: Flask) -> ProductRepository:
    return cast(ProductRepository, app.extensions["product_repository"])


def configurations(app: Flask) -> PluginConfigurationRepository:
    return cast(
        PluginConfigurationRepository,
        app.extensions["plugin_configuration_repository"],
    )


def operation_logs(app: Flask) -> OperationLogRepository:
    return cast(
        OperationLogRepository, app.extensions["operation_log_repository"]
    )
