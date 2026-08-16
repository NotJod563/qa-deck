"""Generic Product Setup export and read-only preview coordination."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from logging import Logger
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from secrets import token_urlsafe
from threading import Lock
from uuid import uuid4

from qa_deck.domain import PluginConfiguration, Product
from qa_deck.domain.product_setup import (
    PluginSetupSection,
    PortablePath,
    ProductSetupPackage,
    ProductSetupProduct,
)
from qa_deck.plugins import PluginManager
from qa_deck.storage import PluginConfigurationRepository, ProductRepository

MAX_ADAPTED_NAME_LENGTH = 200
MAX_ADAPTED_PATH_LENGTH = 4_096


@dataclass(frozen=True, slots=True)
class SetupPathPreview:
    label: str
    path: PortablePath
    exists: bool
    requires_adaptation: bool


@dataclass(frozen=True, slots=True)
class SetupPluginPreview:
    plugin_identifier: str
    display_name: str
    status: str
    details: tuple[str, ...] = ()
    paths: tuple[SetupPathPreview, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ProductSetupPreview:
    package: ProductSetupPackage
    product_paths: tuple[SetupPathPreview, ...]
    plugin_sections: tuple[SetupPluginPreview, ...]

    @property
    def requires_adaptation(self) -> bool:
        paths = [*self.product_paths]
        for section in self.plugin_sections:
            paths.extend(section.paths)
        return any(item.requires_adaptation for item in paths)

    @property
    def validation_issue_count(self) -> int:
        return sum(item.status == "invalid" for item in self.plugin_sections)


@dataclass(frozen=True, slots=True)
class SetupImportField:
    key: str
    label: str
    value: str
    path_status: str


@dataclass(frozen=True, slots=True)
class SetupPluginImportPreparation:
    plugin_identifier: str
    display_name: str
    status: str
    fields: tuple[SetupImportField, ...] = ()
    details: tuple[str, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ProductSetupImportSourceEntry:
    index: int
    product_id: str
    package: ProductSetupPackage


@dataclass(frozen=True, slots=True)
class ProductSetupImportSource:
    token: str
    document_type: str
    entries: tuple[ProductSetupImportSourceEntry, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SetupProductAdaptation:
    entry: ProductSetupImportSourceEntry
    product_fields: tuple[SetupImportField, ...]
    install_directory: str
    executable_relative_path: str | None
    working_relative_path: str | None
    plugins: tuple[SetupPluginImportPreparation, ...]


@dataclass(frozen=True, slots=True)
class SetupProductAdaptedValues:
    index: int
    name: str
    executable_path: str
    working_directory: str
    plugin_values: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]

    def values_for(self, plugin_identifier: str) -> dict[str, str]:
        values = next(
            (
                values
                for identifier, values in self.plugin_values
                if identifier == plugin_identifier
            ),
            (),
        )
        return dict(values)


@dataclass(frozen=True, slots=True)
class SetupPluginImportReview:
    plugin_identifier: str
    display_name: str
    status: str
    configuration: PluginConfiguration | None = None
    fields: tuple[SetupImportField, ...] = ()
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SetupProductImportReview:
    entry: ProductSetupImportSourceEntry
    product: Product | None
    product_name: str
    plugins: tuple[SetupPluginImportReview, ...]
    product_paths: tuple[SetupImportField, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def can_import(self) -> bool:
        return self.product is not None and not self.errors


@dataclass(frozen=True, slots=True)
class ProductSetupImportReview:
    products: tuple[SetupProductImportReview, ...]

    @property
    def can_confirm(self) -> bool:
        return bool(self.products) and all(item.can_import for item in self.products)


@dataclass(frozen=True, slots=True)
class ProductSetupImportIntent:
    token: str
    entries: tuple[ProductSetupImportSourceEntry, ...]
    adaptations: tuple[SetupProductAdaptedValues, ...]
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class SetupPluginImportResult:
    plugin_identifier: str
    display_name: str
    status: str
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SetupProductImportResult:
    product_id: str
    product_name: str
    status: str
    plugins: tuple[SetupPluginImportResult, ...]
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ProductSetupImportResult:
    products: tuple[SetupProductImportResult, ...]


class ProductSetupImportStateStore:
    """Keep bounded import sources, one-time intents, and PRG results in memory."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = limit
        self._sources: dict[str, ProductSetupImportSource] = {}
        self._intents: dict[str, ProductSetupImportIntent] = {}
        self._results: dict[str, ProductSetupImportResult] = {}
        self._lock = Lock()

    def create_source(
        self,
        packages: tuple[ProductSetupPackage, ...],
        document_type: str,
    ) -> ProductSetupImportSource:
        if document_type not in {"package", "bundle"}:
            raise ValueError("Invalid Product Setup document type")
        entries = tuple(
            ProductSetupImportSourceEntry(index, str(uuid4()), package)
            for index, package in enumerate(packages)
        )
        source = ProductSetupImportSource(
            token_urlsafe(24),
            document_type,
            entries,
            _packages_fingerprint(packages),
        )
        with self._lock:
            self._trim(self._sources)
            self._sources[source.token] = source
        return source

    def get_source(self, token: str) -> ProductSetupImportSource | None:
        with self._lock:
            return self._sources.get(token)

    def create_intent(
        self,
        source: ProductSetupImportSource,
        adaptations: tuple[SetupProductAdaptedValues, ...],
    ) -> ProductSetupImportIntent:
        selected = {item.index for item in adaptations}
        entries = tuple(item for item in source.entries if item.index in selected)
        intent = ProductSetupImportIntent(
            token_urlsafe(24),
            entries,
            adaptations,
            _packages_fingerprint(tuple(item.package for item in entries)),
        )
        with self._lock:
            self._trim(self._intents)
            self._intents[intent.token] = intent
        return intent

    def take_intent(self, token: str) -> ProductSetupImportIntent | None:
        with self._lock:
            return self._intents.pop(token, None)

    def save_result(self, result: ProductSetupImportResult) -> str:
        result_id = token_urlsafe(18)
        with self._lock:
            self._trim(self._results)
            self._results[result_id] = result
        return result_id

    def get_result(self, result_id: str) -> ProductSetupImportResult | None:
        with self._lock:
            return self._results.get(result_id)

    def _trim(self, values: dict[str, object]) -> None:
        while len(values) >= self._limit:
            values.pop(next(iter(values)))


