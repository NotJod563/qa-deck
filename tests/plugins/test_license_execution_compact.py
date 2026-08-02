"""License execution, backup, rollback, and containment safety."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

import pytest

from qa_deck.domain import OperationStatus, RollbackStatus
from qa_deck.plugins.builtin.license_manager import LicenseManager
from qa_deck.plugins.builtin.license_manager.models import PlannedChange
from qa_deck.plugins.builtin.license_manager.service import (
    LicenseManagerService,
    MoveWithoutOverwriteError,
    move_without_overwrite,
)
from qa_deck.storage import OperationLogRepository
from tests.helpers import license_configuration


def test_hide_creates_backup_manifest_and_hides_file(tmp_path: Path) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    original = licenses / "license.dat"
    original.write_bytes(b"license content")
    backup_root = tmp_path / "backups"
    plugin = LicenseManager()
    configuration = license_configuration(licenses)
    plan = plugin.build_plan("sample", configuration, "hide-licenses")

    result = _execute(
        plugin, configuration, plan.fingerprint, "hide-licenses", backup_root
    )

    assert result.status is OperationStatus.SUCCESS
    assert not original.exists()
    assert (licenses / "license.dat.hidden").read_bytes() == b"license content"
    manifest_path = next(backup_root.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["action_identifier"] == "hide-licenses"
    backup = manifest_path.parent / manifest["files"][0]["backup_name"]
    assert backup.read_bytes() == b"license content"


def test_restore_creates_backup_and_restores_file(tmp_path: Path) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    hidden = licenses / "license.dat.hidden"
    hidden.write_bytes(b"hidden")
    plugin = LicenseManager()
    configuration = license_configuration(licenses)
    plan = plugin.build_plan("sample", configuration, "restore-licenses")

    result = _execute(
        plugin,
        configuration,
        plan.fingerprint,
        "restore-licenses",
        tmp_path / "backups",
    )

    assert result.status is OperationStatus.SUCCESS
    assert not hidden.exists()
    assert (licenses / "license.dat").read_bytes() == b"hidden"
    assert list((tmp_path / "backups").rglob("manifest.json"))


def test_move_destination_is_never_overwritten(tmp_path: Path) -> None:
    for race in (False, True):
        directory = tmp_path / str(race)
        directory.mkdir()
        source = directory / "source.dat"
        destination = directory / "destination.dat"
        source.write_bytes(b"source")
        if not race:
            destination.write_bytes(b"existing")
            with pytest.raises(MoveWithoutOverwriteError):
                move_without_overwrite(source, destination)
        else:
            from qa_deck.plugins.builtin.license_manager import service

            real_link = service.os.link

            def racing_link(path: Path, target: Path, **kwargs: object) -> None:
                target.write_bytes(b"appeared")
                real_link(path, target, **kwargs)

            with (
                patch.object(service.os, "link", racing_link),
                pytest.raises(MoveWithoutOverwriteError),
            ):
                move_without_overwrite(source, destination)
        assert source.read_bytes() == b"source", f"race={race}"
        assert destination.read_bytes() in {b"existing", b"appeared"}


def test_backup_failure_leaves_original_unchanged(tmp_path: Path) -> None:
    original = tmp_path / "license.dat"
    original.write_bytes(b"license")
    plugin = LicenseManager()
    configuration = license_configuration(tmp_path)
    plan = plugin.build_plan("sample", configuration, "hide-licenses")

    result = _execute(
        plugin,
        configuration,
        plan.fingerprint,
        "hide-licenses",
        tmp_path / "backup-inside-license-directory",
    )

    assert result.status is OperationStatus.FAILED
    assert original.read_bytes() == b"license"
    assert not (tmp_path / "license.dat.hidden").exists()


def test_second_move_failure_rolls_back_first(tmp_path: Path) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    for name in ("first.dat", "second.dat"):
        (licenses / name).write_bytes(name.encode())
    plugin = LicenseManager()
    configuration = license_configuration(
        licenses, ["first.dat", "second.dat"]
    )
    plan = plugin.build_plan("sample", configuration, "hide-licenses")
    real_move = move_without_overwrite
    forward_count = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal forward_count
        if destination.name.endswith(".hidden"):
            forward_count += 1
            if forward_count == 2:
                raise MoveWithoutOverwriteError(
                    "simulated", destination_created=False
                )
        real_move(source, destination)

    with patch(
        "qa_deck.plugins.builtin.license_manager.service.move_without_overwrite",
        fail_second,
    ):
        result = _execute(
            plugin,
            configuration,
            plan.fingerprint,
            "hide-licenses",
            tmp_path / "backups",
        )

    assert result.status is OperationStatus.FAILED
    assert result.rollback_status is RollbackStatus.COMPLETE
    assert all((licenses / name).exists() for name in ("first.dat", "second.dat"))


def test_rollback_does_not_overwrite_reappeared_file(tmp_path: Path) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    for name in ("first.dat", "second.dat"):
        (licenses / name).write_bytes(name.encode())
    plugin = LicenseManager()
    configuration = license_configuration(
        licenses, ["first.dat", "second.dat"]
    )
    plan = plugin.build_plan("sample", configuration, "hide-licenses")
    real_move = move_without_overwrite
    forward_count = 0

    def race_rollback(source: Path, destination: Path) -> None:
        nonlocal forward_count
        if destination.name.endswith(".hidden"):
            forward_count += 1
            if forward_count == 2:
                raise MoveWithoutOverwriteError(
                    "second failed", destination_created=False
                )
        elif destination.name == "first.dat":
            destination.write_bytes(b"reappeared")
        real_move(source, destination)

    with patch(
        "qa_deck.plugins.builtin.license_manager.service.move_without_overwrite",
        race_rollback,
    ):
        result = _execute(
            plugin,
            configuration,
            plan.fingerprint,
            "hide-licenses",
            tmp_path / "backups",
        )

    assert result.status is OperationStatus.PARTIAL
    assert result.rollback_status is RollbackStatus.PARTIAL
    assert (licenses / "first.dat").read_bytes() == b"reappeared"
    assert (licenses / "first.dat.hidden").read_bytes() == b"first.dat"


def test_backup_containment_links_and_invalid_manifest_are_controlled(
    tmp_path: Path,
) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    (licenses / "license.dat").write_bytes(b"license")
    service = LicenseManagerService()
    backup_root = tmp_path / "backups"
    product_root = backup_root / "license-manager" / uuid5(
        NAMESPACE_URL, "sample"
    ).hex
    product_root.mkdir(parents=True)

    with (
        patch(
            "qa_deck.plugins.builtin.license_manager.service._is_link_or_junction",
            lambda path: path == product_root,
        ),
        pytest.raises(OSError),
    ):
        _create_backup(service, backup_root, licenses)
    assert (licenses / "license.dat").read_bytes() == b"license"

    invalid_operation = product_root / "invalid"
    invalid_operation.mkdir()
    (invalid_operation / "manifest.json").write_text(
        json.dumps({"timestamp": 123, "action_identifier": [], "files": 1}),
        encoding="utf-8",
    )
    inspection = service.inspect_backup("sample", backup_root)
    assert inspection.has_backup is True
    assert inspection.manifest_available is False
    assert "пошкоджений" in (inspection.message or "")


def _execute(
    plugin: LicenseManager,
    configuration,  # noqa: ANN001
    fingerprint: str,
    action: str,
    backup_root: Path,
):  # noqa: ANN202
    return plugin.execute(
        product_id="sample",
        configuration=configuration,
        action_identifier=action,
        expected_fingerprint=fingerprint,
        confirmed=True,
        backup_root=backup_root,
        operation_logs=OperationLogRepository(backup_root.parent / "logs.json"),
    )


def _create_backup(
    service: LicenseManagerService,
    backup_root: Path,
    licenses: Path,
) -> Path:
    return service._create_backup(
        backup_root=backup_root,
        product_id="sample",
        operation_id="operation-fixed",
        timestamp=datetime(2026, 8, 2, tzinfo=UTC),
        action_identifier="hide-licenses",
        directory=licenses,
        changes=(PlannedChange("license.dat", "license.dat.hidden"),),
    )
