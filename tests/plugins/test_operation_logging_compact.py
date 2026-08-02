"""Operation history outcomes and best-effort persistence."""

from logging import Logger
from pathlib import Path
from unittest.mock import Mock

from qa_deck.domain import OperationStatus, Product
from qa_deck.plugins.builtin.license_manager import LicenseManager
from qa_deck.plugins.builtin.log_collector import LogCollectionService
from qa_deck.storage import OperationLogRepository
from tests.helpers import license_configuration, log_configuration


def test_success_partial_and_failed_collection_statuses_are_logged(
    tmp_path: Path,
) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    (source / "one.log").write_bytes(b"1")
    (source / "two.log").write_bytes(b"2")
    repository = OperationLogRepository(tmp_path / "operations.json")
    product = Product("sample", "Sample Product")

    complete = _collector(repository, max_files=10).collect(
        product, log_configuration(source)
    )
    partial = _collector(repository, max_files=1).collect(
        product, log_configuration(source)
    )
    _collector(repository, max_files=10).collect(product, None)

    assert {log.status for log in repository.list_for_product("sample")} == {
        OperationStatus.SUCCESS,
        OperationStatus.PARTIAL,
        OperationStatus.FAILED,
    }
    assert all(
        log.action_identifier == "collect-logs"
        for log in repository.list_for_product("sample")
    )
    _cleanup(complete.temporary_directory)
    _cleanup(partial.temporary_directory)


def test_operation_log_failure_does_not_mask_license_result(
    tmp_path: Path,
) -> None:
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    original = licenses / "license.dat"
    original.write_bytes(b"license")
    plugin = LicenseManager()
    configuration = license_configuration(licenses)
    plan = plugin.build_plan("sample", configuration, "hide-licenses")
    logger = Mock(spec=Logger)

    class FailingRepository:
        def append(self, operation_log: object) -> None:
            raise OSError("private path")

    result = plugin.execute(
        product_id="sample",
        configuration=configuration,
        action_identifier="hide-licenses",
        expected_fingerprint=plan.fingerprint,
        confirmed=True,
        backup_root=tmp_path / "backups",
        operation_logs=FailingRepository(),  # type: ignore[arg-type]
        logger=logger,
    )

    assert result.status is OperationStatus.SUCCESS
    assert result.operation_log_saved is False
    assert not original.exists()
    assert (licenses / "license.dat.hidden").read_bytes() == b"license"
    assert "журналу" in result.warnings[0]
    logger.exception.assert_called_once()


def _collector(
    repository: OperationLogRepository,
    *,
    max_files: int,
) -> LogCollectionService:
    return LogCollectionService(
        max_files=max_files,
        max_total_bytes=10_000,
        max_entries=100,
        operation_logs=repository,
    )


def _cleanup(directory: Path | None) -> None:
    if directory is not None:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
