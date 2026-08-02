"""Compact Product/plugin web integration scenarios."""

import io
import re
from pathlib import Path
from zipfile import ZipFile

from tests.helpers import configurations, make_app


def test_product_page_has_plugins_anchors_details_and_result_slots(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    for plugin_name in (
        "Executable Inspector",
        "License Manager",
        "Log Collector",
    ):
        assert plugin_name in page
    for anchor in (
        "product-overview",
        "executable-inspector",
        "license-manager",
        "log-collector",
        "operation-logs",
    ):
        assert page.count(f'id="{anchor}"') == 1
    assert page.count('data-plugin-result="license-manager"') == 1
    assert page.count('data-plugin-result="log-collector"') == 1
    assert page.count('<details class="plugin-settings"') == 2


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

    assert license_response.location.endswith("#license-manager")
    assert log_response.location.endswith("#log-collector")
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

    assert "active" in inspection.get_data(as_text=True)
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
    assert "collect-logs" in page
