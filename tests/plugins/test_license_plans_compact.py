"""License Manager Change Plan behavior."""

from pathlib import Path

from qa_deck.domain import OperationStatus
from qa_deck.plugins.builtin.license_manager import LicenseManager
from qa_deck.storage import OperationLogRepository
from tests.helpers import license_configuration


def test_hide_plan_is_read_only_and_contains_expected_changes(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.dat"
    active.write_bytes(b"active")
    (tmp_path / "hidden.dat.hidden").write_bytes(b"hidden")
    configuration = license_configuration(
        tmp_path, ["active.dat", "hidden.dat", "missing.dat"]
    )

    plan = LicenseManager().build_plan(
        "sample", configuration, "hide-licenses"
    )

    assert [(item.source_name, item.target_name) for item in plan.changes] == [
        ("active.dat", "active.dat.hidden")
    ]
    assert len(plan.skipped) == 2
    assert plan.requires_confirmation is True
    assert active.read_bytes() == b"active"
    assert not (tmp_path / "active.dat.hidden").exists()


def test_restore_plan_contains_only_hidden_files(tmp_path: Path) -> None:
    (tmp_path / "active.dat").write_bytes(b"active")
    (tmp_path / "hidden.dat.hidden").write_bytes(b"hidden")
    configuration = license_configuration(
        tmp_path, ["active.dat", "hidden.dat", "missing.dat"]
    )

    plan = LicenseManager().build_plan(
        "sample", configuration, "restore-licenses"
    )

    assert [(item.source_name, item.target_name) for item in plan.changes] == [
        ("hidden.dat.hidden", "hidden.dat")
    ]
    assert len(plan.skipped) == 2


def test_conflict_blocks_change_plan_confirmation(tmp_path: Path) -> None:
    (tmp_path / "license.dat").write_bytes(b"active")
    (tmp_path / "license.dat.hidden").write_bytes(b"hidden")

    plan = LicenseManager().build_plan(
        "sample", license_configuration(tmp_path), "hide-licenses"
    )

    assert plan.blocking_error is not None
    assert plan.has_changes is False
    assert plan.requires_confirmation is True


def test_stale_fingerprint_rejects_execution(tmp_path: Path) -> None:
    original = tmp_path / "license.dat"
    original.write_bytes(b"active")
    plugin = LicenseManager()
    configuration = license_configuration(tmp_path)
    plan = plugin.build_plan("sample", configuration, "hide-licenses")
    original.rename(tmp_path / "license.dat.hidden")

    result = plugin.execute(
        product_id="sample",
        configuration=configuration,
        action_identifier="hide-licenses",
        expected_fingerprint=plan.fingerprint,
        confirmed=True,
        backup_root=tmp_path / "backups",
        operation_logs=OperationLogRepository(tmp_path / "operations.json"),
    )

    assert result.status is OperationStatus.REJECTED
    assert result.stale_plan is True
    assert (tmp_path / "license.dat.hidden").exists()
