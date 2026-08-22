"""Focused Product Setup Package and preview behavior."""

import json
import re
from io import BytesIO
from pathlib import Path

import pytest

from qa_deck.domain import (
    PluginConfiguration,
    PluginSetupSection,
    PortablePath,
    Product,
    ProductSetupBundle,
    ProductSetupPackage,
)
from qa_deck.plugins import PluginManager
from qa_deck.product_setup import (
    ProductSetupService,
    SetupPluginImportPreparation,
    SetupPluginPreview,
    product_setup_product,
)
from qa_deck.storage import PluginConfigurationRepository
from tests.helpers import configurations, make_app, products


def _upload_setup(client, document: dict[str, object]) -> str:
    response = client.post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(document).encode()),
                "setup.json",
            )
        },
    )
    assert response.status_code == 200
    match = re.search(
        r'name="source_token" value="([^"]+)"', response.get_data(as_text=True)
    )
    assert match is not None
    return match.group(1)


def _confirmation_token(response) -> str:
    assert response.status_code == 200
    match = re.search(
        r'name="confirmation_token" value="([^"]+)"',
        response.get_data(as_text=True),
    )
    assert match is not None
    return match.group(1)


def _portable_package(name: str, *sections: PluginSetupSection) -> ProductSetupPackage:
    return ProductSetupPackage(
        product_setup_product(
            Product(
                name.casefold().replace(" ", "-"),
                name,
                executable_path=r"C:\Portable\App\app.exe",
                working_directory=r"C:\Portable\App",
            )
        ),
        tuple(sections),
    )


def _review_form(source_token: str, index: int, name: str) -> dict[str, object]:
    return {
        "source_token": source_token,
        "selected_indices": str(index),
        f"product_{index}_name": name,
        f"product_{index}_executable_path": rf"C:\Local\{name}\app.exe",
        f"product_{index}_working_directory": rf"C:\Local\{name}",
    }


