"""Web routes for QA Deck."""

import json
import re
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
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

from qa_deck.domain import (
    EnvironmentProfile,
    EnvironmentProfileLicense,
    OperationLog,
    OperationStatus,
    PluginConfiguration,
    Product,
    ProductSetupBundle,
    ProductSetupPackage,
    ProfileLicenseState,
    RollbackStatus,
    Snapshot,
)
from qa_deck.environment_profiles import (
    EnvironmentProfileComparator,
    EnvironmentProfileExecutionPlanner,
    EnvironmentProfileExecutionResult,
    EnvironmentProfileExecutionStateStore,
    EnvironmentProfileExecutor,
)
from qa_deck.plugins import PluginAction, PluginManager
from qa_deck.plugins.builtin import (
    ExecutableInspector,
    LicenseManager,
    LogCollector,
    WindowsRegistry,
)
from qa_deck.plugins.builtin.windows_registry import (
    RegistryBranchState,
    RegistryChangePlan,
    RegistryDataType,
    RegistryExecutionEntryResult,
    RegistryExecutionResult,
    RegistryExecutionStateStore,
    RegistryInspectionResult,
    RegistryPlanOperation,
    RegistryPlanStatus,
    RegistryPreset,
    RegistryPresetBranch,
    RegistryPresetValue,
    RegistryRollbackStatus,
    RegistryValueState,
    WindowsRegistryConfiguration,
)
from qa_deck.product_deletion import ProductDeletionService
from qa_deck.product_setup import (
    ProductSetupImportReview,
    ProductSetupImportSource,
    ProductSetupImportStateStore,
    ProductSetupService,
    SetupProductAdaptation,
    SetupProductAdaptedValues,
    default_setup_adapted_values,
)
from qa_deck.snapshot import (
    RestorePlanStatus,
    SnapshotBuilder,
    SnapshotDiff,
    SnapshotDiffEntry,
    SnapshotDiffer,
    SnapshotDiffStatus,
    SnapshotRestoreExecutor,
    SnapshotRestorePlan,
    SnapshotRestorePlanner,
    SnapshotRestoreResult,
    SnapshotRestoreStateStore,
)
from qa_deck.storage import (
    EnvironmentProfileRepository,
    OperationLogRepository,
    PluginConfigurationRepository,
    ProductRepository,
    SnapshotRepository,
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


@dataclass(frozen=True, slots=True)
class PresentedJsonValue:
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class RegistryCurrentStateView:
    status: str
    primary: str
    secondary: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryPresetResourceView:
    display_name: str
    desired_state: str
    location: str
    value_name: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryPresetCardView:
    id: str
    name: str
    resources: tuple[RegistryPresetResourceView, ...]
    preset: RegistryPreset
    comparison: RegistryChangePlan | None = None


@dataclass(frozen=True, slots=True)
class SnapshotFieldChangeView:
    path: str
    base_value: PresentedJsonValue
    target_value: PresentedJsonValue


@dataclass(frozen=True, slots=True)
class SnapshotResourceDiffView:
    source: str
    source_display_name: str
    resource_type: str
    identifier: str
    identifier_display_name: str
    status: str
    field_changes: tuple[SnapshotFieldChangeView, ...]
    primary_state: PresentedJsonValue | None
    raw_base: PresentedJsonValue
    raw_target: PresentedJsonValue


@dataclass(frozen=True, slots=True)
class SnapshotSourceGroupView:
    source: str
    display_name: str
    resources: tuple[SnapshotResourceDiffView, ...]


@dataclass(frozen=True, slots=True)
class SnapshotStatusGroupView:
    status: str
    display_name: str
    count: int
    open_by_default: bool
    sources: tuple[SnapshotSourceGroupView, ...]


@dataclass(frozen=True, slots=True)
class SnapshotReferenceView:
    name: str
    created_at: datetime
    short_id: str
    is_current: bool


@dataclass(frozen=True, slots=True)
class SnapshotWarningView:
    origin: str
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SnapshotDiffView:
    base: SnapshotReferenceView
    target: SnapshotReferenceView
    changed_count: int
    added_count: int
    removed_count: int
    unchanged_count: int
    groups: tuple[SnapshotStatusGroupView, ...]
    warnings: tuple[SnapshotWarningView, ...]

    @property
    def warning_count(self) -> int:
        return sum(len(item.messages) for item in self.warnings)


@web_blueprint.get("/")
def index() -> Response:
    """Redirect to the product list."""
    return redirect(url_for("web.product_list"))


@web_blueprint.get("/products")
def product_list() -> str:
    """Show all stored products."""
    return render_template(
        "products/list.html",
        products=_repository().list_all(),
        export_error=None,
        deletion_success=request.args.get("deleted") == "1",
    )


@web_blueprint.route("/products/new", methods=["GET", "POST"])
def product_new() -> str | Response | tuple[str, int]:
    """Show the product form and store valid submissions."""
    if request.method == "GET":
        return render_template("products/new.html", error=None, form={})

    try:
        executable_path = _clean_input_path(
            _optional_text(request.form.get("executable_path", ""))
        )
        product = Product(
            id=str(uuid4()),
            name=(
                request.form.get("name", "").strip()
                or _derived_product_name(executable_path)
            ),
            description=request.form.get("description", "").strip(),
            executable_path=executable_path,
            working_directory=(
                _optional_text(request.form.get("working_directory", ""))
                or _derived_working_directory(executable_path)
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


@web_blueprint.get("/product-setup/import")
def product_setup_import() -> str:
    return render_template("product_setup/import.html", error=None)


@web_blueprint.post("/product-setup/import/configure")
def product_setup_import_configure() -> str | tuple[str, int]:
    upload = request.files.get("setup_file")
    if upload is None or not upload.filename:
        return render_template(
            "product_setup/import.html",
            error="Оберіть JSON-файл Product Setup.",
        ), 400
    limit = _positive_config_int("PRODUCT_SETUP_MAX_BYTES")
    payload = upload.stream.read(limit + 1)
    if len(payload) > limit:
        return render_template(
            "product_setup/import.html",
            error="Product Setup файл перевищує дозволений розмір.",
        ), 413
    try:
        data = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
        document_type, packages = _product_setup_document(data)
        source = _product_setup_import_state().create_source(
            packages, document_type
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        return render_template(
            "product_setup/import.html",
            error="Product Setup файл має некоректний тип, структуру або версію.",
        ), 400
    preparations = tuple(
        _product_setup_service().prepare_import(entry) for entry in source.entries
    )
    initial_values = tuple(
        default_setup_adapted_values(item) for item in preparations
    )
    review = _product_setup_service().review_import(
        source, initial_values, _repository().list_all()
    )
    return _render_setup_configuration(
        source,
        preparations,
        review,
        {},
        tuple(entry.index for entry in source.entries),
    )


@web_blueprint.post("/product-setup/import/configure/validate")
def product_setup_import_validate() -> str | tuple[str, int]:
    source = _product_setup_import_state().get_source(
        request.form.get("source_token", "")
    )
    if source is None:
        return _render_setup_import_error(
            "Налаштування імпорту застаріло. Завантажте файл ще раз.", 409
        )
    try:
        selected = _selected_setup_indices(source)
        all_preparations = tuple(
            _product_setup_service().prepare_import(entry)
            for entry in source.entries
        )
        selected_preparations = tuple(all_preparations[index] for index in selected)
        adaptations = _setup_adapted_values(selected_preparations)
        review = _product_setup_service().review_import(
            source,
            adaptations,
            _repository().list_all(),
        )
    except ValueError:
        return _render_setup_import_error("Дані локальної адаптації некоректні.", 400)
    if not review.can_confirm:
        return (
            _render_setup_configuration(
                source,
                all_preparations,
                review,
                request.form,
                selected,
            ),
            400,
        )
    intent = _product_setup_import_state().create_intent(source, adaptations)
    return _render_setup_configuration(
        source,
        all_preparations,
        review,
        request.form,
        selected,
        intent.token,
    )


@web_blueprint.post("/product-setup/import/confirm")
def product_setup_import_confirm() -> Response | tuple[str, int]:
    if request.form.get("confirm") != "yes":
        return _render_setup_import_error("Підтвердження імпорту не отримано.", 400)
    intent = _product_setup_import_state().take_intent(
        request.form.get("confirmation_token", "")
    )
    if intent is None:
        return _render_setup_import_error(
            "Підтвердження застаріло або вже було використане.", 409
        )
    result = _product_setup_service().execute_import(intent, _repository())
    result_id = _product_setup_import_state().save_result(result)
    return redirect(url_for("web.product_setup_import_result", result_id=result_id))


@web_blueprint.get("/product-setup/import/results/<result_id>")
def product_setup_import_result(result_id: str) -> str:
    result = _product_setup_import_state().get_result(result_id)
    if result is None:
        abort(404)
    return render_template("product_setup/result.html", result=result)


@web_blueprint.post("/products/setup/export")
def export_product_setup_bundle() -> Response | tuple[str, int]:
    products = _repository().list_all()
    selected_ids = request.form.getlist("product_ids")
    selected_id_set = set(selected_ids)
    selected = [product for product in products if product.id in selected_id_set]
    if (
        not selected_ids
        or len(selected_ids) != len(selected_id_set)
        or len(selected) != len(selected_id_set)
    ):
        return (
            render_template(
                "products/list.html",
                products=products,
                export_error="Оберіть щонайменше один коректний Product.",
            ),
            400,
        )
    try:
        service = _product_setup_service()
        bundle = ProductSetupBundle(
            tuple(service.export(product) for product in selected)
        )
        payload = _product_setup_json(bundle.to_dict())
    except Exception:
        current_app.logger.exception("Could not export Product Setup Bundle")
        return (
            render_template(
                "products/list.html",
                products=products,
                export_error=(
                    "Не вдалося безпечно створити Setup Bundle. "
                    "Перевірте унікальність назв Product і повторіть спробу."
                ),
            ),
            503,
        )
    response = Response(payload, content_type="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = (
        'attachment; filename="qa-deck-setup-bundle.json"'
    )
    return response


@web_blueprint.get("/products/<product_id>")
def product_detail(product_id: str) -> str:
    """Show one product or return 404 when it is missing."""
    return _render_product_detail(_product_or_404(product_id))


@web_blueprint.post("/products/<product_id>/delete")
def delete_product(product_id: str) -> str | Response | tuple[str, int]:
    product = _repository().get(product_id)
    if product is None:
        abort(404)
    if request.form.get("confirm") != "yes":
        return (
            _render_product_detail(
                product,
                deletion_error="Підтвердження видалення не отримано.",
            ),
            400,
        )
    result = _product_deletion_service().delete(product_id)
    if not result.succeeded:
        if result.status == "not_found":
            abort(404)
        return _render_product_detail(product, deletion_error=result.message), 503
    return redirect(url_for("web.product_list", deleted="1"))


@web_blueprint.get("/products/<product_id>/setup/export")
def export_product_setup(product_id: str) -> Response | tuple[str, int]:
    product = _product_or_404(product_id)
    try:
        package = _product_setup_service().export(product)
        payload = _product_setup_json(package.to_dict())
    except Exception:
        current_app.logger.exception(
            "Could not export Product Setup for product %s", product.id
        )
        return (
            _render_product_detail(
                product,
                product_setup_export_error=(
                    "Не вдалося створити файл налаштувань Product Setup. "
                    "Перевірте конфігурації плагінів і повторіть спробу."
                ),
            ),
            503,
        )
    response = Response(payload, content_type="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{_setup_filename(product.name)}"'
    )
    return response


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
            open="license-manager-settings",
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
    return _render_product_detail(
        product,
        license_result=result,
        open_workspace="license-manager",
    )


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
    return _render_product_detail(
        product,
        backup_result=result,
        open_workspace="license-manager",
    )


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
            open="log-collector-settings",
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
    return _render_product_detail(
        product,
        log_result=result,
        open_workspace="log-collector",
    )


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/configuration"
)
def save_windows_registry_configuration(product_id: str) -> str | Response:
    """Validate and save read-only Registry settings for one product."""
    product = _product_or_404(product_id)
    plugin = _available_windows_registry()
    if plugin is None:
        return _render_product_detail(
            product, registry_error="Windows Registry is unavailable."
        ), 503
    try:
        configuration = plugin.create_configuration(
            product_id=product.id,
            enabled=request.form.get("enabled") == "on",
            value_targets_json=request.form.get("value_targets", ""),
            branch_targets_json=request.form.get("branch_targets", ""),
            presets_json=request.form.get("presets", ""),
        )
        _configuration_repository().upsert(configuration)
    except ValueError as error:
        return _render_product_detail(
            product, registry_form=request.form, registry_error=str(error)
        ), 400
    except Exception:
        current_app.logger.exception("Could not save Registry configuration")
        return _render_product_detail(
            product,
            registry_form=request.form,
            registry_error="Could not save Windows Registry settings.",
        ), 500
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="windows-registry",
            _anchor="windows-registry",
        )
    )


@web_blueprint.post("/products/<product_id>/plugins/windows-registry/inspect")
def inspect_windows_registry(product_id: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_windows_registry()
    if plugin is None:
        return _render_product_detail(
            product, registry_error="Windows Registry is unavailable."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, WindowsRegistry.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, registry_error=storage_error
        ), 500
    try:
        result = plugin.inspect(configuration)
    except ValueError as error:
        return _render_product_detail(
            product, registry_error=str(error)
        ), 400
    return _render_product_detail(product, registry_result=result)


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/presets/preview"
)
def preview_windows_registry_preset(product_id: str) -> str:
    product = _product_or_404(product_id)
    plugin = _available_windows_registry()
    if plugin is None:
        return _render_product_detail(
            product, registry_error="Windows Registry is unavailable."
        ), 503
    configuration, storage_error = _configuration_for_route(
        product.id, WindowsRegistry.identifier
    )
    if storage_error:
        return _render_product_detail(
            product, registry_error=storage_error
        ), 500
    try:
        preview = plugin.preview_preset(
            configuration, request.form.get("preset_id", "")
        )
        typed = plugin.typed_configuration(configuration)
        if typed is None:
            raise ValueError("Windows Registry is not configured")
        executable_count = sum(
            entry.status is RegistryPlanStatus.READY
            and entry.operation
            in {
                RegistryPlanOperation.SET_VALUE,
                RegistryPlanOperation.HIDE_BRANCH,
                RegistryPlanOperation.RESTORE_BRANCH,
            }
            for entry in preview.entries
        )
        unsupported_count = 0
        intent = (
            _registry_execution_state().create_intent(
                product.id, typed, preview
            )
            if executable_count
            else None
        )
    except ValueError as error:
        return _render_product_detail(
            product, registry_error=str(error)
        ), 400
    return _render_product_detail(
        product,
        registry_preset_preview=preview,
        registry_execution_token=intent.token if intent else None,
        registry_executable_count=executable_count,
        registry_unsupported_count=unsupported_count,
    )


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/presets/<preset_id>/apply"
)
def execute_windows_registry_preset(product_id: str, preset_id: str) -> Response:
    """Consume one intent and execute a freshly reconstructed Registry plan."""
    product = _product_or_404(product_id)
    if request.form.get("confirm") != "yes":
        abort(400)
    intent = _registry_execution_state().take_intent(
        request.form.get("execution_token", ""), product.id, preset_id
    )
    if intent is None:
        result = RegistryExecutionResult(
            product.id,
            preset_id,
            "Registry preset",
            datetime.now(UTC),
            warnings=(
                "Registry confirmation is invalid, expired, or already used. "
                "No changes were made.",
            ),
        )
    else:
        plugin = _available_windows_registry()
        configuration, storage_error = _configuration_for_route(
            product.id, WindowsRegistry.identifier
        )
        if plugin is None or storage_error:
            result = RegistryExecutionResult(
                product.id,
                intent.preset_id,
                intent.preset_name,
                datetime.now(UTC),
                warnings=(
                    storage_error or "Windows Registry is unavailable.",
                ),
            )
        else:
            try:
                result = plugin.execute_preset(configuration, intent)
            except ValueError:
                result = RegistryExecutionResult(
                    product.id,
                    intent.preset_id,
                    intent.preset_name,
                    datetime.now(UTC),
                    warnings=(
                        "Конфігурація Registry змінилася після перевірки. "
                        "Зміни не виконано.",
                    ),
                )
        result = _log_registry_execution(result)
    result_id = _registry_execution_state().save_result(result)
    return redirect(
        url_for(
            "web.windows_registry_execution_result",
            product_id=product.id,
            result_id=result_id,
            _anchor="windows-registry",
        )
    )


