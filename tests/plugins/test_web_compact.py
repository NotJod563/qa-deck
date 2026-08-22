"""Compact Product/plugin web integration scenarios."""

import io
import re
from pathlib import Path
from zipfile import ZipFile

from qa_deck.domain import PluginConfiguration
from tests.helpers import configurations, make_app


def test_product_page_has_plugins_anchors_without_empty_result_slots(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    for plugin_name in (
        "Executable Inspector",
        "License Manager",
        "Log Collector",
        "Windows Registry",
    ):
        assert plugin_name in page
    for anchor in (
        "product-overview",
        "executable-inspector",
        "license-manager",
        "log-collector",
        "windows-registry",
            "operation-logs",
            "environment-profiles",
    ):
        assert page.count(f'id="{anchor}"') == 1
    assert 'data-plugin-result="license-manager"' not in page
    assert 'data-plugin-result="log-collector"' not in page
    assert "Оберіть дію, щоб побачити поточний результат" not in page
    assert "Перевірку джерел логів ще не запускали" not in page
    assert "Перевірку ще не запускали" not in page
    assert "Файл не запускається, його вміст не читається" not in page
    assert page.count('<details class="plugin-settings') == 6
    assert page.count('class="plugin-launcher-tile"') == 6
    assert "ІНСТРУМЕНТИ СТАНУ" in page


def test_plugin_configurations_are_saved_only_for_selected_product(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    licenses = tmp_path / "licenses"
    logs = tmp_path / "logs"
    licenses.mkdir()
    logs.mkdir()

    license_response = client.post(
        "/products/sample/plugins/license-manager/configuration",
        data={
            "enabled": "on",
            "license_directory": str(licenses),
            "license_files": "license.dat",
        },
    )
    log_response = client.post(
        "/products/sample/plugins/log-collector/configuration",
        data={"enabled": "on", "log_directories": str(logs)},
    )

    assert "open=license-manager-settings" in license_response.location
    assert license_response.location.endswith("#license-manager")
    assert "open=log-collector-settings" in log_response.location
    assert log_response.location.endswith("#log-collector")
    license_page = client.get(license_response.location).get_data(as_text=True)
    log_page = client.get(log_response.location).get_data(as_text=True)
    assert '<details id="license-manager" class="plugin-workspace" open>' in (
        license_page
    )
    assert '<details class="plugin-settings" open>' in license_page[
        license_page.index('id="license-manager"') : license_page.index(
            'id="snapshots"'
        )
    ]
    assert '<details id="log-collector" class="plugin-workspace" open>' in log_page
    assert '<details class="plugin-settings" open>' in log_page[
        log_page.index('id="log-collector"') : log_page.index(
            'id="windows-registry"'
        )
    ]
    assert configurations(app).get("sample", "license-manager") is not None
    assert configurations(app).get("sample", "log-collector") is not None
    assert configurations(app).list_for_product("other") == []


def test_license_routes_cover_inspect_preview_confirm_disabled_and_unknown(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    original = licenses / "license.dat"
    original.write_bytes(b"license")
    client.post(
        "/products/sample/plugins/license-manager/configuration",
        data={
            "enabled": "on",
            "license_directory": str(licenses),
            "license_files": "license.dat",
        },
    )

    inspection = client.post(
        "/products/sample/plugins/license-manager/inspect"
    )
    preview = client.post(
        "/products/sample/plugins/license-manager/preview-hide"
    )
    match = re.search(
        r'name="fingerprint" value="([a-f0-9]+)"',
        preview.get_data(as_text=True),
    )
    assert match is not None
    confirmation = client.post(
        "/products/sample/plugins/license-manager/confirm-hide",
        data={"confirm": "yes", "fingerprint": match.group(1)},
    )
    client.post(
        "/products/sample/plugins/license-manager/configuration",
        data={
            "license_directory": str(licenses),
            "license_files": "license.dat",
        },
    )
    disabled = client.post(
        "/products/sample/plugins/license-manager/preview-restore"
    )

    assert "Активний" in inspection.get_data(as_text=True)
    assert original.name + ".hidden" in preview.get_data(as_text=True)
    assert confirmation.status_code == 200
    assert not original.exists()
    assert (licenses / "license.dat.hidden").exists()
    assert "License Manager вимкнений" in disabled.get_data(as_text=True)
    for route in (
        "inspect",
        "preview-hide",
        "confirm-hide",
        "inspect-backup",
    ):
        response = client.post(
            f"/products/unknown/plugins/license-manager/{route}"
        )
        assert response.status_code == 404, route


def test_license_runtime_actions_preserve_workspace_anchor_and_closed_settings(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    licenses = tmp_path / "runtime-licenses"
    licenses.mkdir()
    (licenses / "license.dat").write_bytes(b"license")
    client.post(
        "/products/sample/plugins/license-manager/configuration",
        data={
            "enabled": "on",
            "license_directory": str(licenses),
            "license_files": "license.dat",
        },
    )
    page = client.get("/products/sample").get_data(as_text=True)

    assert (
        'action="/products/sample/plugins/license-manager/inspect'
        '?open=license-manager#license-manager"'
        in page
    )
    inspection = client.post(
        "/products/sample/plugins/license-manager/inspect?open=license-manager"
    )
    inspection_page = inspection.get_data(as_text=True)
    license_section = inspection_page[
        inspection_page.index('id="license-manager"') : inspection_page.index(
            'id="snapshots"'
        )
    ]

    assert '<details id="license-manager" class="plugin-workspace" open>' in (
        inspection_page
    )
    assert 'data-plugin-result="license-manager"' in license_section
    assert "Стан ліцензій" in license_section
    assert '<details class="plugin-settings" open>' not in license_section

    preview = client.post(
        "/products/sample/plugins/license-manager/preview-hide?open=license-manager"
    )
    preview_page = preview.get_data(as_text=True)
    match = re.search(
        r'name="fingerprint" value="([a-f0-9]+)"', preview_page
    )
    assert match is not None
    assert '<details id="license-manager" class="plugin-workspace" open>' in (
        preview_page
    )
    assert "План змін" in preview_page
    assert "?open=license-manager#license-manager" in preview_page

    result = client.post(
        "/products/sample/plugins/license-manager/confirm-hide?open=license-manager",
        data={"confirm": "yes", "fingerprint": match.group(1)},
    )
    result_page = result.get_data(as_text=True)
    result_section = result_page[
        result_page.index('id="license-manager"') : result_page.index(
            'id="snapshots"'
        )
    ]
    assert '<details id="license-manager" class="plugin-workspace" open>' in (
        result_page
    )
    assert "Результат операції" in result_section
    assert '<details class="plugin-settings" open>' not in result_section

    backup = client.post(
        "/products/sample/plugins/license-manager/inspect-backup?open=license-manager"
    ).get_data(as_text=True)
    assert '<details id="license-manager" class="plugin-workspace" open>' in backup
    assert "Стан backup" in backup


def test_log_runtime_inspection_preserves_workspace_and_anchor(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    logs = tmp_path / "runtime-logs"
    logs.mkdir()
    (logs / "application.log").write_bytes(b"log")
    client.post(
        "/products/sample/plugins/log-collector/configuration",
        data={"enabled": "on", "log_directories": str(logs)},
    )
    page = client.get("/products/sample").get_data(as_text=True)

    assert (
        'action="/products/sample/plugins/log-collector/inspect'
        '?open=log-collector#log-collector"'
        in page
    )
    inspection = client.post(
        "/products/sample/plugins/log-collector/inspect?open=log-collector"
    )
    inspection_page = inspection.get_data(as_text=True)
    log_section = inspection_page[
        inspection_page.index('id="log-collector"') : inspection_page.index(
            'id="windows-registry"'
        )
    ]

    assert '<details id="log-collector" class="plugin-workspace" open>' in (
        inspection_page
    )
    assert 'data-plugin-result="log-collector"' in log_section
    assert "Результат перевірки" in log_section
    assert '<details class="plugin-settings" open>' not in log_section


def test_log_download_is_zip_and_page_uses_operation_history_name(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    source = tmp_path / "logs"
    source.mkdir()
    (source / "application.log").write_bytes(b"product log")
    client.post(
        "/products/sample/plugins/log-collector/configuration",
        data={"enabled": "on", "log_directories": str(source)},
    )

    response = client.post("/products/sample/plugins/log-collector/collect")

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    assert "attachment" in response.headers["Content-Disposition"]
    with ZipFile(io.BytesIO(response.get_data())) as archive:
        assert "source-01/application.log" in archive.namelist()
        assert "manifest.json" in archive.namelist()
    page = client.get("/products/sample").get_data(as_text=True)
    assert "Історія операцій QA Deck" in page
    assert "Збір логів" in page


def test_configuration_errors_open_only_the_affected_plugin(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    repository = configurations(app)
    repository.upsert(
        PluginConfiguration(
            "sample",
            "license-manager",
            True,
            {"license_directory": "", "license_files": []},
        )
    )
    repository.upsert(
        PluginConfiguration(
            "sample",
            "log-collector",
            True,
            {"log_directories": [str(tmp_path / "logs")]},
        )
    )
    client = app.test_client()

    license_page = client.get("/products/sample").get_data(as_text=True)

    assert "Конфігурація License Manager некоректна" in license_page
    assert '<details id="license-manager" class="plugin-workspace" open>' in (
        license_page
    )
    assert '<details id="log-collector" class="plugin-workspace" open>' not in (
        license_page
    )

    repository.upsert(
        PluginConfiguration(
            "sample",
            "license-manager",
            False,
            {"license_directory": "", "license_files": []},
        )
    )
    repository.upsert(
        PluginConfiguration("sample", "log-collector", True, {})
    )

    log_page = client.get("/products/sample").get_data(as_text=True)

    assert "Конфігурація Log Collector некоректна" in log_page
    assert '<details id="log-collector" class="plugin-workspace" open>' in log_page
    assert '<details id="license-manager" class="plugin-workspace" open>' not in (
        log_page
    )
