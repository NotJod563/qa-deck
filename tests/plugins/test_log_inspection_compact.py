"""Representative bounded Log Collector inspection."""

from pathlib import Path
from unittest.mock import patch

from qa_deck.plugins.builtin.log_collector import LogCollector
from tests.helpers import log_configuration


def test_existing_log_source_reports_count_size_and_latest_time(
    tmp_path: Path,
) -> None:
    source = tmp_path / "logs"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "one.log").write_bytes(b"123")
    (nested / "two.log").write_bytes(b"45")

    result = LogCollector().inspect(log_configuration(source))

    assert result.enabled is True
    assert len(result.sources) == 1
    inspected = result.sources[0]
    assert (inspected.exists, inspected.is_directory) == (True, True)
    assert (inspected.file_count, inspected.total_size) == (2, 5)
    assert inspected.latest_modified is not None


def test_invalid_and_unreadable_log_sources_are_controlled(
    tmp_path: Path,
) -> None:
    regular_file = tmp_path / "file.log"
    regular_file.write_bytes(b"log")
    plugin = LogCollector()
    missing = plugin.inspect(log_configuration(tmp_path / "missing")).sources[0]
    invalid = plugin.inspect(log_configuration(regular_file)).sources[0]
    with patch.object(Path, "lstat", side_effect=PermissionError("private")):
        denied = plugin.inspect(log_configuration(tmp_path)).sources[0]

    assert (missing.exists, invalid.is_directory, denied.exists) == (
        False,
        False,
        None,
    )
    assert all(
        "private" not in (item.message or "")
        for item in (missing, invalid, denied)
    )


def test_bounded_scan_does_not_traverse_link_like_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "logs"
    linked = source / "linked"
    linked.mkdir(parents=True)
    (source / "visible.log").write_bytes(b"visible")
    (linked / "private.log").write_bytes(b"private")
    from qa_deck.plugins.builtin.log_collector import plugin as plugin_module

    real_check = plugin_module._is_link_like
    with patch.object(
        plugin_module,
        "_is_link_like",
        lambda path, metadata: path == linked or real_check(path, metadata),
    ):
        linked_result = LogCollector().inspect(log_configuration(source)).sources[0]
    limited_result = LogCollector(max_entries=1).inspect(
        log_configuration(source)
    ).sources[0]

    assert linked_result.file_count == 1
    assert limited_result.truncated is True