@web_blueprint.get(
    "/products/<product_id>/plugins/windows-registry/results/<result_id>"
)
def windows_registry_execution_result(product_id: str, result_id: str) -> str:
    product = _product_or_404(product_id)
    result = _registry_execution_state().get_result(result_id, product.id)
    if result is None:
        abort(404)
    return _render_product_detail(product, registry_execution_result=result)


@web_blueprint.post("/products/<product_id>/environment-profiles")
def create_environment_profile(product_id: str) -> str | Response:
    product = _product_or_404(product_id)
    try:
        profiles = _environment_profile_repository().list_for_product(product.id)
        profile_id = _environment_profile_id(
            request.form.get("name", ""), profiles
        )
        profile = _environment_profile_from_form(product, profile_id)
        _environment_profile_repository().add(profile)
    except ValueError as error:
        return _render_product_detail(
            product,
            environment_profile_error=str(error),
            environment_profile_form=request.form,
        ), 400
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="environment-profiles",
            _anchor="environment-profiles",
        )
    )


@web_blueprint.post(
    "/products/<product_id>/environment-profiles/<profile_id>"
)
def update_environment_profile(
    product_id: str, profile_id: str
) -> str | Response:
    product = _product_or_404(product_id)
    existing = _environment_profile_repository().get(product.id, profile_id)
    if existing is None:
        abort(404)
    try:
        profile = _environment_profile_from_form(product, existing.id)
        _environment_profile_repository().update(profile)
    except ValueError as error:
        return _render_product_detail(
            product,
            environment_profile_error=str(error),
            environment_profile_form=request.form,
            environment_profile_editing_id=existing.id,
        ), 400
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="environment-profiles",
            _anchor="environment-profiles",
        )
    )