def test_product_creation_derives_editable_defaults_from_executable(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    response = app.test_client().post(
        "/products/new",
        data={"executable_path": r'"C:\Apps\Sample Tool\sample.exe"'},
    )
    created = next(
        item
        for item in products(app).list_all()
        if item.id not in {"sample", "other"}
    )

    assert response.status_code == 302
    assert created.name == "sample"
    assert created.executable_path == r"C:\Apps\Sample Tool\sample.exe"
    assert created.working_directory == r"C:\Apps\Sample Tool"
    page = app.test_client().get("/products/new").get_data(as_text=True)
    assert page.index('id="executable_path"') < page.index('id="name"')
    assert "product_form.js" in page


def test_package_roundtrip_validates_schema_duplicates_and_portable_paths() -> None:
    setup_product = product_setup_product(
        Product(
            "sample",
            "Sample",
            executable_path=r"C:\Apps\Sample\sample.exe",
            working_directory=r"C:\Apps\Sample",
        )
    )
    package = ProductSetupPackage(
        setup_product,
        (PluginSetupSection("example-plugin", 1, {"enabled": True}),),
    )

    restored = ProductSetupPackage.from_dict(package.to_dict())

    assert restored == package
    assert restored.product.executable_path == PortablePath(
        r"C:\Apps\Sample\sample.exe", "sample.exe"
    )
    assert restored.product.working_directory == PortablePath(
        r"C:\Apps\Sample", "."
    )
    with pytest.raises(ValueError):
        ProductSetupPackage.from_dict({**package.to_dict(), "schema_version": 99})
    with pytest.raises(ValueError):
        ProductSetupPackage(
            setup_product,
            (
                PluginSetupSection("duplicate", 1, {}),
                PluginSetupSection("duplicate", 1, {}),
            ),
        )
    with pytest.raises(ValueError):
        PortablePath(r"C:\Apps\Sample", "../outside")


def test_bundle_roundtrip_preserves_order_and_rejects_ambiguous_entries() -> None:
    first = ProductSetupPackage(product_setup_product(Product("one", "First")))
    second = ProductSetupPackage(product_setup_product(Product("two", "Second")))
    bundle = ProductSetupBundle((first, second))

    restored = ProductSetupBundle.from_dict(bundle.to_dict())

    assert restored == bundle
    assert [item.product.name for item in restored.packages] == ["First", "Second"]
    assert restored.to_dict()["document_type"] == "product_setup_bundle"
    with pytest.raises(ValueError):
        ProductSetupBundle(())
    with pytest.raises(ValueError):
        ProductSetupBundle((first, first))
    with pytest.raises(ValueError):
        ProductSetupBundle(
            (
                first,
                ProductSetupPackage(
                    product_setup_product(Product("other", " first "))
                ),
            )
        )
    with pytest.raises(ValueError):
        ProductSetupBundle.from_dict(
            {**bundle.to_dict(), "document_type": "product_setup_package"}
        )
    invalid_package = first.to_dict()
    invalid_package["schema_version"] = 99
    with pytest.raises(ValueError):
        ProductSetupBundle.from_dict(
            {**bundle.to_dict(), "packages": [invalid_package]}
        )


def test_export_is_deterministic_product_scoped_and_contains_no_runtime_data(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    configurations(app).upsert(
        PluginConfiguration(
            "sample",
            "log-collector",
            True,
            {"log_directories": [str(tmp_path / "sample-logs")]},
        )
    )
    configurations(app).upsert(
        PluginConfiguration(
            "other",
            "log-collector",
            True,
            {"log_directories": [str(tmp_path / "other-private-logs")]},
        )
    )
    configurations(app).upsert(
        PluginConfiguration(
            "sample",
            "license-manager",
            False,
            {"license_directory": "", "license_files": []},
        )
    )
    configurations(app).upsert(
        PluginConfiguration(
            "sample",
            "windows-registry",
            False,
            {"value_targets": [], "branch_targets": [], "presets": []},
        )
    )
    client = app.test_client()

    first = client.get("/products/sample/setup/export")
    second = client.get("/products/sample/setup/export")
    exported = json.loads(first.get_data(as_text=True))

    assert first.status_code == 200 and first.data == second.data
    assert first.headers["Content-Disposition"].endswith(
        'Sample-Product-setup.json"'
    )
    assert [item["plugin_identifier"] for item in exported["plugin_sections"]] == [
        "license-manager",
        "log-collector",
        "windows-registry",
    ]
    registry_section = next(
        item
        for item in exported["plugin_sections"]
        if item["plugin_identifier"] == "windows-registry"
    )
    assert registry_section["data"]["presets_omitted"] == 0
    assert "presets" not in registry_section["data"]
    text = first.get_data(as_text=True)
    assert "other-private-logs" not in text
    assert all(
        term not in text for term in ("snapshots", "operation_logs", "backup")
    )


def test_single_product_export_failure_renders_contextual_product_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)

    def fail_export(self, product):
        del self, product
        raise RuntimeError("private export failure")

    monkeypatch.setattr(ProductSetupService, "export", fail_export)

    response = app.test_client().get("/products/sample/setup/export")
    page = response.get_data(as_text=True)

    assert response.status_code == 503
    assert "Sample Product" in page
    assert "Не вдалося створити файл налаштувань Product Setup" in page
    assert "Експорт налаштувань" in page
    assert "private export failure" not in page


def test_bundle_export_is_deterministic_and_uses_selected_product_subset(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    first = client.post(
        "/products/setup/export",
        data={"product_ids": ["other", "sample"]},
    )
    second = client.post(
        "/products/setup/export",
        data={"product_ids": ["sample", "other"]},
    )
    subset = client.post(
        "/products/setup/export",
        data={"product_ids": ["other"]},
    )

    assert first.status_code == 200 and first.data == second.data
    assert first.headers["Content-Disposition"].endswith(
        'qa-deck-setup-bundle.json"'
    )
    assert [
        item["product"]["name"]
        for item in json.loads(first.get_data(as_text=True))["packages"]
    ] == ["Sample Product", "Other Product"]
    assert [
        item["product"]["name"]
        for item in json.loads(subset.get_data(as_text=True))["packages"]
    ] == ["Other Product"]
    assert client.post("/products/setup/export", data={}).status_code == 400
    assert (
        client.post(
            "/products/setup/export",
            data={"product_ids": ["sample", "missing"]},
        ).status_code
        == 400
    )


def test_plugin_capability_failure_is_isolated_and_reported(tmp_path: Path) -> None:
    class BrokenPlugin:
        identifier = "broken-setup"
        display_name = "Broken Setup"
        description = "Broken"
        version = "1"

        def get_actions(self):
            return []

        def export_product_setup(self, product, configuration):
            raise RuntimeError("private failure")

    manager = PluginManager()
    manager.register(BrokenPlugin())
    repository = PluginConfigurationRepository(tmp_path / "configurations.json")
    repository.upsert(PluginConfiguration("sample", "broken-setup", True, {}))
    service = ProductSetupService(manager, repository)

    package = service.export(Product("sample", "Sample"))
    bundle = ProductSetupBundle(
        (package, service.export(Product("other", "Other")))
    )

    assert package.plugin_sections == ()
    assert package.omitted_plugins == ("broken-setup",)
    assert bundle.packages[0].omitted_plugins == ("broken-setup",)
    assert bundle.packages[1].omitted_plugins == ()


def test_import_preview_rejects_malformed_and_performs_no_writes(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    package = ProductSetupPackage(
        product_setup_product(Product("portable", "Portable Product")),
        (PluginSetupSection("unavailable-plugin", 1, {}),),
    )
    product_path = tmp_path / "products.json"
    configuration_path = tmp_path / "configurations.json"
    products_before = product_path.read_bytes()
    configurations_before = (
        configuration_path.read_bytes() if configuration_path.exists() else None
    )

    malformed = client.post(
        "/product-setup/import/configure",
        data={"setup_file": (BytesIO(b"{broken"), "broken.json")},
    )
    wrong_version_data = package.to_dict()
    wrong_version_data["schema_version"] = 2
    wrong_version = client.post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(wrong_version_data).encode()),
                "future.json",
            )
        },
    )
    preview = client.post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(package.to_dict()).encode()),
                "setup.json",
            )
        },
    )

    assert malformed.status_code == 400 and wrong_version.status_code == 400
    assert preview.status_code == 200
    page = preview.get_data(as_text=True)
    assert "Налаштування імпорту" in page
    assert "Вкажіть локальний шлях до executable" in page
    assert "Не підтримується" in page
    assert product_path.read_bytes() == products_before
    assert (
        configuration_path.read_bytes() if configuration_path.exists() else None
    ) == configurations_before
    assert products(app).get("portable") is None


