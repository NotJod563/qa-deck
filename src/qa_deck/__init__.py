"""QA Deck application package."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from flask import Flask

from qa_deck.plugins import PluginManager
from qa_deck.plugins.discovery import discover_builtin_plugins
from qa_deck.storage import (
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
)


def create_app(test_config: Mapping[str, object] | None = None) -> Flask:
    """Create and configure the QA Deck Flask application."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        PRODUCT_DATA_PATH=Path(app.instance_path) / "products.json",
        PLUGIN_CONFIGURATION_DATA_PATH=(
            Path(app.instance_path) / "plugin_configurations.json"
        ),
        OPERATION_LOG_DATA_PATH=Path(app.instance_path) / "operation_logs.json",
        PLUGIN_BACKUP_ROOT=Path(app.instance_path) / "backups",
        LOG_COLLECTION_MAX_FILES=5_000,
        LOG_COLLECTION_MAX_TOTAL_BYTES=500 * 1024 * 1024,
    )

    if test_config is not None:
        app.config.from_mapping(test_config)

    product_data_path = cast(str | Path, app.config["PRODUCT_DATA_PATH"])
    app.extensions["product_repository"] = ProductRepository(product_data_path)
    configuration_data_path = cast(
        str | Path, app.config["PLUGIN_CONFIGURATION_DATA_PATH"]
    )
    app.extensions["plugin_configuration_repository"] = (
        PluginConfigurationRepository(configuration_data_path)
    )
    operation_log_data_path = cast(
        str | Path, app.config["OPERATION_LOG_DATA_PATH"]
    )
    app.extensions["operation_log_repository"] = OperationLogRepository(
        operation_log_data_path
    )

    plugin_manager = PluginManager()
    discover_builtin_plugins(plugin_manager)
    app.extensions["plugin_manager"] = plugin_manager

    from qa_deck.web.routes import web_blueprint

    app.register_blueprint(web_blueprint)
    return app