@web_blueprint.post(
    "/products/<product_id>/environment-profiles/<profile_id>/delete"
)
def delete_environment_profile(product_id: str, profile_id: str) -> Response:
    product = _product_or_404(product_id)
    if request.form.get("confirm") != "yes":
        abort(400)
    removed = _environment_profile_repository().remove(product.id, profile_id)
    if removed is None:
        abort(404)
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="environment-profiles",
            _anchor="environment-profiles",
        )
    )


@web_blueprint.post(
    "/products/<product_id>/environment-profiles/<profile_id>/apply-preview"
)
def prepare_environment_profile_execution(
    product_id: str, profile_id: str
) -> str:
    """Build a fresh server-side plan without mutating runtime state."""
    product = _product_or_404(product_id)
    profile = _environment_profile_repository().get(product.id, profile_id)
    if profile is None:
        abort(404)
    plan = _environment_profile_execution_planner().prepare(product, profile)
    intent = _environment_profile_execution_state().create_intent(profile, plan)
    return _render_product_detail(
        product,
        environment_profile_execution_plan=plan,
        environment_profile_execution_token=intent.token,
    )


@web_blueprint.post(
    "/products/<product_id>/environment-profiles/<profile_id>/apply"
)
def execute_environment_profile(product_id: str, profile_id: str) -> Response:
    """Consume one opaque confirmation and reconstruct authority server-side."""
    product = _product_or_404(product_id)
    if request.form.get("confirm") != "yes":
        abort(400)
    intent = _environment_profile_execution_state().take_intent(
        request.form.get("confirmation_token", ""), product.id, profile_id
    )
    if intent is None:
        result = EnvironmentProfileExecutionResult(
            product.id,
            profile_id,
            "Environment Profile",
            datetime.now(UTC),
            warnings=(
                "Підтвердження недійсне, прострочене або вже використане. "
                "Жодних змін не виконано.",
            ),
        )
    else:
        result = _environment_profile_executor().execute(product, intent)
    result_id = _environment_profile_execution_state().save_result(result)
    return redirect(
        url_for(
            "web.environment_profile_execution_result",
            product_id=product.id,
            profile_id=profile_id,
            result_id=result_id,
            _anchor="environment-profile-result",
        )
    )


@web_blueprint.get(
    "/products/<product_id>/environment-profiles/<profile_id>/results/<result_id>"
)
def environment_profile_execution_result(
    product_id: str, profile_id: str, result_id: str
) -> str:
    """Show a PRG result while comparison still reads fresh runtime state."""
    product = _product_or_404(product_id)
    result = _environment_profile_execution_state().get_result(
        result_id, product.id, profile_id
    )
    if result is None:
        abort(404)
    return _render_product_detail(
        product, environment_profile_execution_result=result
    )


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/value-targets"
)
def edit_registry_value_target(product_id: str) -> str | Response:
    product = _product_or_404(product_id)
    return _edit_registry_target(product, "value")


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/branch-targets"
)
def edit_registry_branch_target(product_id: str) -> str | Response:
    product = _product_or_404(product_id)
    return _edit_registry_target(product, "branch")


@web_blueprint.post(
    "/products/<product_id>/plugins/windows-registry/presets"
)
def edit_registry_preset(product_id: str) -> str | Response:
    product = _product_or_404(product_id)
    try:
        settings, enabled = _registry_settings(product.id)
        presets = cast(list[object], settings["presets"])
        original_id = request.form.get("original_id", "")
        action = request.form.get("action", "save")
        if action == "delete":
            updated = [
                item
                for item in presets
                if isinstance(item, dict) and item.get("id") != original_id
            ]
            if len(updated) == len(presets):
                abort(404)
            settings["presets"] = updated
        else:
            preset_id = original_id or _registry_preset_id(
                request.form.get("name", ""), presets
            )
            preset = _registry_preset_from_form(product.id, preset_id)
            settings["presets"] = _replace_registry_item(
                presets, original_id, preset
            )
        _save_registry_settings(product.id, enabled, settings)
    except ValueError as error:
        return _render_product_detail(
            product, registry_error=str(error), registry_form=request.form
        ), 400
    except OSError:
        current_app.logger.exception("Could not update Registry preset")
        return _render_product_detail(
            product,
            registry_error="Не вдалося зберегти налаштування Registry.",
        ), 500
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="windows-registry",
            _anchor="windows-registry",
        )
    )


@web_blueprint.post("/products/<product_id>/snapshots")
def create_snapshot(product_id: str) -> str | Response | tuple[str, int]:
    product = _product_or_404(product_id)
    label_value = request.form.get("label", "")
    try:
        label = _normalize_snapshot_label(label_value)
    except ValueError as error:
        return (
            _render_product_detail(
                product,
                snapshot_error=str(error),
                snapshot_form_label=label_value,
            ),
            400,
        )

    builder = _snapshot_builder()
    try:
        snapshot = builder.build_snapshot(product, label=label)
        _snapshot_repository().add(snapshot)
    except ValueError as error:
        return (
            _render_product_detail(
                product,
                snapshot_error=str(error),
                snapshot_form_label=label_value,
            ),
            400,
        )
    except Exception:
        current_app.logger.exception(
            "Could not save snapshot for product %s", product.id
        )
        return (
            _render_product_detail(
                product,
                snapshot_error=(
                    "Не вдалося зберегти snapshot. "
                    "Сховище недоступне або ушкоджене."
                ),
                snapshot_form_label=label_value,
            ),
            500,
        )

    try:
        _append_snapshot_operation_log(product.id, snapshot)
    except Exception:
        current_app.logger.exception(
            "Snapshot %s was saved, but its operation log could not be written",
            snapshot.id,
        )
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="snapshots",
            _anchor="snapshots",
        )
    )


@web_blueprint.post("/products/<product_id>/snapshots/<snapshot_id>/delete")
def delete_snapshot(product_id: str, snapshot_id: str) -> Response:
    """Delete one persisted Snapshot after explicit server-checked confirmation."""
    product = _product_or_404(product_id)
    snapshot = _snapshot_repository().get(snapshot_id)
    if snapshot is None or snapshot.product_id != product.id:
        abort(404)
    if request.form.get("confirm") != "yes":
        abort(400)
    removed = _snapshot_repository().remove(product.id, snapshot.id)
    if removed is None:
        abort(404)
    try:
        _append_snapshot_delete_operation_log(product.id, removed.id)
    except Exception:
        current_app.logger.exception(
            "Snapshot %s was deleted, but its operation log could not be written",
            removed.id,
        )
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="snapshots",
            _anchor="snapshots",
        )
    )


