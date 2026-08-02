"""Web routes for QA Deck."""

import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from qa_deck.domain import PluginConfiguration, Product
from qa_deck.plugins import PluginAction, PluginManager
from qa_deck.plugins.builtin import (
    ExecutableInspector,
    LicenseManager,
    LogCollector,
)
from qa_deck.storage import (
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
)

web_blueprint = Blueprint("web", __name__)


@dataclass(frozen=True, slots=True)
class PluginView:
    identifier: str
    display_name: str
    description: str
    version: str
    actions: tuple[PluginAction, ...]
    actions_error: str | None = None


@web_blueprint.get("/")
def index() -> Response:
    """Redirect to the product list."""
    return redirect(url_for("web.product_list"))


@web_blueprint.get("/products")
def product_list() -> str:
    """Show all stored products."""
    return render_template("products/list.html", products=_repository().list_all())


@web_blueprint.route("/products/new", methods=["GET", "POST"])
def product_new() -> str | Response | tuple[str, int]:
    """Show the product form and store valid submissions."""
    if request.method == "GET":
        return render_template("products/new.html", error=None, form={})

    try:
        product = Product(
            id=str(uuid4()),
            name=request.form.get("name", ""),
            description=request.form.get("description", "").strip(),
            executable_path=_optional_text(request.form.get("executable_path", "")),
            working_directory=_optional_text(
                request.form.get("working_directory", "")
            ),
            launch_arguments=_launch_arguments(
                request.form.get("launch_arguments", "")
            ),
        )
    except ValueError:
        return (
            render_template(
                "products/new.html",
                error="Вкажіть назву продукту.",
                form=request.form,
            ),
            400,
        )

    _repository().add(product)
    return redirect(url_for("web.product_detail", product_id=product.id))


@web_blueprint.get("/products/<product_id>")
def product_detail(product_id: str) -> str:
    """Show one product or return 404 when it is missing."""
    return _render_product_detail(_product_or_404(product_id))


@web_blueprint.post("/products/<product_id>/inspect-executable")
def product_inspect_executable(product_id: str) -> str:
    """Inspect a product executable without changing the product."""
    product = _product_or_404(product_id)

    inspection_result = _executable_inspector().inspect(product.executable_path)
    return _render_product_detail(product, inspection_result=inspection_result)


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/configuration"
)
def save_license_manager_configuration(product_id: str) -> str | Response:
    """Validate and save License Manager settings for one product."""
    product = _product_or_404(product_id)
    plugin = _available_license_manager()
    if plugin is None:
        return _render_product_detail(
            product, license_error="License Manager зараз недоступний."
        ), 503
    try:
        configuration = plugin.create_configuration(
            product_id=product.id,
            enabled=request.form.get("enabled") == "on",
            license_directory=request.form.get("license_directory", ""),
            license_files_text=request.form.get("license_files", ""),
        )
    except ValueError as error:
        return _render_product_detail(
            product,
            license_form=request.form,
            license_error=str(error),
        ), 400
    try:
        _configuration_repository().upsert(configuration)
    except Exception:
        current_app.logger.exception(
            "Could not save License Manager configuration"
        )
        return _render_product_detail(
            product,
            license_form=request.form,
            license_error=(
                "Не вдалося зберегти налаштування: сховище конфігурацій "
                "недоступне або пошкоджене."
            ),
        ), 500
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            _anchor="license-manager",
        )
    )


@web_blueprint.post("/products/<product_id>/plugins/license-manager/inspect")
def inspect_licenses(product_id: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_license_manager()
    if plugin is None:
        return _render_product_detail(
            product, license_error="License Manager зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LicenseManager.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, license_error=storage_error
        ), 500
    validation_error = _license_configuration_validation_error(
        plugin, configuration
    )
    if validation_error:
        return _render_product_detail(
            product, license_error=validation_error
        ), 400
    result = plugin.inspect(configuration)
    return _render_product_detail(product, license_result=result)


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/preview-hide"
)
def preview_hide_licenses(product_id: str) -> str:
    return _preview_license_action(product_id, "hide-licenses")


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/preview-restore"
)
def preview_restore_licenses(product_id: str) -> str:
    return _preview_license_action(product_id, "restore-licenses")


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/confirm-hide"
)
def confirm_hide_licenses(product_id: str) -> str:
    return _confirm_license_action(product_id, "hide-licenses")


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/confirm-restore"
)
def confirm_restore_licenses(product_id: str) -> str:
    return _confirm_license_action(product_id, "restore-licenses")