def test_import_accepts_bundle_and_reuses_package_preview_without_writes(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    bundle = ProductSetupBundle(
        (
            ProductSetupPackage(
                product_setup_product(Product("first", "First Portable")),
                (PluginSetupSection("unavailable-plugin", 1, {}),),
            ),
            ProductSetupPackage(
                product_setup_product(
                    Product(
                        "second",
                        "Second Portable",
                        executable_path=r"\\server\share\second.exe",
                    )
                )
            ),
        )
    )
    product_path = tmp_path / "products.json"
    configuration_path = tmp_path / "configurations.json"
    products_before = product_path.read_bytes()
    configurations_before = (
        configuration_path.read_bytes() if configuration_path.exists() else None
    )

    response = client.post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(bundle.to_dict()).encode()),
                "bundle.json",
            )
        },
    )
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Налаштування імпорту" in page
    assert "First Portable" in page and "Second Portable" in page
    assert "Вибрано:" in page and "Очистити вибір" in page
    assert "Не підтримується" in page
    assert "data-selectable-item" in page
    assert "Продовжити імпорт" in page
    assert "data-dialog-auto-open" not in page
    source_token = re.search(
        r'name="source_token" value="([^"]+)"', page
    )
    assert source_token is not None
    assert "Шлях не перевірено з міркувань безпеки" in page
    assert product_path.read_bytes() == products_before
    assert (
        configuration_path.read_bytes() if configuration_path.exists() else None
    ) == configurations_before
    assert products(app).get("first") is None


def test_existing_name_conflict_is_visible_on_first_configuration_page(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    package = _portable_package(
        "Sample Product",
        PluginSetupSection(
            "windows-registry",
            1,
            {
                "enabled": False,
                "value_targets": [],
                "branch_targets": [],
                "presets_omitted": 0,
            },
        ),
    )
    response = app.test_client().post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(package.to_dict()).encode()),
                "setup.json",
            )
        },
    )
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Product із такою назвою вже існує" in page
    assert "field-attention" in page
    assert 'class="setup-plugin-details" open' not in page
    assert "data-dialog-auto-open" not in page


