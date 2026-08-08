"""QA Deck application package."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from flask import Flask

from qa_deck.environment_profiles import EnvironmentProfileExecutionStateStore
from qa_deck.plugins import PluginManager
from qa_deck.plugins.builtin.windows_registry import RegistryExecutionStateStore
from qa_deck.plugins.discovery import discover_builtin_plugins
from qa_deck.snapshot import SnapshotRestoreStateStore
from qa_deck.storage import (
    EnvironmentProfileRepository,
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
    SnapshotRepository,
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
        ENVIRONMENT_PROFILE_DATA_PATH=(
            Path(app.instance_path) / "environment_profiles.json"
        ),
        SNAPSHOT_DATA_PATH=Path(app.instance_path) / "snapshots.json",
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
    environment_profile_data_path = cast(
        str | Path, app.config["ENVIRONMENT_PROFILE_DATA_PATH"]
    )
    app.extensions["environment_profile_repository"] = (
        EnvironmentProfileRepository(environment_profile_data_path)
    )
    snapshot_data_path = cast(str | Path, app.config["SNAPSHOT_DATA_PATH"])
    app.extensions["snapshot_repository"] = SnapshotRepository(
        snapshot_data_path
    )
    app.extensions["snapshot_restore_state"] = SnapshotRestoreStateStore()
    app.extensions["registry_execution_state"] = RegistryExecutionStateStore()
    app.extensions["environment_profile_execution_state"] = (
        EnvironmentProfileExecutionStateStore()
    )

    plugin_manager = PluginManager()
    discover_builtin_plugins(plugin_manager)
    app.extensions["plugin_manager"] = plugin_manager

    from qa_deck.web.routes import web_blueprint

    app.register_blueprint(web_blueprint)
    return app