@web_blueprint.get("/products/<product_id>/snapshots/<snapshot_id>/compare/current")
def compare_snapshot_with_current(product_id: str, snapshot_id: str) -> str:
    product = _product_or_404(product_id)
    snapshot = _snapshot_repository().get(snapshot_id)
    if snapshot is None or snapshot.product_id != product.id:
        abort(404)

    current_snapshot = _snapshot_builder().build_snapshot(product)
    diff = _snapshot_differ().diff(snapshot, current_snapshot)
    return _render_product_detail(
        product,
        snapshot_diff=diff,
        snapshot_diff_base=snapshot,
        snapshot_diff_target=current_snapshot,
        snapshot_diff_target_is_current=True,
    )


@web_blueprint.get(
    "/products/<product_id>/snapshots/<snapshot_id>/restore-plan"
)
def prepare_snapshot_restore(product_id: str, snapshot_id: str) -> str:
    """Prepare a read-only plan from current state to a persisted snapshot."""
    product = _product_or_404(product_id)
    snapshot = _snapshot_repository().get(snapshot_id)
    if snapshot is None or snapshot.product_id != product.id:
        abort(404)

    restore_plan = _snapshot_restore_planner().prepare(product, snapshot)
    restore_executable_count = _restore_executable_count(restore_plan)
    restore_intent = (
        _snapshot_restore_state().create_intent(restore_plan)
        if restore_executable_count > 0
        else None
    )
    return _render_product_detail(
        product,
        restore_plan=restore_plan,
        restore_plan_snapshot=snapshot,
        restore_confirmation_token=(
            restore_intent.token if restore_intent is not None else None
        ),
        restore_executable_count=restore_executable_count,
        restore_source_names=_snapshot_source_display_names(),
    )


@web_blueprint.post(
    "/products/<product_id>/snapshots/<snapshot_id>/restore"
)
def execute_snapshot_restore(product_id: str, snapshot_id: str) -> Response:
    """Consume one confirmation and execute a server-reconstructed plan."""
    product = _product_or_404(product_id)
    snapshot = _snapshot_repository().get(snapshot_id)
    if snapshot is None or snapshot.product_id != product.id:
        abort(404)
    if request.form.get("confirm") != "yes":
        abort(400)

    intent = _snapshot_restore_state().take_intent(
        request.form.get("confirmation_token", ""),
        product.id,
        snapshot.id,
    )
    if intent is None:
        result = SnapshotRestoreResult(
            product_id=product.id,
            snapshot_id=snapshot.id,
            created_at=datetime.now(UTC),
            warnings=(
                "Restore confirmation is invalid, expired, or already used. "
                "No changes were made.",
            ),
        )
    else:
        result = _snapshot_restore_executor().execute(
            product,
            snapshot,
            intent,
        )
    result_id = _snapshot_restore_state().save_result(result)
    return redirect(
        url_for(
            "web.snapshot_restore_result",
            product_id=product.id,
            snapshot_id=snapshot.id,
            result_id=result_id,
            _anchor="snapshot-restore-result",
        )
    )


@web_blueprint.get(
    "/products/<product_id>/snapshots/<snapshot_id>/restore-results/<result_id>"
)
def snapshot_restore_result(
    product_id: str,
    snapshot_id: str,
    result_id: str,
) -> str:
    """Show a previous restore result without repeating execution."""
    product = _product_or_404(product_id)
    snapshot = _snapshot_repository().get(snapshot_id)
    if snapshot is None or snapshot.product_id != product.id:
        abort(404)
    result = _snapshot_restore_state().get_result(
        result_id,
        product.id,
        snapshot.id,
    )
    if result is None:
        abort(404)
    return _render_product_detail(
        product,
        restore_result=result,
        restore_result_snapshot=snapshot,
        restore_source_names=_snapshot_source_display_names(),
    )


@web_blueprint.get("/products/<product_id>/snapshots/compare")
def compare_snapshots(product_id: str) -> str | tuple[str, int]:
    product = _product_or_404(product_id)
    base_snapshot_id = request.args.get("base")
    target_snapshot_id = request.args.get("target")
    if not base_snapshot_id or not target_snapshot_id:
        error_message = (
            "Both base and target snapshot IDs are required for comparison."
        )
        return (
            _render_product_detail(
                product,
                snapshot_error=error_message,
            ),
            400,
        )

    if base_snapshot_id == target_snapshot_id:
        return (
            _render_product_detail(
                product,
                snapshot_error="Неможливо порівняти snapshot із самим собою.",
            ),
            400,
        )

    base_snapshot = _snapshot_repository().get(base_snapshot_id)
    target_snapshot = _snapshot_repository().get(target_snapshot_id)
    if (
        base_snapshot is None
        or target_snapshot is None
        or base_snapshot.product_id != product.id
        or target_snapshot.product_id != product.id
    ):
        abort(404)

    diff = _snapshot_differ().diff(base_snapshot, target_snapshot)
    return _render_product_detail(
        product,
        snapshot_diff=diff,
        snapshot_diff_base=base_snapshot,
        snapshot_diff_target=target_snapshot,
    )


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
            product,
            log_collection_result=result,
            open_workspace="log-collector",
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


def _environment_profile_repository() -> EnvironmentProfileRepository:
    return cast(
        EnvironmentProfileRepository,
        current_app.extensions["environment_profile_repository"],
    )


def _plugin_manager() -> PluginManager:
    return cast(
        PluginManager,
        current_app.extensions["plugin_manager"],
    )


def _product_setup_service() -> ProductSetupService:
    return ProductSetupService(
        _plugin_manager(),
        _configuration_repository(),
        current_app.logger,
    )


def _product_deletion_service() -> ProductDeletionService:
    return ProductDeletionService(
        _repository(),
        _configuration_repository(),
        _snapshot_repository(),
        _environment_profile_repository(),
        current_app.logger,
    )