class ProductSetupService:
    """Coordinate optional plugin setup capabilities without applying data."""

    def __init__(
        self,
        plugins: PluginManager,
        configurations: PluginConfigurationRepository,
        logger: Logger | None = None,
    ) -> None:
        self._plugins = plugins
        self._configurations = configurations
        self._logger = logger

    def export(self, product: Product) -> ProductSetupPackage:
        setup_product = product_setup_product(product)
        sections: list[PluginSetupSection] = []
        omitted: list[str] = []
        configurations = self._configurations.list_for_product(product.id)
        for configuration in sorted(
            configurations, key=lambda item: item.plugin_identifier
        ):
            identifier = configuration.plugin_identifier
            plugin = self._plugins.get(identifier)
            try:
                exporter = getattr(plugin, "export_product_setup", None)
            except Exception:
                exporter = None
            if not callable(exporter):
                omitted.append(identifier)
                continue
            try:
                section = exporter(setup_product, configuration)
                if (
                    not isinstance(section, PluginSetupSection)
                    or section.plugin_identifier != identifier
                ):
                    raise TypeError("Plugin returned an invalid setup section")
                sections.append(section)
            except Exception:
                self._log_failure("export", identifier)
                omitted.append(identifier)
        return ProductSetupPackage(
            setup_product,
            tuple(sections),
            tuple(omitted),
        )

    def preview(self, package: ProductSetupPackage) -> ProductSetupPreview:
        product_paths = tuple(
            item
            for item in (
                preview_path("Executable", package.product.executable_path),
                preview_path("Робочий каталог", package.product.working_directory),
            )
            if item is not None
        )
        previews: list[SetupPluginPreview] = []
        for section in package.plugin_sections:
            plugin = self._plugins.get(section.plugin_identifier)
            display_name = _display_name(plugin, section.plugin_identifier)
            try:
                previewer = getattr(plugin, "preview_product_setup", None)
            except Exception:
                previewer = None
            if not callable(previewer):
                previews.append(
                    SetupPluginPreview(
                        section.plugin_identifier,
                        display_name,
                        "unsupported",
                        message=(
                            "Плагін недоступний або не підтримує попередній "
                            "перегляд імпорту."
                        ),
                    )
                )
                continue
            try:
                result = previewer(package.product, section)
                if not isinstance(result, SetupPluginPreview):
                    raise TypeError("Plugin returned an invalid setup preview")
                previews.append(result)
            except Exception:
                self._log_failure("preview", section.plugin_identifier)
                previews.append(
                    SetupPluginPreview(
                        section.plugin_identifier,
                        display_name,
                        "invalid",
                        message="Секція плагіна містить некоректні дані.",
                    )
                )
        for identifier in package.omitted_plugins:
            previews.append(
                SetupPluginPreview(
                    identifier,
                    _display_name(self._plugins.get(identifier), identifier),
                    "omitted",
                    message="Конфігурацію не було включено під час експорту.",
                )
            )
        return ProductSetupPreview(package, product_paths, tuple(previews))

    def prepare_import(
        self, entry: ProductSetupImportSourceEntry
    ) -> SetupProductAdaptation:
        product = entry.package.product
        executable = product.executable_path
        working = product.working_directory
        product_fields = (
            SetupImportField("name", "Назва Product", product.name, "not_applicable"),
            SetupImportField(
                "executable_path",
                "Шлях до executable",
                executable.original if executable else "",
                path_probe_status(executable.original if executable else ""),
            ),
            SetupImportField(
                "working_directory",
                "Робочий каталог",
                working.original if working else "",
                path_probe_status(working.original if working else ""),
            ),
        )
        plugins: list[SetupPluginImportPreparation] = []
        for section in entry.package.plugin_sections:
            plugin = self._plugins.get(section.plugin_identifier)
            display_name = _display_name(plugin, section.plugin_identifier)
            try:
                preparer = getattr(plugin, "prepare_product_setup_import", None)
            except Exception:
                preparer = None
            if not callable(preparer):
                plugins.append(
                    SetupPluginImportPreparation(
                        section.plugin_identifier,
                        display_name,
                        "unsupported",
                        message="Плагін недоступний або не підтримує імпорт.",
                    )
                )
                continue
            try:
                preparation = preparer(product, section)
                if (
                    not isinstance(preparation, SetupPluginImportPreparation)
                    or preparation.plugin_identifier != section.plugin_identifier
                ):
                    raise TypeError("Plugin returned invalid import preparation")
                plugins.append(preparation)
            except Exception:
                self._log_failure("import preparation", section.plugin_identifier)
                plugins.append(
                    SetupPluginImportPreparation(
                        section.plugin_identifier,
                        display_name,
                        "invalid",
                        message="Секцію плагіна не вдалося підготувати до імпорту.",
                    )
                )
        for identifier in entry.package.omitted_plugins:
            plugins.append(
                SetupPluginImportPreparation(
                    identifier,
                    _display_name(self._plugins.get(identifier), identifier),
                    "omitted",
                    message="Конфігурацію не було включено під час експорту.",
                )
            )
        return SetupProductAdaptation(
            entry,
            product_fields,
            product.install_directory_hint or "",
            executable.relative_to_install if executable else None,
            working.relative_to_install if working else None,
            tuple(plugins),
        )

    def review_import(
        self,
        source: ProductSetupImportSource,
        adaptations: tuple[SetupProductAdaptedValues, ...],
        existing_products: list[Product],
    ) -> ProductSetupImportReview:
        entries = {item.index: item for item in source.entries}
        indices = [item.index for item in adaptations]
        if (
            not adaptations
            or len(indices) != len(set(indices))
            or any(index not in entries for index in indices)
        ):
            return ProductSetupImportReview(())
        final_names = [item.name.strip().casefold() for item in adaptations]
        duplicate_names = {
            name for name in final_names if name and final_names.count(name) > 1
        }
        existing_names = {item.name.casefold() for item in existing_products}
        reviews = tuple(
            self._review_import_entry(
                entries[adaptation.index],
                adaptation,
                existing_names,
                duplicate_names,
            )
            for adaptation in adaptations
        )
        return ProductSetupImportReview(reviews)

    def execute_import(
        self,
        intent: ProductSetupImportIntent,
        products: ProductRepository,
    ) -> ProductSetupImportResult:
        selected_fingerprint = _packages_fingerprint(
            tuple(item.package for item in intent.entries)
        )
        if selected_fingerprint != intent.source_fingerprint:
            return ProductSetupImportResult(
                tuple(
                    SetupProductImportResult(
                        item.product_id,
                        item.package.product.name,
                        "BLOCKED",
                        (),
                        "Джерело імпорту змінилося.",
                    )
                    for item in intent.entries
                )
            )
        source = ProductSetupImportSource(
            "", "bundle" if len(intent.entries) > 1 else "package", intent.entries,
            selected_fingerprint,
        )
        try:
            existing_products = products.list_all()
        except Exception:
            self._log_failure("Product conflict validation", "repository")
            return ProductSetupImportResult(
                tuple(
                    SetupProductImportResult(
                        item.product_id,
                        item.package.product.name,
                        "FAILED",
                        (),
                        "Не вдалося перевірити наявні Products.",
                    )
                    for item in intent.entries
                )
            )
        review = self.review_import(source, intent.adaptations, existing_products)
        if len(review.products) != len(intent.entries):
            return ProductSetupImportResult(())
        results: list[SetupProductImportResult] = []
        for item in review.products:
            results.append(self._persist_import_product(item, products))
        return ProductSetupImportResult(tuple(results))

    def _review_import_entry(
        self,
        entry: ProductSetupImportSourceEntry,
        adaptation: SetupProductAdaptedValues,
        existing_names: set[str],
        duplicate_names: set[str],
    ) -> SetupProductImportReview:
        errors: list[str] = []
        warnings: list[str] = []
        name = adaptation.name.strip()
        if (
            not name
            or len(name) > MAX_ADAPTED_NAME_LENGTH
            or _has_control_characters(name)
        ):
            errors.append("Назва Product має некоректний формат.")
        elif name.casefold() in existing_names:
            errors.append("Product із такою назвою вже існує.")
        if name.casefold() in duplicate_names:
            errors.append("Назва Product повторюється у вибраному імпорті.")
        executable = _validated_adapted_path(
            adaptation.executable_path, "Шлях до executable", errors
        )
        working = _validated_adapted_path(
            adaptation.working_directory, "Робочий каталог", errors
        )
        if not executable:
            errors.append("Вкажіть локальний шлях до executable.")
        if entry.package.product.working_directory is not None and not working:
            errors.append("Вкажіть локальний робочий каталог.")
        product: Product | None = None
        if not errors:
            try:
                product = Product(
                    entry.product_id,
                    name,
                    entry.package.product.description,
                    executable,
                    working,
                    [],
                )
            except ValueError:
                errors.append("Product містить некоректні значення.")
        plugin_reviews: list[SetupPluginImportReview] = []
        for section in entry.package.plugin_sections:
            plugin = self._plugins.get(section.plugin_identifier)
            display_name = _display_name(plugin, section.plugin_identifier)
            try:
                preparer = getattr(plugin, "prepare_product_setup_import", None)
                builder = getattr(
                    plugin, "build_product_setup_configuration", None
                )
            except Exception:
                preparer = None
                builder = None
            if not callable(preparer) or not callable(builder):
                message = "Плагін недоступний або не підтримує імпорт."
                warnings.append(f"{display_name}: {message}")
                plugin_reviews.append(
                    SetupPluginImportReview(
                        section.plugin_identifier,
                        display_name,
                        "unsupported",
                        message=message,
                    )
                )
                continue
            try:
                preparation = preparer(entry.package.product, section)
                if not isinstance(preparation, SetupPluginImportPreparation):
                    raise TypeError("Plugin returned invalid import preparation")
                adapted = adaptation.values_for(section.plugin_identifier)
                adapted_fields = tuple(
                    SetupImportField(
                        field.key,
                        field.label,
                        adapted.get(field.key, ""),
                        path_probe_status(adapted.get(field.key, "")),
                    )
                    for field in preparation.fields
                )
                missing_fields = [
                    field.label for field in adapted_fields if not field.value
                ]
                if missing_fields:
                    message = "Вкажіть локальне значення: " + ", ".join(
                        missing_fields
                    )
                    errors.append(f"{display_name}: {message}")
                    plugin_reviews.append(
                        SetupPluginImportReview(
                            section.plugin_identifier,
                            display_name,
                            "invalid",
                            fields=adapted_fields,
                            message=message,
                        )
                    )
                    continue
                configuration = builder(
                    entry.product_id,
                    entry.package.product,
                    section,
                    adapted,
                )
                if (
                    not isinstance(configuration, PluginConfiguration)
                    or configuration.product_id != entry.product_id
                    or configuration.plugin_identifier != section.plugin_identifier
                ):
                    raise TypeError("Plugin returned invalid configuration")
                plugin_reviews.append(
                    SetupPluginImportReview(
                        section.plugin_identifier,
                        display_name,
                        "ready",
                        configuration,
                        adapted_fields,
                    )
                )
            except Exception:
                self._log_failure("import validation", section.plugin_identifier)
                message = "Адаптована конфігурація плагіна некоректна."
                errors.append(f"{display_name}: {message}")
                plugin_reviews.append(
                    SetupPluginImportReview(
                        section.plugin_identifier,
                        display_name,
                        "invalid",
                        message=message,
                    )
                )
        for identifier in entry.package.omitted_plugins:
            message = "Конфігурацію не було включено під час експорту."
            warnings.append(f"{identifier}: {message}")
            plugin_reviews.append(
                SetupPluginImportReview(
                    identifier,
                    _display_name(self._plugins.get(identifier), identifier),
                    "omitted",
                    message=message,
                )
            )
        return SetupProductImportReview(
            entry=entry,
            product=product,
            product_name=name or entry.package.product.name,
            plugins=tuple(plugin_reviews),
            product_paths=(
                import_path_field(
                    "executable_path", "Шлях до executable", executable or ""
                ),
                import_path_field(
                    "working_directory", "Робочий каталог", working or ""
                ),
            ),
            warnings=tuple(warnings),
            errors=tuple(dict.fromkeys(errors)),
        )

    def _persist_import_product(
        self,
        review: SetupProductImportReview,
        products: ProductRepository,
    ) -> SetupProductImportResult:
        plugin_results = [
            SetupPluginImportResult(
                item.plugin_identifier,
                item.display_name,
                "UNSUPPORTED" if item.status == "unsupported" else "OMITTED",
                item.message,
            )
            for item in review.plugins
            if item.status in {"unsupported", "omitted"}
        ]
        if not review.can_import or review.product is None:
            return SetupProductImportResult(
                review.entry.product_id,
                review.product_name,
                "BLOCKED",
                tuple(
                    _blocked_plugin_result(item)
                    for item in review.plugins
                ),
                "; ".join(review.errors) or "Імпорт заблоковано.",
            )
        product = review.product
        try:
            products.add(product)
        except Exception:
            self._log_failure("Product persistence", product.id)
            return SetupProductImportResult(
                product.id,
                product.name,
                "FAILED",
                tuple(
                    _blocked_plugin_result(item)
                    for item in review.plugins
                ),
                "Не вдалося створити Product.",
            )
        configured: list[str] = []
        ready_plugins = [item for item in review.plugins if item.status == "ready"]
        try:
            for plugin_review in ready_plugins:
                configuration = plugin_review.configuration
                if configuration is None:  # pragma: no cover
                    raise ValueError("Missing prepared plugin configuration")
                self._configurations.upsert(configuration)
                configured.append(plugin_review.plugin_identifier)
                plugin_results.append(
                    SetupPluginImportResult(
                        plugin_review.plugin_identifier,
                        plugin_review.display_name,
                        "CONFIGURED",
                    )
                )
        except Exception:
            self._log_failure("configuration persistence", product.id)
            plugin_results = [
                _replace_result_status(item, "ROLLBACK_ATTEMPTED")
                if item.status == "CONFIGURED"
                else item
                for item in plugin_results
            ]
            failed_identifier = (
                ready_plugins[len(configured)].plugin_identifier
                if len(configured) < len(ready_plugins)
                else "plugin"
            )
            plugin_results.append(
                SetupPluginImportResult(
                    failed_identifier,
                    _display_name(
                        self._plugins.get(failed_identifier), failed_identifier
                    ),
                    "FAILED",
                    "Не вдалося зберегти конфігурацію.",
                )
            )
            for blocked in ready_plugins[len(configured) + 1 :]:
                plugin_results.append(
                    SetupPluginImportResult(
                        blocked.plugin_identifier,
                        blocked.display_name,
                        "BLOCKED",
                        "Попередню конфігурацію не вдалося зберегти.",
                    )
                )
            cleanup_ok = True
            for identifier in dict.fromkeys((*configured, failed_identifier)):
                try:
                    self._configurations.delete(product.id, identifier)
                except Exception:
                    cleanup_ok = False
            try:
                cleanup_ok = products.remove(product.id) is not None and cleanup_ok
            except Exception:
                cleanup_ok = False
            message = "Не вдалося зберегти конфігурації Product."
            if not cleanup_ok:
                message += " Автоматичне очищення було неповним."
            return SetupProductImportResult(
                product.id,
                product.name,
                "FAILED",
                tuple(plugin_results),
                message,
            )
        return SetupProductImportResult(
            product.id,
            product.name,
            "CREATED",
            tuple(plugin_results),
        )

    def _log_failure(self, stage: str, identifier: str) -> None:
        if self._logger is not None:
            self._logger.exception("Product Setup %s failed for %s", stage, identifier)


