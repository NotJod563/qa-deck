"""Bounded and path-safe ZIP collection."""

import json
import shutil
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from qa_deck.domain import OperationStatus, Product
from qa_deck.plugins.builtin.log_collector import LogCollectionService
from qa_deck.plugins.builtin.log_collector import collection as collection_module
from qa_deck.storage import OperationLogRepository
from tests.helpers import log_configuration, make_app


def test_single_source_zip_contains_nested_file_and_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "logs"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "application.log").write_bytes(b"log body")

    result = _service(tmp_path).collect(
        Product("sample", "Sample Product"), log_configuration(source)
    )

    assert result.status is OperationStatus.SUCCESS
    assert result.archive_path is not None
    with ZipFile(result.archive_path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "source-01/nested/application.log",
        }
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["added_file_count"] == 1
    assert manifest["total_size_bytes"] == 8
    _cleanup(result.temporary_directory)


def test_multiple_sources_keep_duplicate_names_under_distinct_prefixes(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "application.log").write_bytes(b"first")
    (second / "application.log").write_bytes(b"second")

    result = _service(tmp_path).collect(
        Product("sample", "Sample Product"), log_configuration(first, second)
    )

    assert result.archive_path is not None
    with ZipFile(result.archive_path) as archive:
        assert archive.read("source-01/application.log") == b"first"
        assert archive.read("source-02/application.log") == b"second"
    _cleanup(result.temporary_directory)


def test_unsafe_and_changed_paths_are_skipped_from_zip(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    linked = source / "linked"
    linked.mkdir(parents=True)
    candidate = source / "application.log"
    candidate.write_bytes(b"before")
    (linked / "private.log").write_bytes(b"private")
    service = _service(tmp_path)
    original_copy = service._stable_copy
    real_link_check = collection_module._is_link_like

    def change_candidate(root, canonical_root, path, scanned):  # noqa: ANN001, ANN202
        path.write_bytes(b"changed and longer")
        return original_copy(root, canonical_root, path, scanned)

    with (
        patch.object(
            collection_module,
            "_is_link_like",
            lambda path, metadata: path == linked
            or real_link_check(path, metadata),
        ),
        patch.object(service, "_stable_copy", change_candidate),
    ):
        result = service.collect(
            Product("sample", "Sample Product"), log_configuration(source)
        )

    assert result.status is OperationStatus.FAILED
    assert result.archive_path is None
    assert {item.relative_path for item in result.skipped} == {
        "application.log",
        "linked",
    }
    for unsafe in (Path("../secret.log"), Path("/absolute.log"), Path("C:log")):
        try:
            collection_module._safe_archive_name("source-01", unsafe)
        except OSError:
            pass
        else:
            raise AssertionError(f"unsafe ZIP path accepted: {unsafe}")


def test_file_and_byte_limits_create_partial_archives(tmp_path: Path) -> None:
    source = tmp_path / "logs"
    source.mkdir()
    (source / "a-small.log").write_bytes(b"123")
    (source / "z-large.log").write_bytes(b"12345")
    scenarios = [(1, 100), (10, 4)]

    for max_files, max_bytes in scenarios:
        service = LogCollectionService(
            max_files=max_files,
            max_total_bytes=max_bytes,
            max_entries=100,
            operation_logs=OperationLogRepository(
                tmp_path / f"operations-{max_files}-{max_bytes}.json"
            ),
        )
        result = service.collect(
            Product("sample", "Sample Product"), log_configuration(source)
        )
        assert result.status is OperationStatus.PARTIAL
        assert result.file_count == 1
        assert result.truncated is True
        with ZipFile(result.archive_path) as archive:  # type: ignore[arg-type]
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["truncated"] is True
        assert manifest["skipped_files"]
        _cleanup(result.temporary_directory)


def test_sources_remain_unchanged_and_response_cleanup_removes_archive(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    source = tmp_path / "logs"
    source.mkdir()
    original = source / "application.log"
    original.write_bytes(b"unchanged")
    before = original.stat()
    app.test_client().post(
        "/products/sample/plugins/log-collector/configuration",
        data={"enabled": "on", "log_directories": str(source)},
    )
    temporary_directory = tmp_path / "temporary-download"

    def controlled_directory(*args, **kwargs) -> str:  # noqa: ANN002, ANN003
        temporary_directory.mkdir()
        return str(temporary_directory)

    with patch.object(
        collection_module.tempfile, "mkdtemp", controlled_directory
    ):
        response = app.test_client().post(
            "/products/sample/plugins/log-collector/collect"
        )
        response.get_data()

    after = original.stat()
    assert response.status_code == 200
    assert not temporary_directory.exists()
    assert original.read_bytes() == b"unchanged"
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def _service(tmp_path: Path) -> LogCollectionService:
    return LogCollectionService(
        max_files=100,
        max_total_bytes=10_000,
        max_entries=100,
        operation_logs=OperationLogRepository(tmp_path / "operations.json"),
    )


def _cleanup(directory: Path | None) -> None:
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)