@web_blueprint.post(
    "/products/<product_id>/plugins/license-manager/inspect-backup"
)
def inspect_license_backup(product_id: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_license_manager()
    if plugin is None:
        return _render_product_detail(
            product, license_error="License Manager зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LicenseManager.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, license_error=storage_error
        ), 500
    if configuration is None or not configuration.enabled:
        return _render_product_detail(
            product,
            license_error="Спочатку увімкніть і збережіть License Manager.",
        ), 400
    try:
        plugin.typed_configuration(configuration)
    except ValueError:
        current_app.logger.exception("Invalid License Manager configuration")
        return _render_product_detail(
            product,
            license_error="Збережена конфігурація License Manager некоректна.",
        ), 400
    try:
        result = plugin.inspect_backup(product.id, _backup_root())
    except Exception:
        current_app.logger.exception("Could not inspect License Manager backup")
        return _render_product_detail(
            product,
            license_error="Не вдалося безпечно перевірити backup.",
        ), 500
    return _render_product_detail(product, backup_result=result)


@web_blueprint.post(
    "/products/<product_id>/plugins/log-collector/configuration"
)
def save_log_collector_configuration(product_id: str) -> str | Response:
    """Validate and save Log Collector settings for one product."""
    product = _product_or_404(product_id)
    plugin = _available_log_collector()
    if plugin is None:
        return _render_product_detail(
            product, log_error="Log Collector зараз недоступний."
        ), 503
    try:
        configuration = plugin.create_configuration(
            product.id,
            request.form.get("enabled") == "on",
            request.form.get("log_directories", "").splitlines(),
        )
    except ValueError as error:
        return _render_product_detail(
            product,
            log_form=request.form,
            log_error=str(error),
        ), 400
    try:
        _configuration_repository().upsert(configuration)
    except Exception:
        current_app.logger.exception("Could not save Log Collector configuration")
        return _render_product_detail(
            product,
            log_form=request.form,
            log_error=(
                "Не вдалося зберегти налаштування: сховище конфігурацій "
                "недоступне або пошкоджене."
            ),
        ), 500
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            _anchor="log-collector",
        )
    )