def default_setup_adapted_values(
    preparation: SetupProductAdaptation,
) -> SetupProductAdaptedValues:
    """Build server-owned initial values for the first configuration view."""
    product_values = {field.key: field.value for field in preparation.product_fields}
    plugin_values = tuple(
        (
            plugin.plugin_identifier,
            tuple((field.key, field.value) for field in plugin.fields),
        )
        for plugin in preparation.plugins
        if plugin.status == "supported"
    )
    return SetupProductAdaptedValues(
        preparation.entry.index,
        product_values.get("name", ""),
        product_values.get("executable_path", ""),
        product_values.get("working_directory", ""),
        plugin_values,
    )


def product_setup_product(product: Product) -> ProductSetupProduct:
    install_hint = _parent_path(product.executable_path)
    return ProductSetupProduct(
        product.name,
        product.description,
        install_hint,
        make_portable_path(product.executable_path, install_hint),
        make_portable_path(product.working_directory, install_hint),
    )


def make_portable_path(
    value: str | None, install_hint: str | None
) -> PortablePath | None:
    if not value:
        return None
    relative: str | None = None
    if install_hint:
        try:
            path = _pure_path(value)
            base = _pure_path(install_hint)
            relative = str(path.relative_to(base)).replace("\\", "/")
        except (TypeError, ValueError):
            pass
    return PortablePath(value, relative)