def test_plugin_import_warning_opens_plugin_details(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    document = ProductSetupBundle(
        (
            _portable_package(
                "Unsupported Plugin Product",
                PluginSetupSection("not-installed", 1, {}),
            ),
            _portable_package(
                "Ready Plugin Product",
                PluginSetupSection(
                    "windows-registry",
                    1,
                    {
                        "enabled": False,
                        "value_targets": [],
                        "branch_targets": [],
                        "presets_omitted": 0,
                    },
                ),
            ),
        )
    )

    response = app.test_client().post(
        "/product-setup/import/configure",
        data={
            "setup_file": (
                BytesIO(json.dumps(document.to_dict()).encode()),
                "setup.json",
            )
        },
    )
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert page.count('class="setup-plugin-details"') == 2
    assert page.count('class="setup-plugin-details" open') == 1
    assert "Плагін недоступний або не підтримує імпорт" in page


def test_import_rejects_ambiguous_or_duplicate_bundle_documents(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    package = ProductSetupPackage(
        product_setup_product(Product("portable", "Portable"))
    )
    duplicate = ProductSetupBundle((package,)).to_dict()
    duplicate["packages"] = [package.to_dict(), package.to_dict()]

    for document in (
        {**package.to_dict(), "packages": []},
        duplicate,
        {
            "document_type": "product_setup_bundle",
            "schema_version": 99,
            "packages": [package.to_dict()],
        },
    ):
        response = client.post(
        "/product-setup/import/configure",
            data={
                "setup_file": (
                    BytesIO(json.dumps(document).encode()),
                    "invalid.json",
                )
            },
        )
        assert response.status_code == 400


def test_confirmed_single_import_persists_product_and_builtin_configurations_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)
    manager = app.extensions["plugin_manager"]
    license_plugin = manager.get("license-manager")
    log_plugin = manager.get("log-collector")
    registry_plugin = manager.get("windows-registry")

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("Runtime capability must not run during setup import")

    monkeypatch.setattr(license_plugin, "execute", forbidden)
    monkeypatch.setattr(license_plugin, "inspect", forbidden)
    monkeypatch.setattr(log_plugin, "collect", forbidden)
    monkeypatch.setattr(log_plugin, "inspect", forbidden)
    monkeypatch.setattr(registry_plugin, "execute_preset", forbidden)
    monkeypatch.setattr(registry_plugin, "inspect", forbidden)
    license_directory = tmp_path / "licenses"
    log_directory = tmp_path / "logs"
    license_directory.mkdir()
    log_directory.mkdir()
    license_file = license_directory / "license.dat"
    log_file = log_directory / "application.log"
    license_file.write_bytes(b"license-state")
    log_file.write_bytes(b"log-state")
    package = _portable_package(
        "Imported Product",
        PluginSetupSection(
            "license-manager",
            1,
            {
                "enabled": True,
                "license_directory": PortablePath(r"C:\Portable\licenses").to_dict(),
                "license_files": ["license.dat"],
            },
        ),
        PluginSetupSection(
            "log-collector",
            1,
            {
                "enabled": True,
                "log_directories": [
                    PortablePath(r"C:\Portable\logs").to_dict()
                ],
            },
        ),
        PluginSetupSection(
            "windows-registry",
            1,
            {
                "enabled": True,
                "value_targets": [
                    {
                        "id": "theme",
                        "hive": "HKCU",
                        "key_path": r"Software\Example",
                        "value_name": "Theme",
                        "display_name": "Theme",
                        "enabled": True,
                    }
                ],
                "branch_targets": [],
                "presets_omitted": 0,
            },
        ),
    )
    client = app.test_client()
    source_token = _upload_setup(client, package.to_dict())
    form = _review_form(source_token, 0, "Imported Product")
    form.update(
        {
            "plugin_0_license-manager_license_directory": str(
                license_directory
            ),
            "plugin_0_log-collector_log_directory_0": str(log_directory),
        }
    )
    review = client.post("/product-setup/import/configure/validate", data=form)
    token = _confirmation_token(review)
    review_page = review.get_data(as_text=True)

    assert "Налаштування імпорту" in review_page
    assert "data-dialog-auto-open" in review_page
    assert "Фінальна перевірка" not in review_page

    confirmation = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )

    assert confirmation.status_code == 302
    result = client.get(confirmation.headers["Location"])
    assert result.status_code == 200
    assert "Створено" in result.get_data(as_text=True)
    imported = next(
        item for item in products(app).list_all() if item.name == "Imported Product"
    )
    imported_configurations = configurations(app).list_for_product(imported.id)
    assert {item.plugin_identifier for item in imported_configurations} == {
        "license-manager",
        "log-collector",
        "windows-registry",
    }
    assert next(
        item
        for item in imported_configurations
        if item.plugin_identifier == "license-manager"
    ).settings["license_directory"] == str(tmp_path / "licenses")
    assert next(
        item
        for item in imported_configurations
        if item.plugin_identifier == "log-collector"
    ).settings["log_directories"] == [str(tmp_path / "logs")]
    assert next(
        item
        for item in imported_configurations
        if item.plugin_identifier == "windows-registry"
    ).settings["presets"] == []
    assert license_file.read_bytes() == b"license-state"
    assert log_file.read_bytes() == b"log-state"