def _product_setup_import_state() -> ProductSetupImportStateStore:
    return cast(
        ProductSetupImportStateStore,
        current_app.extensions["product_setup_import_state"],
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


def _available_windows_registry() -> WindowsRegistry | None:
    plugin = _plugin_manager().get(WindowsRegistry.identifier)
    return plugin if isinstance(plugin, WindowsRegistry) else None


def _product_or_404(product_id: str) -> Product:
    product = _repository().get(product_id)
    if product is None:
        abort(404)
    return product


def _license_configuration(product_id: str) -> PluginConfiguration | None:
    return _configuration_repository().get(product_id, LicenseManager.identifier)


def _log_configuration(product_id: str) -> PluginConfiguration | None:
    return _configuration_repository().get(product_id, LogCollector.identifier)


def _registry_configuration(product_id: str) -> PluginConfiguration | None:
    return _configuration_repository().get(product_id, WindowsRegistry.identifier)


def _registry_settings(product_id: str) -> tuple[dict[str, object], bool]:
    configuration = _registry_configuration(product_id)
    if configuration is None:
        return {"value_targets": [], "branch_targets": [], "presets": []}, True
    typed = WindowsRegistry().typed_configuration(configuration)
    if typed is None:  # pragma: no cover - configuration is present
        raise ValueError("Windows Registry configuration is missing")
    return typed.to_settings(), configuration.enabled


def _save_registry_settings(
    product_id: str,
    enabled: bool,
    settings: dict[str, object],
) -> None:
    plugin = _available_windows_registry()
    if plugin is None:
        raise ValueError("Windows Registry is unavailable")
    configuration = plugin.create_configuration(
        product_id=product_id,
        enabled=enabled,
        value_targets_json=json.dumps(settings["value_targets"]),
        branch_targets_json=json.dumps(settings["branch_targets"]),
        presets_json=json.dumps(settings["presets"]),
    )
    _configuration_repository().upsert(configuration)


def _edit_registry_target(product: Product, kind: str) -> str | Response:
    try:
        settings, enabled = _registry_settings(product.id)
        key = "value_targets" if kind == "value" else "branch_targets"
        targets = cast(list[object], settings[key])
        original_id = request.form.get("original_id", "")
        action = request.form.get("action", "save")
        if action == "delete":
            updated = [
                item
                for item in targets
                if isinstance(item, dict) and item.get("id") != original_id
            ]
            if len(updated) == len(targets):
                abort(404)
            settings[key] = updated
        else:
            target: dict[str, object] = {
                "id": request.form.get("id", ""),
                "display_name": request.form.get("display_name", "").strip()
                or None,
                "hive": request.form.get("hive", ""),
                "key_path": request.form.get("key_path", ""),
                "enabled": request.form.get("enabled") == "on",
            }
            if kind == "value":
                target["value_name"] = request.form.get("value_name", "")
            settings[key] = _replace_registry_item(
                targets, original_id, target
            )
        _save_registry_settings(product.id, enabled, settings)
    except ValueError as error:
        return _render_product_detail(
            product, registry_error=str(error), registry_form=request.form
        ), 400
    except OSError:
        current_app.logger.exception("Could not update Registry target")
        return _render_product_detail(
            product,
            registry_error="Не вдалося зберегти налаштування Registry.",
        ), 500
    return redirect(
        url_for(
            "web.product_detail",
            product_id=product.id,
            open="windows-registry",
            _anchor="windows-registry",
        )
    )


def _replace_registry_item(
    items: list[object],
    original_id: str,
    replacement: dict[str, object],
) -> list[object]:
    if not original_id:
        return [*items, replacement]
    found = False
    updated: list[object] = []
    for item in items:
        if isinstance(item, dict) and item.get("id") == original_id:
            updated.append(replacement)
            found = True
        else:
            updated.append(item)
    if not found:
        raise ValueError("Configured Registry item does not exist")
    return updated


def _registry_preset_from_form(
    product_id: str,
    preset_id: str,
) -> dict[str, object]:
    configuration = _registry_configuration(product_id)
    typed = WindowsRegistry().typed_configuration(configuration)
    if typed is None:
        raise ValueError("Configure Registry targets before creating a preset")
    values: list[dict[str, object]] = []
    branches: list[dict[str, object]] = []
    for target in typed.value_targets:
        if request.form.get(f"include_value__{target.id}") != "on":
            continue
        type_name = request.form.get(f"value_type__{target.id}", "")
        try:
            registry_type = RegistryDataType(type_name)
        except ValueError as error:
            raise ValueError("Registry preset value type is unsupported") from error
        raw_value = request.form.get(f"value_data__{target.id}", "")
        values.append(
            {
                "target_id": target.id,
                "registry_type": registry_type.value,
                "value": _registry_form_value(registry_type, raw_value),
            }
        )
    for target in typed.branch_targets:
        if request.form.get(f"include_branch__{target.id}") == "on":
            branches.append(
                {
                    "target_id": target.id,
                    "visibility": request.form.get(
                        f"branch_visibility__{target.id}", "visible"
                    ),
                }
            )
    if not values and not branches:
        raise ValueError("Включіть у preset хоча б один Registry target")
    return {
        "id": preset_id,
        "name": request.form.get("name", ""),
        "values": values,
        "branches": branches,
    }


def _registry_preset_id(name: str, presets: list[object]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "preset"
    base = base[:56].rstrip("-")
    existing = {
        str(item.get("id", "")).casefold()
        for item in presets
        if isinstance(item, dict)
    }
    candidate = base
    suffix = 2
    while candidate.casefold() in existing:
        candidate = f"{base[: 63 - len(str(suffix))]}-{suffix}"
        suffix += 1
    return candidate


def _environment_profile_id(
    name: str,
    profiles: list[EnvironmentProfile],
) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "profile"
    base = base[:56].rstrip("-")
    existing = {profile.id.casefold() for profile in profiles}
    candidate = base
    suffix = 2
    while candidate.casefold() in existing:
        candidate = f"{base[: 63 - len(str(suffix))]}-{suffix}"
        suffix += 1
    return candidate


def _environment_profile_from_form(
    product: Product,
    profile_id: str,
) -> EnvironmentProfile:
    registry_preset_id = request.form.get("registry_preset_id", "").strip()
    registry_plugin = _available_windows_registry()
    registry_typed = (
        registry_plugin.typed_configuration(_registry_configuration(product.id))
        if registry_plugin is not None
        else None
    )
    if registry_preset_id and (
        registry_typed is None
        or not any(
            preset.id == registry_preset_id for preset in registry_typed.presets
        )
    ):
        raise ValueError("Вибраний Registry preset більше не налаштований")

    license_plugin = _available_license_manager()
    license_typed = (
        license_plugin.typed_configuration(_license_configuration(product.id))
        if license_plugin is not None
        else None
    )
    license_states: list[EnvironmentProfileLicense] = []
    for filename in license_typed.license_files if license_typed else ():
        if request.form.get(f"include_license__{filename}") != "on":
            continue
        try:
            desired_state = ProfileLicenseState(
                request.form.get(f"license_state__{filename}", "")
            )
        except ValueError as error:
            raise ValueError("Бажаний стан ліцензії некоректний") from error
        license_states.append(
            EnvironmentProfileLicense(filename, desired_state)
        )
    return EnvironmentProfile(
        id=profile_id,
        product_id=product.id,
        name=request.form.get("name", ""),
        registry_preset_id=registry_preset_id or None,
        license_states=tuple(license_states),
    )


def _registry_form_value(
    registry_type: RegistryDataType,
    raw_value: str,
) -> object:
    if registry_type in {RegistryDataType.REG_DWORD, RegistryDataType.REG_QWORD}:
        try:
            return int(raw_value)
        except ValueError as error:
            raise ValueError(
                f"{registry_type.value} requires an unsigned integer"
            ) from error
    if registry_type is RegistryDataType.REG_MULTI_SZ:
        return raw_value.splitlines()
    return raw_value


def _snapshot_repository() -> SnapshotRepository:
    return cast(
        SnapshotRepository,
        current_app.extensions["snapshot_repository"],
    )


def _snapshot_builder() -> SnapshotBuilder:
    return SnapshotBuilder(
        _plugin_manager(),
        _configuration_repository(),
        current_app.logger,
    )


def _snapshot_differ() -> SnapshotDiffer:
    return SnapshotDiffer()


def _snapshot_restore_planner() -> SnapshotRestorePlanner:
    return SnapshotRestorePlanner(
        _snapshot_builder(),
        _snapshot_differ(),
        _plugin_manager(),
        _configuration_repository(),
        current_app.logger,
    )


def _snapshot_restore_executor() -> SnapshotRestoreExecutor:
    return SnapshotRestoreExecutor(
        _snapshot_restore_planner(),
        _plugin_manager(),
        _configuration_repository(),
        _backup_root(),
        _operation_logs(),
        current_app.logger,
    )


def _snapshot_restore_state() -> SnapshotRestoreStateStore:
    return cast(
        SnapshotRestoreStateStore,
        current_app.extensions["snapshot_restore_state"],
    )


def _registry_execution_state() -> RegistryExecutionStateStore:
    return cast(
        RegistryExecutionStateStore,
        current_app.extensions["registry_execution_state"],
    )


def _environment_profile_execution_planner() -> EnvironmentProfileExecutionPlanner:
    return EnvironmentProfileExecutionPlanner(
        _plugin_manager(), _configuration_repository(), current_app.logger
    )


def _environment_profile_executor() -> EnvironmentProfileExecutor:
    return EnvironmentProfileExecutor(
        _environment_profile_execution_planner(),
        _plugin_manager(),
        _configuration_repository(),
        _environment_profile_repository(),
        _backup_root(),
        _operation_logs(),
        current_app.logger,
    )


def _environment_profile_execution_state() -> EnvironmentProfileExecutionStateStore:
    return cast(
        EnvironmentProfileExecutionStateStore,
        current_app.extensions["environment_profile_execution_state"],
    )


def _restore_executable_count(plan: SnapshotRestorePlan) -> int:
    count = 0
    for entry in plan.entries:
        if entry.status is not RestorePlanStatus.READY or not entry.fingerprint:
            continue
        plugin = _plugin_manager().get(entry.source)
        if callable(getattr(plugin, "execute_restore", None)):
            count += 1
    return count


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
    return _render_product_detail(
        product,
        change_plan=plan,
        open_workspace="license-manager",
    )


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
    return _render_product_detail(
        product,
        operation_result=result,
        open_workspace="license-manager",
    )


def _render_product_detail(
    product: Product,
    **results: Any,
) -> str:
    selected_snapshot_id = request.args.get("snapshot", "")
    if selected_snapshot_id and "snapshot_diff" not in results:
        selected_snapshot = _snapshot_repository().get(selected_snapshot_id)
        if selected_snapshot is None or selected_snapshot.product_id != product.id:
            abort(404)
        current_snapshot = _snapshot_builder().build_snapshot(product)
        results.update(
            snapshot_diff=_snapshot_differ().diff(
                selected_snapshot,
                current_snapshot,
            ),
            snapshot_diff_base=selected_snapshot,
            snapshot_diff_target=current_snapshot,
            snapshot_diff_target_is_current=True,
        )
    snapshot_diff = results.get("snapshot_diff")
    snapshot_diff_base = results.get("snapshot_diff_base")
    snapshot_diff_target = results.get("snapshot_diff_target")
    if (
        isinstance(snapshot_diff, SnapshotDiff)
        and isinstance(snapshot_diff_base, Snapshot)
        and isinstance(snapshot_diff_target, Snapshot)
    ):
        results["snapshot_diff_view"] = _snapshot_diff_view(
            snapshot_diff,
            snapshot_diff_base,
            snapshot_diff_target,
            target_is_current=bool(
                results.get("snapshot_diff_target_is_current", False)
            ),
        )

    license_configuration = None
    log_configuration = None
    registry_configuration = None
    license_configuration_error = None
    log_configuration_error = None
    registry_configuration_error = None
    try:
        license_configuration = _license_configuration(product.id)
    except Exception:
        current_app.logger.exception(
            "Could not read License Manager configuration for product %s",
            product.id,
        )
        license_configuration_error = (
            "Не вдалося завантажити конфігурацію License Manager. "
            "Інші інструменти залишаються доступними."
        )
    try:
        log_configuration = _log_configuration(product.id)
    except Exception:
        current_app.logger.exception(
            "Could not read Log Collector configuration for product %s",
            product.id,
        )
        log_configuration_error = (
            "Не вдалося завантажити конфігурацію Log Collector. "
            "Інші інструменти залишаються доступними."
        )
    try:
        registry_configuration = _registry_configuration(product.id)
    except Exception:
        current_app.logger.exception(
            "Could not read Windows Registry configuration for product %s",
            product.id,
        )
        registry_configuration_error = (
            "Не вдалося завантажити конфігурацію Windows Registry. "
            "Інші інструменти залишаються доступними."
        )
    license_typed = None
    log_typed = None
    registry_typed = None
    license_plugin = _available_license_manager()
    log_plugin = _available_log_collector()
    registry_plugin = _available_windows_registry()
    license_plugin_error = None
    log_plugin_error = None
    registry_plugin_error = None
    if license_plugin is None:
        license_plugin_error = "License Manager зараз недоступний."
    else:
        try:
            license_typed = license_plugin.typed_configuration(
                license_configuration
            )
        except ValueError:
            license_configuration_error = (
                "Конфігурація License Manager некоректна."
            )
    if log_plugin is None:
        log_plugin_error = "Log Collector зараз недоступний."
    else:
        try:
            if log_configuration is not None:
                log_typed = log_plugin.typed_configuration(log_configuration)
        except ValueError:
            log_configuration_error = "Конфігурація Log Collector некоректна."

    if registry_plugin is None:
        registry_plugin_error = "Windows Registry is unavailable."
    else:
        try:
            registry_typed = registry_plugin.typed_configuration(
                registry_configuration
            )
        except ValueError:
            registry_configuration_error = (
                "Конфігурація Windows Registry некоректна."
            )

    registry_inspection = results.get("registry_result")
    registry_comparisons: dict[str, RegistryChangePlan] = {}
    if (
        registry_plugin is not None
        and registry_configuration is not None
        and registry_typed is not None
        and registry_typed.enabled
    ):
        try:
            if registry_inspection is None:
                registry_inspection = registry_plugin.inspect(registry_configuration)
            registry_comparisons = registry_plugin.compare_presets(
                registry_configuration,
                registry_inspection,
            )
        except (OSError, ValueError):
            current_app.logger.exception("Could not inspect Registry presentation")

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

    try:
        snapshots = _snapshot_repository().list_for_product(product.id)
    except Exception:
        current_app.logger.exception(
            "Could not read snapshots for product %s", product.id
        )
        snapshots = []

    environment_profile_error = None
    try:
        environment_profiles = (
            _environment_profile_repository().list_for_product(product.id)
        )
        environment_profile_comparisons = EnvironmentProfileComparator(
            _plugin_manager(),
            _configuration_repository(),
            current_app.logger,
        ).compare_all(
            product,
            environment_profiles,
            (
                {registry_plugin.identifier: registry_inspection}
                if registry_plugin is not None
                and registry_inspection is not None
                else None
            ),
        )
    except Exception:
        current_app.logger.exception(
            "Could not load Environment Profiles for product %s", product.id
        )
        environment_profiles = []
        environment_profile_comparisons = ()
        environment_profile_error = (
            "Профілі середовища зараз недоступні; сховище не змінено."
        )

    context: dict[str, object] = {
        "product": product,
        "inspection_result": None,
        "license_configuration": license_configuration,
        "license_typed": license_typed,
        "log_configuration": log_configuration,
        "log_typed": log_typed,
        "registry_configuration": registry_configuration,
        "registry_typed": registry_typed,
        "operation_logs": recent_operation_logs,
        "operation_log_error": operation_log_error,
        "license_configuration_error": license_configuration_error,
        "log_configuration_error": log_configuration_error,
        "registry_configuration_error": registry_configuration_error,
        "license_plugin_available": license_plugin is not None,
        "log_plugin_available": log_plugin is not None,
        "registry_plugin_available": registry_plugin is not None,
        "license_plugin_error": license_plugin_error,
        "log_plugin_error": log_plugin_error,
        "registry_plugin_error": registry_plugin_error,
        "license_form": {},
        "log_form": {},
        "registry_form": {},
        "snapshot_form_label": "",
        "snapshot_error": None,
        "snapshots": snapshots,
        "environment_profiles": environment_profiles,
        "environment_profile_comparisons": environment_profile_comparisons,
        "environment_profile_error": environment_profile_error,
        "environment_profile_form": {},
        "environment_profile_editing_id": "",
        "environment_profile_execution_plan": None,
        "environment_profile_execution_token": None,
        "environment_profile_execution_result": None,
        "uk_changes": _uk_changes,
        "license_error": None,
        "log_error": None,
        "license_result": None,
        "change_plan": None,
        "operation_result": None,
        "backup_result": None,
        "log_result": None,
        "registry_error": None,
        "registry_result": registry_inspection,
        "registry_preset_preview": None,
        "registry_execution_token": None,
        "registry_executable_count": 0,
        "registry_unsupported_count": 0,
        "registry_execution_result": None,
        "log_collection_result": None,
        "snapshot_diff": None,
        "snapshot_diff_view": None,
        "snapshot_diff_base": None,
        "snapshot_diff_target": None,
        "snapshot_diff_target_is_current": False,
        "restore_plan": None,
        "restore_plan_snapshot": None,
        "restore_confirmation_token": None,
        "restore_executable_count": 0,
        "restore_result": None,
        "restore_result_snapshot": None,
        "restore_source_names": {},
        "registry_workspace_open": (
            request.args.get("open") == "windows-registry"
            or registry_configuration_error is not None
            or "registry" in (request.endpoint or "")
            or any(key.startswith("registry_") for key in results)
        ),
        "open_workspace": request.args.get("open", ""),
        "product_setup_export_error": None,
        "registry_editing_preset_id": (
            request.form.get("original_id", "")
            if request.endpoint == "web.edit_registry_preset"
            else ""
        ),
        "registry_editor_kind": (request.endpoint or "").removeprefix("web."),
    }
    context.update(results)
    context.update(
        _registry_presentation(
            registry_typed,
            context["registry_result"],
            context["registry_preset_preview"],
            registry_comparisons,
        )
    )
    return render_template("products/detail.html", **context)


def _registry_presentation(
    typed: WindowsRegistryConfiguration | None,
    inspection: RegistryInspectionResult | None,
    preview: RegistryChangePlan | None,
    comparisons: dict[str, RegistryChangePlan],
) -> dict[str, object]:
    value_states: dict[str, RegistryCurrentStateView] = {}
    branch_states: dict[str, RegistryCurrentStateView] = {}
    if inspection is not None:
        for item in inspection.values:
            value_states[item.target.id] = RegistryCurrentStateView(
                item.status.value,
                item.registry_type or "—",
                str(item.value) if item.value is not None else None,
            )
        for item in inspection.branches:
            branch_states[item.target.id] = RegistryCurrentStateView(
                item.status.value,
                item.status.value,
            )
    elif preview is not None:
        for entry in preview.entries:
            if entry.target_type.value == "value":
                value_states[entry.target_id] = RegistryCurrentStateView(
                    entry.current_state.status,
                    entry.current_state.registry_type or "—",
                    (
                        str(entry.current_state.value)
                        if entry.current_state.value is not None
                        else None
                    ),
                )
            else:
                branch_states[entry.target_id] = RegistryCurrentStateView(
                    entry.current_state.status,
                    entry.current_state.visibility,
                )

    cards: list[RegistryPresetCardView] = []
    if typed is not None:
        value_targets = {item.id: item for item in typed.value_targets}
        branch_targets = {item.id: item for item in typed.branch_targets}
        for preset in typed.presets:
            resources = [
                RegistryPresetResourceView(
                    value_targets[item.target_id].display_name or item.target_id,
                    f"→ {item.registry_type.value} {item.value}",
                    (
                        f"{value_targets[item.target_id].hive.value}\\"
                        f"{value_targets[item.target_id].key_path}"
                    ),
                    value_targets[item.target_id].value_name,
                )
                for item in preset.values
            ]
            resources.extend(
                RegistryPresetResourceView(
                    branch_targets[item.target_id].display_name or item.target_id,
                    (
                        "→ Видима"
                        if item.visibility.value == "visible"
                        else "→ Прихована"
                    ),
                    (
                        f"{branch_targets[item.target_id].hive.value}\\"
                        f"{branch_targets[item.target_id].key_path}"
                    ),
                )
                for item in preset.branches
            )
            cards.append(
                RegistryPresetCardView(
                    preset.id,
                    preset.name,
                    tuple(resources),
                    preset,
                    comparisons.get(preset.id),
                )
            )
    return {
        "registry_value_states": value_states,
        "registry_branch_states": branch_states,
        "registry_preset_cards": tuple(cards),
    }


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


def _normalize_snapshot_label(value: str) -> str | None:
    label = value.strip()
    if not label:
        return None
    if len(label) > 100:
        raise ValueError("Назва snapshot повинна містити не більше ніж 100 символів.")
    if any(ord(character) < 32 for character in label):
        raise ValueError("Назва snapshot містить заборонені символи.")
    return label


def _snapshot_diff_view(
    snapshot_diff: SnapshotDiff,
    base_snapshot: Snapshot,
    target_snapshot: Snapshot,
    *,
    target_is_current: bool,
) -> SnapshotDiffView:
    source_names = _snapshot_source_display_names()
    resource_views = tuple(
        _snapshot_resource_diff_view(entry, source_names)
        for entry in snapshot_diff.entries
    )
    status_details = (
        (SnapshotDiffStatus.CHANGED, "Змінено", snapshot_diff.changed_count),
        (SnapshotDiffStatus.ADDED, "Додано", snapshot_diff.added_count),
        (SnapshotDiffStatus.REMOVED, "Видалено", snapshot_diff.removed_count),
        (SnapshotDiffStatus.UNCHANGED, "Без змін", snapshot_diff.unchanged_count),
    )
    groups = tuple(
        SnapshotStatusGroupView(
            status=status.value,
            display_name=display_name,
            count=count,
            open_by_default=False,
            sources=_snapshot_source_groups(
                tuple(item for item in resource_views if item.status == status.value)
            ),
        )
        for status, display_name, count in status_details
        if count > 0
    )
    base_reference = _snapshot_reference(base_snapshot, is_current=False)
    target_reference = _snapshot_reference(
        target_snapshot,
        is_current=target_is_current,
    )
    warnings = tuple(
        item
        for item in (
            _snapshot_warning_view(base_reference.name, snapshot_diff.base_metadata),
            _snapshot_warning_view(
                target_reference.name,
                snapshot_diff.target_metadata,
            ),
        )
        if item is not None
    )
    return SnapshotDiffView(
        base=base_reference,
        target=target_reference,
        changed_count=snapshot_diff.changed_count,
        added_count=snapshot_diff.added_count,
        removed_count=snapshot_diff.removed_count,
        unchanged_count=snapshot_diff.unchanged_count,
        groups=groups,
        warnings=warnings,
    )


def _snapshot_resource_diff_view(
    entry: SnapshotDiffEntry,
    source_names: dict[str, str],
) -> SnapshotResourceDiffView:
    primary_snapshot = (
        entry.target_state
        if entry.status in (SnapshotDiffStatus.ADDED, SnapshotDiffStatus.UNCHANGED)
        else entry.base_state
        if entry.status == SnapshotDiffStatus.REMOVED
        else None
    )
    primary_state = None
    if primary_snapshot is not None:
        state = primary_snapshot.get("state")
        primary_state = _present_json_value(state, limit=1_000)
    return SnapshotResourceDiffView(
        source=entry.source,
        source_display_name=source_names.get(
            entry.source,
            _humanize_source(entry.source),
        ),
        resource_type=entry.resource_type,
        identifier=entry.identifier,
        identifier_display_name=_humanize_resource_identifier(entry.identifier),
        status=entry.status.value,
        field_changes=tuple(
            SnapshotFieldChangeView(
                path=change.path,
                base_value=_present_json_value(
                    change.base_value,
                    limit=500,
                    present=change.base_present,
                ),
                target_value=_present_json_value(
                    change.target_value,
                    limit=500,
                    present=change.target_present,
                ),
            )
            for change in entry.field_changes
        ),
        primary_state=primary_state,
        raw_base=_present_json_value(entry.base_state, formatted=True),
        raw_target=_present_json_value(entry.target_state, formatted=True),
    )


def _snapshot_source_groups(
    resources: tuple[SnapshotResourceDiffView, ...],
) -> tuple[SnapshotSourceGroupView, ...]:
    sources: dict[str, list[SnapshotResourceDiffView]] = {}
    for resource in resources:
        sources.setdefault(resource.source, []).append(resource)
    return tuple(
        SnapshotSourceGroupView(
            source=source,
            display_name=source_resources[0].source_display_name,
            resources=tuple(source_resources),
        )
        for source, source_resources in sorted(
            sources.items(),
            key=lambda item: item[1][0].source_display_name.casefold(),
        )
    )


def _snapshot_source_display_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for plugin in _plugin_manager().list_all():
        try:
            names[plugin.identifier] = plugin.display_name
        except Exception:
            current_app.logger.exception(
                "Could not prepare snapshot display name for a plugin"
            )
    return names


def _snapshot_reference(
    snapshot: Snapshot,
    *,
    is_current: bool,
) -> SnapshotReferenceView:
    if is_current:
        name = "Поточний стан"
    elif snapshot.label:
        name = snapshot.label
    else:
        name = "Snapshot без назви"
    return SnapshotReferenceView(
        name=name,
        created_at=snapshot.created_at,
        short_id=snapshot.id[:8],
        is_current=is_current,
    )


def _snapshot_warning_view(
    origin: str,
    metadata: dict[str, object],
) -> SnapshotWarningView | None:
    warnings = metadata.get("warnings")
    if not isinstance(warnings, (list, tuple)):
        return None
    messages = tuple(
        _safe_snapshot_warning(item)
        for item in warnings
        if isinstance(item, str)
    )
    if not messages:
        return None
    return SnapshotWarningView(origin=origin, messages=messages)


def _safe_snapshot_warning(message: str) -> str:
    if "traceback (most recent call last)" in message.casefold():
        return "Технічні деталі помилки приховано."
    return message


def _humanize_source(source: str) -> str:
    name = source.rsplit(".", maxsplit=1)[-1]
    return name.replace("_", " ").replace("-", " ").title()


def _humanize_resource_identifier(identifier: str) -> str:
    if any(separator in identifier for separator in ("/", "\\")) or "." in identifier:
        return identifier
    return identifier.replace("_", " ").replace("-", " ").capitalize()


def _present_json_value(
    value: object,
    limit: int = 2_000,
    *,
    formatted: bool = False,
    present: bool = True,
) -> PresentedJsonValue:
    if not present:
        return PresentedJsonValue(text="—", truncated=False)
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if formatted else None,
    )
    if len(text) <= limit:
        return PresentedJsonValue(text=text, truncated=False)
    return PresentedJsonValue(text=f"{text[:limit]}…", truncated=True)


def _append_snapshot_operation_log(
    product_id: str,
    snapshot,
) -> None:
    warnings = snapshot.metadata.get("warnings")
    warning_count = len(warnings) if isinstance(warnings, (list, tuple)) else 0
    _operation_logs().append(
        OperationLog(
            id=str(uuid4()),
            timestamp=datetime.now(UTC),
            product_id=product_id,
            plugin_identifier="snapshot-system",
            action_identifier="create-snapshot",
            status=(
                OperationStatus.PARTIAL
                if warning_count > 0
                else OperationStatus.SUCCESS
            ),
            summary=(
                "Snapshot створено частково." if warning_count else "Snapshot створено."
            ),
            changed_count=len(snapshot.resources),
            skipped_count=0,
            error_count=warning_count,
            rollback_status=RollbackStatus.NOT_REQUIRED,
        )
    )


def _append_snapshot_delete_operation_log(
    product_id: str,
    snapshot_id: str,
) -> None:
    _operation_logs().append(
        OperationLog(
            id=str(uuid4()),
            timestamp=datetime.now(UTC),
            product_id=product_id,
            plugin_identifier="snapshot-system",
            action_identifier="delete-snapshot",
            status=OperationStatus.SUCCESS,
            summary=f"Snapshot {snapshot_id[:8]} видалено.",
            changed_count=1,
            skipped_count=0,
            error_count=0,
            rollback_status=RollbackStatus.NOT_REQUIRED,
        )
    )


def _log_registry_execution(
    result: RegistryExecutionResult,
) -> RegistryExecutionResult:
    try:
        failed = result.failed_count
        stale = result.stale_count
        blocked = result.blocked_count
        succeeded = result.succeeded_count
        if failed:
            status = OperationStatus.PARTIAL if succeeded else OperationStatus.FAILED
        elif stale or blocked:
            status = OperationStatus.PARTIAL if succeeded else OperationStatus.BLOCKED
        elif succeeded:
            status = (
                OperationStatus.PARTIAL
                if result.unsupported_count
                else OperationStatus.SUCCESS
            )
        else:
            status = OperationStatus.NO_CHANGES
        rollback = RollbackStatus.NOT_REQUIRED
        if any(
            entry.rollback_status is RegistryRollbackStatus.FAILED
            for entry in result.entries
        ):
            rollback = RollbackStatus.PARTIAL
        elif any(
            entry.rollback_status is RegistryRollbackStatus.SUCCEEDED
            for entry in result.entries
        ):
            rollback = RollbackStatus.COMPLETE
        entry_summary = "; ".join(
            _registry_log_entry(entry) for entry in result.entries
        )
        _operation_logs().append(
            OperationLog(
                id=str(uuid4()),
                timestamp=datetime.now(UTC),
                product_id=result.product_id,
                plugin_identifier=WindowsRegistry.identifier,
                action_identifier="apply-registry-preset-values",
                status=status,
                summary=(
                    f"Registry preset {result.preset_name}: {entry_summary}"
                    if entry_summary
                    else f"Registry preset {result.preset_name}: no changes"
                ),
                changed_count=succeeded,
                skipped_count=(result.unsupported_count + stale + blocked),
                error_count=failed,
                rollback_status=rollback,
            )
        )
        return result
    except Exception:
        current_app.logger.exception("Could not save Registry execution log")
        return replace(
            result,
            warnings=(
                *result.warnings,
                "Registry execution finished, but its operation log was not saved.",
            ),
            operation_log_saved=False,
        )


def _registry_log_entry(entry: RegistryExecutionEntryResult) -> str:
    type_changed = "no"
    if isinstance(entry.current_state, RegistryValueState) and isinstance(
        entry.desired_state, RegistryPresetValue
    ):
        type_changed = (
            "yes"
            if entry.current_state.registry_type
            != entry.desired_state.registry_type.value
            else "no"
        )
    transition = ""
    if isinstance(entry.current_state, RegistryBranchState) and isinstance(
        entry.desired_state, RegistryPresetBranch
    ):
        transition = (
            f"; transition: {entry.current_state.visibility}"
            f"->{entry.desired_state.visibility.value}"
        )
    return (
        f"{entry.display_name} ({entry.target_id}) {entry.status.value} "
        f"[type changed: {type_changed}{transition}; "
        f"rollback: {entry.rollback_status.value}]"
    )


def _positive_config_int(key: str) -> int:
    value = current_app.config[key]
    if type(value) is not int or value < 1:
        current_app.logger.error("Invalid positive integer config: %s", key)
        abort(500)
    return value


def _uk_changes(count: int) -> str:
    """Return a compact Ukrainian change count."""
    if count % 100 in {11, 12, 13, 14}:
        word = "змін"
    elif count % 10 == 1:
        word = "зміна"
    elif count % 10 in {2, 3, 4}:
        word = "зміни"
    else:
        word = "змін"
    return f"{count} {word}"


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


def _derived_product_name(executable_path: str | None) -> str:
    if not executable_path:
        return ""
    path = _portable_pure_path(executable_path)
    return path.stem or path.parent.name


def _derived_working_directory(executable_path: str | None) -> str | None:
    if not executable_path:
        return None
    parent = _portable_pure_path(executable_path).parent
    return str(parent) if str(parent) not in {"", "."} else None


def _portable_pure_path(value: str) -> PurePath:
    return (
        PureWindowsPath(value)
        if "\\" in value or re.match(r"^[A-Za-z]:/", value)
        else PurePosixPath(value)
    )


def _clean_input_path(value: str | None) -> str | None:
    if (
        value is not None
        and len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {'"', "'"}
    ):
        return value[1:-1]
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Product Setup JSON contains a duplicate field")
        result[key] = value
    return result


def _setup_filename(product_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", product_name).strip("-._")
    return f"{slug or 'product'}-setup.json"


def _product_setup_json(data: dict[str, object]) -> str:
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _product_setup_document(
    data: object,
) -> tuple[str, tuple[ProductSetupPackage, ...]]:
    if not isinstance(data, dict):
        raise ValueError("Product Setup document must be an object")
    fields = set(data)
    if fields == {"document_type", "schema_version", "packages"}:
        bundle = ProductSetupBundle.from_dict(data)
        return "bundle", bundle.packages
    if fields == {
        "schema_version",
        "product",
        "plugin_sections",
        "omitted_plugins",
    }:
        package = ProductSetupPackage.from_dict(data)
        return "package", (package,)
    raise ValueError("Product Setup document type is unknown or ambiguous")


def _selected_setup_indices(source: ProductSetupImportSource) -> tuple[int, ...]:
    try:
        selected = tuple(int(item) for item in request.form.getlist("selected_indices"))
    except ValueError as error:
        raise ValueError("Invalid Product selection") from error
    allowed = {item.index for item in source.entries}
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(index not in allowed for index in selected)
    ):
        raise ValueError("Invalid Product selection")
    return tuple(index for index in sorted(allowed) if index in set(selected))


def _setup_adapted_values(
    preparations: tuple[SetupProductAdaptation, ...],
) -> tuple[SetupProductAdaptedValues, ...]:
    values: list[SetupProductAdaptedValues] = []
    for preparation in preparations:
        index = preparation.entry.index
        plugin_values = tuple(
            (
                plugin.plugin_identifier,
                tuple(
                    (
                        field.key,
                        request.form.get(
                            _setup_plugin_field_name(
                                index, plugin.plugin_identifier, field.key
                            ),
                            "",
                        ),
                    )
                    for field in plugin.fields
                ),
            )
            for plugin in preparation.plugins
            if plugin.status == "supported"
        )
        values.append(
            SetupProductAdaptedValues(
                index,
                request.form.get(f"product_{index}_name", ""),
                request.form.get(f"product_{index}_executable_path", ""),
                request.form.get(f"product_{index}_working_directory", ""),
                plugin_values,
            )
        )
    return tuple(values)


def _setup_plugin_field_name(index: int, identifier: str, key: str) -> str:
    return f"plugin_{index}_{identifier}_{key}"


def _render_setup_import_error(message: str, status: int) -> tuple[str, int]:
    return render_template("product_setup/import.html", error=message), status


def _render_setup_configuration(
    source: ProductSetupImportSource,
    preparations: tuple[SetupProductAdaptation, ...],
    review: ProductSetupImportReview,
    form: object,
    selected_indices: tuple[int, ...],
    confirmation_token: str | None = None,
) -> str:
    review_by_index = {item.entry.index: item for item in review.products}
    plugin_count = sum(
        plugin.status == "ready"
        for product in review.products
        for plugin in product.plugins
    )
    return render_template(
        "product_setup/adapt.html",
        source_token=source.token,
        document_type=source.document_type,
        adaptations=preparations,
        form=form,
        selected_indices=set(selected_indices),
        review=review,
        review_by_index=review_by_index,
        confirmation_token=confirmation_token,
        confirmation_product_count=len(review.products),
        confirmation_plugin_count=plugin_count,
    )