def preview_path(label: str, path: PortablePath | None) -> SetupPathPreview | None:
    if path is None:
        return None
    exists = _safe_local_path_exists(path.original)
    return SetupPathPreview(
        label,
        path,
        exists,
        not exists,
    )


def import_path_field(key: str, label: str, value: str) -> SetupImportField:
    return SetupImportField(key, label, value, path_probe_status(value))


def validate_import_path(value: str, label: str) -> str:
    errors: list[str] = []
    normalized = _validated_adapted_path(value, label, errors)
    if errors:
        raise ValueError(errors[0])
    return normalized or ""


def _replace_result_status(
    result: SetupPluginImportResult, status: str
) -> SetupPluginImportResult:
    return SetupPluginImportResult(
        result.plugin_identifier,
        result.display_name,
        status,
        result.message,
    )


def _blocked_plugin_result(
    review: SetupPluginImportReview,
) -> SetupPluginImportResult:
    statuses = {
        "unsupported": "UNSUPPORTED",
        "omitted": "OMITTED",
        "invalid": "INVALID",
    }
    return SetupPluginImportResult(
        review.plugin_identifier,
        review.display_name,
        statuses.get(review.status, "BLOCKED"),
        review.message,
    )


def path_probe_status(value: str) -> str:
    if not value:
        return "not_provided"
    if _is_unprobeable_path(value):
        return "not_probed"
    try:
        return "exists" if Path(value).exists() else "missing"
    except OSError:
        return "not_probed"