def test_bundle_subset_and_renamed_conflict_use_shared_import_pipeline(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    bundle = ProductSetupBundle(
        (
            _portable_package("Sample Product"),
            _portable_package("Bundle Second"),
        )
    )
    source_token = _upload_setup(client, bundle.to_dict())

    conflict = client.post(
        "/product-setup/import/configure/validate",
        data=_review_form(source_token, 0, "Sample Product"),
    )
    assert conflict.status_code == 400
    assert "вже існує" in conflict.get_data(as_text=True)

    renamed = client.post(
        "/product-setup/import/configure/validate",
        data=_review_form(source_token, 0, "Sample Product Copy"),
    )
    token = _confirmation_token(renamed)
    response = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )

    assert response.status_code == 302
    names = [item.name for item in products(app).list_all()]
    assert "Sample Product Copy" in names
    assert "Bundle Second" not in names


def test_duplicate_final_names_and_invalid_adaptation_are_rejected(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    bundle = ProductSetupBundle(
        (_portable_package("First Import"), _portable_package("Second Import"))
    )
    source_token = _upload_setup(client, bundle.to_dict())
    form = {
        **_review_form(source_token, 0, "Duplicate"),
        **{
            key: value
            for key, value in _review_form(source_token, 1, "Duplicate").items()
            if key != "source_token"
        },
        "selected_indices": ["0", "1"],
    }

    duplicate = client.post(
        "/product-setup/import/configure/validate", data=form
    )
    invalid = client.post(
        "/product-setup/import/configure/validate",
        data={
            **_review_form(source_token, 0, "Invalid\nName"),
            "product_0_executable_path": "x" * 4097,
        },
    )

    assert duplicate.status_code == 400
    assert "повторюється" in duplicate.get_data(as_text=True)
    assert invalid.status_code == 400
    assert "некорект" in invalid.get_data(as_text=True)
    assert len(products(app).list_all()) == 2


def test_confirmation_is_one_time_and_result_refresh_is_safe(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    source_token = _upload_setup(
        client, _portable_package("One Time Import").to_dict()
    )
    token = _confirmation_token(
        client.post(
        "/product-setup/import/configure/validate",
            data=_review_form(source_token, 0, "One Time Import"),
        )
    )

    first = client.post(
        "/product-setup/import/confirm",
        data={
            "confirmation_token": token,
            "confirm": "yes",
            "product_0_name": "Tampered Name",
        },
    )
    reused = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )
    count_after = len(products(app).list_all())
    first_refresh = client.get(first.headers["Location"])
    second_refresh = client.get(first.headers["Location"])

    assert first.status_code == 302 and reused.status_code == 409
    assert first_refresh.status_code == 200 and second_refresh.status_code == 200
    assert len(products(app).list_all()) == count_after == 3
    assert any(item.name == "One Time Import" for item in products(app).list_all())
    assert not any(item.name == "Tampered Name" for item in products(app).list_all())


def test_stale_name_conflict_blocks_one_product_but_allows_independent_product(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    bundle = ProductSetupBundle(
        (_portable_package("Stale Import"), _portable_package("Independent Import"))
    )
    source_token = _upload_setup(client, bundle.to_dict())
    first_form = _review_form(source_token, 0, "Stale Import")
    second_form = _review_form(source_token, 1, "Independent Import")
    form = {
        **first_form,
        **{key: value for key, value in second_form.items() if key != "source_token"},
        "selected_indices": ["0", "1"],
    }
    token = _confirmation_token(
        client.post("/product-setup/import/configure/validate", data=form)
    )
    products(app).add(Product("conflict", "Stale Import"))

    confirmation = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )
    result = client.get(confirmation.headers["Location"]).get_data(as_text=True)

    assert "Заблоковано" in result and "Створено" in result
    assert sum(item.name == "Stale Import" for item in products(app).list_all()) == 1
    assert any(
        item.name == "Independent Import" for item in products(app).list_all()
    )


def test_unsupported_plugin_is_explicit_but_does_not_block_product_import(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    package = _portable_package(
        "Unsupported Import",
        PluginSetupSection("unavailable-plugin", 1, {}),
    )
    source_token = _upload_setup(client, package.to_dict())
    review = client.post(
        "/product-setup/import/configure/validate",
        data=_review_form(source_token, 0, "Unsupported Import"),
    )
    page = review.get_data(as_text=True)

    assert review.status_code == 200
    assert "Не підтримується" in page
    token = _confirmation_token(review)
    confirmation = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )
    result = client.get(confirmation.headers["Location"]).get_data(as_text=True)
    assert "Створено" in result and "Не підтримується" in result


def test_plugin_import_preparation_failure_is_isolated_and_blocks_confirmation(
    tmp_path: Path,
) -> None:
    class BrokenImportPlugin:
        identifier = "broken-import"
        display_name = "Broken Import"
        description = "Broken"
        version = "1"

        def get_actions(self):
            return []

        def preview_product_setup(self, product, section):
            del product, section
            return SetupPluginPreview(
                self.identifier, self.display_name, "supported"
            )

        def prepare_product_setup_import(self, product, section):
            del product, section
            raise RuntimeError("private provider failure")

        def build_product_setup_configuration(
            self, product_id, product, section, adapted_values
        ):
            del product_id, product, section, adapted_values
            raise RuntimeError("private provider failure")

    app = make_app(tmp_path)
    app.extensions["plugin_manager"].register(BrokenImportPlugin())
    client = app.test_client()
    package = _portable_package(
        "Broken Provider Import",
        PluginSetupSection("broken-import", 1, {}),
    )
    source_token = _upload_setup(client, package.to_dict())

    review = client.post(
        "/product-setup/import/configure/validate",
        data=_review_form(source_token, 0, "Broken Provider Import"),
    )

    assert review.status_code == 400
    assert "Потребує виправлення" in review.get_data(as_text=True)
    assert "data-dialog-auto-open" not in review.get_data(as_text=True)
    assert not any(
        item.name == "Broken Provider Import" for item in products(app).list_all()
    )


def test_configuration_persistence_failure_rolls_back_one_product_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ImportPlugin:
        identifier = "import-plugin"
        display_name = "Import Plugin"
        description = "Import"
        version = "1"

        def get_actions(self):
            return []

        def preview_product_setup(self, product, section):
            del product, section
            return SetupPluginPreview(
                self.identifier, self.display_name, "supported"
            )

        def prepare_product_setup_import(self, product, section):
            del product, section
            return SetupPluginImportPreparation(
                self.identifier, self.display_name, "supported"
            )

        def build_product_setup_configuration(
            self, product_id, product, section, adapted_values
        ):
            del product, section, adapted_values
            return PluginConfiguration(product_id, self.identifier, True, {})

    app = make_app(tmp_path)
    app.extensions["plugin_manager"].register(ImportPlugin())
    repository = configurations(app)
    original_upsert = repository.upsert
    calls = 0

    def fail_first(configuration):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private persistence failure")
        original_upsert(configuration)

    monkeypatch.setattr(repository, "upsert", fail_first)
    section = PluginSetupSection("import-plugin", 1, {})
    bundle = ProductSetupBundle(
        (
            _portable_package("Rollback Import", section),
            _portable_package("Continued Import", section),
        )
    )
    client = app.test_client()
    source_token = _upload_setup(client, bundle.to_dict())
    first_form = _review_form(source_token, 0, "Rollback Import")
    second_form = _review_form(source_token, 1, "Continued Import")
    form = {
        **first_form,
        **{key: value for key, value in second_form.items() if key != "source_token"},
        "selected_indices": ["0", "1"],
    }
    token = _confirmation_token(
        client.post("/product-setup/import/configure/validate", data=form)
    )

    confirmation = client.post(
        "/product-setup/import/confirm",
        data={"confirmation_token": token, "confirm": "yes"},
    )
    result = client.get(confirmation.headers["Location"]).get_data(as_text=True)

    assert "Не створено" in result and "Створено" in result
    assert not any(
        item.name == "Rollback Import" for item in products(app).list_all()
    )
    created = next(
        item for item in products(app).list_all() if item.name == "Continued Import"
    )
    assert configurations(app).get(created.id, "import-plugin") is not None
