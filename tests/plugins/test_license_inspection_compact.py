"""License configuration and inspection behavior."""

from pathlib import Path
from unittest.mock import patch

import pytest

from qa_deck.domain import PluginConfiguration
from qa_deck.plugins.builtin.license_manager import LicenseManager
from qa_deck.plugins.builtin.license_manager.models import (
    LicenseFileStatus,
    LicenseInspectionStatus,
    LicenseManagerConfiguration,
)
from tests.helpers import license_configuration


def test_valid_license_configuration_is_normalized() -> None:
    configuration = LicenseManagerConfiguration.create(
        ' "C:\\ProgramData\\Licenses" ',
        ["License.dat", "license.DAT", "trial.lic", ""],
        enabled=True,
    )

    assert configuration.license_directory == "C:\\ProgramData\\Licenses"
    assert configuration.license_files == ("License.dat", "trial.lic")


def test_unsafe_windows_filenames_are_rejected_in_one_scenario() -> None:
    unsafe_names = [
        "/absolute/license.dat",
        "C:\\absolute\\license.dat",
        "C:file.dat",
        "../license.dat",
        "file.dat:stream",
        "CON",
        "con.txt",
        "LPT1.lic",
        "license.",
        "license ",
        "LICENSE.DAT.HIDDEN",
        'bad"name.dat',
        "bad|name.dat",
        "bad?name.dat",
        "bad*name.dat",
        "null\x00.dat",
        "control\x1f.dat",
        ".",
        "..",
    ]
    for filename in unsafe_names:
        try:
            LicenseManagerConfiguration.create(
                "C:/licenses", [filename], enabled=True
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe filename accepted: {filename!r}")


def test_malformed_persisted_license_settings_are_rejected() -> None:
    empty_product = PluginConfiguration("sample", "license-manager", True, {})
    empty_product.product_id = ""
    invalid_enabled = PluginConfiguration("sample", "license-manager", True, {})
    invalid_enabled.enabled = "false"  # type: ignore[assignment]
    malformed = [
        object(),
        PluginConfiguration("sample", "other", True, {}),
        empty_product,
        invalid_enabled,
        PluginConfiguration(
            "sample",
            "license-manager",
            True,
            {"license_directory": 123, "license_files": ["license.dat"]},
        ),
        PluginConfiguration(
            "sample",
            "license-manager",
            True,
            {"license_directory": "C:/licenses", "license_files": [123]},
        ),
    ]
    for configuration in malformed:
        with pytest.raises(ValueError):
            LicenseManagerConfiguration.from_plugin_configuration(
                configuration  # type: ignore[arg-type]
            )


def test_license_inspection_reports_state_matrix(tmp_path: Path) -> None:
    (tmp_path / "active.dat").write_bytes(b"active")
    (tmp_path / "hidden.dat.hidden").write_bytes(b"hidden")
    (tmp_path / "conflict.dat").write_bytes(b"active")
    (tmp_path / "conflict.dat.hidden").write_bytes(b"hidden")
    configuration = license_configuration(
        tmp_path,
        ["active.dat", "hidden.dat", "missing.dat", "conflict.dat"],
    )

    result = LicenseManager().inspect(configuration)

    assert result.status is LicenseInspectionStatus.READY
    assert {item.filename: item.status for item in result.files} == {
        "active.dat": LicenseFileStatus.ACTIVE,
        "hidden.dat": LicenseFileStatus.HIDDEN,
        "missing.dat": LicenseFileStatus.MISSING,
        "conflict.dat": LicenseFileStatus.CONFLICT,
    }


def test_license_inspection_handles_directory_and_filesystem_failures(
    tmp_path: Path,
) -> None:
    missing = LicenseManager().inspect(
        license_configuration(tmp_path / "missing")
    )
    regular_file = tmp_path / "not-directory"
    regular_file.write_bytes(b"file")
    invalid = LicenseManager().inspect(license_configuration(regular_file))
    with patch.object(Path, "stat", side_effect=PermissionError("private")):
        denied = LicenseManager().inspect(license_configuration(tmp_path))

    assert [missing.status, invalid.status, denied.status] == [
        LicenseInspectionStatus.DIRECTORY_MISSING,
        LicenseInspectionStatus.NOT_A_DIRECTORY,
        LicenseInspectionStatus.ERROR,
    ]
    assert "private" not in (denied.message or "")