def _parent_path(value: str | None) -> str | None:
    if not value:
        return None
    parent = _pure_path(value).parent
    return str(parent) if str(parent) not in {"", "."} else None


def _pure_path(value: str) -> PurePath:
    return (
        PureWindowsPath(value)
        if "\\" in value or re.match(r"^[A-Za-z]:/", value)
        else PurePosixPath(value)
    )


def _display_name(plugin: object, fallback: str) -> str:
    try:
        return str(getattr(plugin, "display_name", fallback))
    except Exception:
        return fallback


def _safe_local_path_exists(value: str) -> bool:
    if _is_unprobeable_path(value):
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _is_unprobeable_path(value: str) -> bool:
    if value.startswith(("//", "\\\\")):
        return True
    pure = _pure_path(value)
    if not pure.is_absolute():
        return True
    return isinstance(pure, PureWindowsPath) and (
        pure.drive.startswith("\\\\")
        or value.startswith(("\\\\?\\", "\\\\.\\"))
    )


def _validated_adapted_path(
    value: str,
    label: str,
    errors: list[str],
) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{label} має бути текстовим значенням.")
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_ADAPTED_PATH_LENGTH or _has_control_characters(
        normalized
    ):
        errors.append(f"{label} має некоректний формат.")
        return None
    return normalized


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 for character in value)


def _packages_fingerprint(packages: tuple[ProductSetupPackage, ...]) -> str:
    payload = json.dumps(
        [item.to_dict() for item in packages],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