@web_blueprint.post("/products/<product_id>/plugins/log-collector/inspect")
def inspect_log_sources(product_id: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_log_collector()
    if plugin is None:
        return _render_product_detail(
            product, log_error="Log Collector зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LogCollector.identifier
    )
    if storage_error:
        return _render_product_detail(product, log_error=storage_error), 500
    if configuration is None:
        return _render_product_detail(
            product,
            log_error="Спочатку налаштуйте Log Collector.",
        ), 400
    try:
        result = plugin.inspect(configuration)
    except ValueError:
        current_app.logger.exception("Invalid Log Collector configuration")
        return _render_product_detail(
            product,
            log_error="Збережена конфігурація Log Collector некоректна.",
        ), 400
    return _render_product_detail(product, log_result=result)


@web_blueprint.post("/products/<product_id>/plugins/log-collector/collect")
def collect_logs(product_id: str) -> str | Response | tuple[str, int]:
    """Create and download a temporary ZIP from saved product log sources."""
    product = _product_or_404(product_id)
    plugin = _available_log_collector()
    if plugin is None:
        return _render_product_detail(
            product, log_error="Log Collector зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LogCollector.identifier
    )
    if storage_error:
        return _render_product_detail(product, log_error=storage_error), 500

    result = plugin.collect(
        product=product,
        configuration=configuration,
        max_files=_positive_config_int("LOG_COLLECTION_MAX_FILES"),
        max_total_bytes=_positive_config_int(
            "LOG_COLLECTION_MAX_TOTAL_BYTES"
        ),
        operation_logs=_operation_logs(),
        logger=current_app.logger,
    )
    if not result.has_archive:
        return _render_product_detail(
            product, log_collection_result=result
        ), 400

    assert result.archive_path is not None
    assert result.temporary_directory is not None
    assert result.download_name is not None
    response = send_file(
        result.archive_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=result.download_name,
        conditional=False,
    )
    temporary_directory = result.temporary_directory
    response.response = _CleanupIterable(
        cast(Iterable[bytes], response.response), temporary_directory
    )
    if not result.operation_log_saved:
        response.headers["X-QA-Deck-Warning"] = "operation-log-not-saved"
    return response


@web_blueprint.get("/plugins")
def plugin_list() -> str:
    """Show plugins currently available to QA Deck."""
    plugin_views: list[PluginView] = []
    for plugin in _plugin_manager().list_all():
        try:
            actions = tuple(plugin.get_actions())
            actions_error = None
        except Exception:
            current_app.logger.exception(
                "Could not load actions for plugin %s", plugin.identifier
            )
            actions = ()
            actions_error = "Не вдалося завантажити дії цього плагіна."
        plugin_views.append(
            PluginView(
                identifier=plugin.identifier,
                display_name=plugin.display_name,
                description=plugin.description,
                version=plugin.version,
                actions=actions,
                actions_error=actions_error,
            )
        )
    return render_template(
        "plugins/list.html",
        plugins=plugin_views,
    )


@web_blueprint.get("/health")
def health() -> dict[str, str]:
    """Report whether the application is running."""
    return {"status": "ok"}


def _repository() -> ProductRepository:
    return cast(
        ProductRepository,
        current_app.extensions["product_repository"],
    )


def _configuration_repository() -> PluginConfigurationRepository:
    return cast(
        PluginConfigurationRepository,
        current_app.extensions["plugin_configuration_repository"],
    )


def _operation_logs() -> OperationLogRepository:
    return cast(
        OperationLogRepository,
        current_app.extensions["operation_log_repository"],
    )


def _plugin_manager() -> PluginManager:
    return cast(
        PluginManager,
        current_app.extensions["plugin_manager"],
    )


def _executable_inspector() -> ExecutableInspector:
    plugin = _plugin_manager().get(ExecutableInspector.identifier)
    if not isinstance(plugin, ExecutableInspector):
        abort(503)

    return plugin


def _available_license_manager() -> LicenseManager | None:
    plugin = _plugin_manager().get(LicenseManager.identifier)
    return plugin if isinstance(plugin, LicenseManager) else None


def _available_log_collector() -> LogCollector | None:
    plugin = _plugin_manager().get(LogCollector.identifier)
    return plugin if isinstance(plugin, LogCollector) else None


def _product_or_404(product_id: str) -> Product:
    product = _repository().get(product_id)
    if product is None:
        abort(404)
    return product


def _license_configuration(product_id: str) -> PluginConfiguration | None:
    return _configuration_repository().get(product_id, LicenseManager.identifier)


def _log_configuration(product_id: str) -> PluginConfiguration | None:
    return _configuration_repository().get(product_id, LogCollector.identifier)


def _backup_root() -> str | Path:
    return cast(str | Path, current_app.config["PLUGIN_BACKUP_ROOT"])


def _preview_license_action(product_id: str, action_identifier: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_license_manager()
    if plugin is None:
        return _render_product_detail(
            product, license_error="License Manager зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LicenseManager.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, license_error=storage_error
        ), 500
    validation_error = _license_configuration_validation_error(
        plugin, configuration
    )
    if validation_error:
        return _render_product_detail(
            product, license_error=validation_error
        ), 400
    plan = plugin.build_plan(
        product.id,
        configuration,
        action_identifier,
    )
    return _render_product_detail(product, change_plan=plan)


def _confirm_license_action(product_id: str, action_identifier: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_license_manager()
    if plugin is None:
        return _render_product_detail(
            product, license_error="License Manager зараз недоступний."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, LicenseManager.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, license_error=storage_error
        ), 500
    validation_error = _license_configuration_validation_error(
        plugin, configuration
    )
    if validation_error:
        return _render_product_detail(
            product, license_error=validation_error
        ), 400
    result = plugin.execute(
        product_id=product.id,
        configuration=configuration,
        action_identifier=action_identifier,
        expected_fingerprint=request.form.get("fingerprint", ""),
        confirmed=request.form.get("confirm") == "yes",
        backup_root=_backup_root(),
        operation_logs=_operation_logs(),
        logger=current_app.logger,
    )
    return _render_product_detail(product, operation_result=result)


def _render_product_detail(
    product: Product,
    **results: Any,
) -> str:
    configuration_error = None
    try:
        license_configuration = _license_configuration(product.id)
        log_configuration = _log_configuration(product.id)
    except Exception:
        current_app.logger.exception(
            "Could not read plugin configurations for product %s", product.id
        )
        license_configuration = None
        log_configuration = None
        configuration_error = (
            "Сховище конфігурацій плагінів недоступне або пошкоджене. "
            "Інші дані продукту залишаються доступними."
        )
    license_typed = None
    log_typed = None
    license_plugin = _available_license_manager()
    log_plugin = _available_log_collector()
    license_plugin_error = None
    log_plugin_error = None
    if license_plugin is None:
        license_plugin_error = "License Manager зараз недоступний."
    else:
        try:
            license_typed = license_plugin.typed_configuration(
                license_configuration
            )
        except ValueError:
            configuration_error = "Конфігурація License Manager некоректна."
    if log_plugin is None:
        log_plugin_error = "Log Collector зараз недоступний."
    else:
        try:
            if log_configuration is not None:
                log_typed = log_plugin.typed_configuration(log_configuration)
        except ValueError:
            configuration_error = "Конфігурація Log Collector некоректна."

    operation_log_error = None
    try:
        recent_operation_logs = _operation_logs().list_for_product(
            product.id, limit=5
        )
    except Exception:
        current_app.logger.exception(
            "Could not read operation logs for product %s", product.id
        )
        recent_operation_logs = []
        operation_log_error = (
            "Історія операцій QA Deck зараз недоступна. "
            "Інші секції працюють незалежно."
        )

    context: dict[str, object] = {
        "product": product,
        "inspection_result": None,
        "license_configuration": license_configuration,
        "license_typed": license_typed,
        "log_configuration": log_configuration,
        "log_typed": log_typed,
        "operation_logs": recent_operation_logs,
        "operation_log_error": operation_log_error,
        "configuration_error": configuration_error,
        "license_plugin_available": license_plugin is not None,
        "log_plugin_available": log_plugin is not None,
        "license_plugin_error": license_plugin_error,
        "log_plugin_error": log_plugin_error,
        "license_form": {},
        "log_form": {},
        "license_error": None,
        "log_error": None,
        "license_result": None,
        "change_plan": None,
        "operation_result": None,
        "backup_result": None,
        "log_result": None,
        "log_collection_result": None,
    }
    context.update(results)
    return render_template("products/detail.html", **context)


def _configuration_for_route(
    product_id: str,
    plugin_identifier: str,
) -> tuple[PluginConfiguration | None, str | None]:
    try:
        configuration = _configuration_repository().get(
            product_id, plugin_identifier
        )
    except Exception:
        current_app.logger.exception(
            "Could not read %s configuration for product %s",
            plugin_identifier,
            product_id,
        )
        return (
            None,
            "Сховище конфігурацій недоступне або пошкоджене. "
            "Операцію не виконано.",
        )
    return configuration, None


def _license_configuration_validation_error(
    plugin: LicenseManager,
    configuration: PluginConfiguration | None,
) -> str | None:
    if configuration is None:
        return None
    try:
        plugin.typed_configuration(configuration)
    except ValueError:
        current_app.logger.exception("Invalid License Manager configuration")
        return "Збережена конфігурація License Manager некоректна."
    return None


def _optional_text(value: str) -> str | None:
    stripped_value = value.strip()
    return stripped_value or None


def _positive_config_int(key: str) -> int:
    value = current_app.config[key]
    if type(value) is not int or value < 1:
        current_app.logger.error("Invalid positive integer config: %s", key)
        abort(500)
    return value


class _CleanupIterable:
    """Close a response stream before removing its temporary directory."""

    def __init__(self, iterable: Iterable[bytes], directory: Path) -> None:
        self._iterable = iterable
        self._directory = directory
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            yield from self._iterable
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._iterable, "close", None)
        try:
            if close is not None:
                close()
        finally:
            shutil.rmtree(self._directory, ignore_errors=True)


def _launch_arguments(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]
