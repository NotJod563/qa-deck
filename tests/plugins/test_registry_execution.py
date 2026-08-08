"""Confirmed value-only Windows Registry preset execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from flask import Flask

from qa_deck.domain import PluginConfiguration, Snapshot
from qa_deck.plugins import PluginManager
from qa_deck.plugins.builtin.windows_registry import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryBranchVisibility,
    RegistryDataType,
    RegistryExecutionStateStore,
    RegistryRollbackStatus,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
    WindowsRegistry,
    WindowsRegistryWriter,
)
from qa_deck.snapshot import SnapshotBuilder
from qa_deck.storage import SnapshotRepository
from tests.helpers import (
    configurations,
    license_configuration,
    make_app,
    operation_logs,
    products,
)

VALUE_TARGET = {
    "id": "mode",
    "hive": "HKCU",
    "key_path": "Software\\Example",
    "value_name": "Mode",
    "display_name": "Mode",
    "enabled": True,
}
BRANCH_TARGET = {
    "id": "feature",
    "hive": "HKCU",
    "key_path": "Software\\Example\\Feature",
    "display_name": "Feature",
    "enabled": True,
}


class MutableReader:
    def __init__(
        self,
        registry_type: str,
        value: object,
        *,
        original_exists: bool = True,
        hidden_exists: bool = False,
    ) -> None:
        self.registry_type = registry_type
        self.value = value
        self.original_exists = original_exists
        self.hidden_exists = hidden_exists
        self.branch_read_overrides: list[tuple[bool, bool]] = []
        self.value_reads = 0
        self.branch_reads = 0

    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        self.value_reads += 1
        return RegistryValueInspection(
            target,
            True,
            self.registry_type,
            self.value,
            RegistryValueStatus.AVAILABLE,
            "available",
        )

    def inspect_branch(
        self, target: RegistryBranchTarget
    ) -> RegistryBranchInspection:
        self.branch_reads += 1
        if self.branch_read_overrides:
            original_exists, hidden_exists = self.branch_read_overrides.pop(0)
        else:
            original_exists, hidden_exists = (
                self.original_exists,
                self.hidden_exists,
            )
        status = {
            (True, False): RegistryBranchStatus.VISIBLE,
            (False, True): RegistryBranchStatus.HIDDEN,
            (False, False): RegistryBranchStatus.MISSING,
            (True, True): RegistryBranchStatus.CONFLICT,
        }[(original_exists, hidden_exists)]
        return RegistryBranchInspection(
            target,
            original_exists,
            hidden_exists,
            status,
            status.value,
        )


class FakeWriter:
    def __init__(
        self,
        reader: MutableReader,
        *,
        corrupt_first_write: bool = False,
        fail_rollback: bool = False,
        corrupt_first_branch_read: bool = False,
        fail_branch_rollback: bool = False,
        branch_error: OSError | None = None,
    ) -> None:
        self.reader = reader
        self.corrupt_first_write = corrupt_first_write
        self.fail_rollback = fail_rollback
        self.corrupt_first_branch_read = corrupt_first_branch_read
        self.fail_branch_rollback = fail_branch_rollback
        self.branch_error = branch_error
        self.calls: list[tuple[RegistryValueTarget, RegistryDataType, object]] = []
        self.branch_calls: list[
            tuple[RegistryBranchTarget, RegistryBranchVisibility]
        ] = []

    def set_value(
        self,
        target: RegistryValueTarget,
        registry_type: RegistryDataType,
        value: object,
    ) -> None:
        self.calls.append((target, registry_type, value))
        if self.fail_rollback and len(self.calls) == 2:
            raise OSError("rollback failed")
        self.reader.registry_type = registry_type.value
        self.reader.value = list(value) if isinstance(value, tuple) else value
        if self.corrupt_first_write and len(self.calls) == 1:
            self.reader.value = "unexpected"

    def rename_branch(
        self,
        target: RegistryBranchTarget,
        desired_visibility: RegistryBranchVisibility,
    ) -> None:
        self.branch_calls.append((target, desired_visibility))
        if self.branch_error is not None and len(self.branch_calls) == 1:
            raise self.branch_error
        if self.fail_branch_rollback and len(self.branch_calls) == 2:
            raise OSError("branch rollback failed")
        if desired_visibility is RegistryBranchVisibility.HIDDEN:
            if not self.reader.original_exists or self.reader.hidden_exists:
                raise FileExistsError("unsafe hide")
            self.reader.original_exists = False
            self.reader.hidden_exists = True
        else:
            if self.reader.original_exists or not self.reader.hidden_exists:
                raise FileExistsError("unsafe restore")
            self.reader.original_exists = True
            self.reader.hidden_exists = False
        if self.corrupt_first_branch_read and len(self.branch_calls) == 1:
            self.reader.branch_read_overrides.append((True, True))


def registry_configuration(
    plugin: WindowsRegistry,
    *,
    desired_type: str = "REG_DWORD",
    desired_value: object = 99,
    value_target: dict[str, object] | None = None,
    include_branch: bool = False,
    branch_visibility: str = "hidden",
    branch_target: dict[str, object] | None = None,
) -> PluginConfiguration:
    preset: dict[str, object] = {
        "id": "qa",
        "name": "QA mode",
        "values": [
            {
                "target_id": "mode",
                "registry_type": desired_type,
                "value": desired_value,
            }
        ],
        "branches": (
            [{"target_id": "feature", "visibility": branch_visibility}]
            if include_branch
            else []
        ),
    }
    return plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps([value_target or VALUE_TARGET]),
        branch_targets_json=json.dumps(
            [branch_target or BRANCH_TARGET] if include_branch else []
        ),
        presets_json=json.dumps([preset]),
    )


def execution_intent(
    plugin: WindowsRegistry,
    configuration: PluginConfiguration,
) -> tuple[RegistryExecutionStateStore, object]:
    typed = plugin.typed_configuration(configuration)
    assert typed is not None
    plan = plugin.preview_preset(configuration, "qa")
    store = RegistryExecutionStateStore()
    return store, store.create_intent("sample", typed, plan)


def install_registry(
    app: Flask,
    plugin: WindowsRegistry,
    configuration: PluginConfiguration,
) -> None:
    configurations(app).upsert(configuration)
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    manager._plugins[WindowsRegistry.identifier] = plugin  # noqa: SLF001


def capture_snapshot(app: Flask, label: str = "Registry baseline") -> Snapshot:
    product = products(app).get("sample")
    assert product is not None
    snapshot = SnapshotBuilder(
        cast(PluginManager, app.extensions["plugin_manager"]),
        configurations(app),
    ).build_snapshot(product, label=label)
    repository = cast(SnapshotRepository, app.extensions["snapshot_repository"])
    repository.add(snapshot)
    return snapshot


def snapshot_restore_token(client: object, snapshot: Snapshot) -> tuple[str, str]:
    response = client.get(  # type: ignore[attr-defined]
        f"/products/sample/snapshots/{snapshot.id}/restore-plan"
    )
    html = response.get_data(as_text=True)
    match = re.search(r'name="confirmation_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1), html


def execute_snapshot_restore(
    client: object,
    snapshot: Snapshot,
    token: str,
    **untrusted: str,
) -> object:
    return client.post(  # type: ignore[attr-defined]
        f"/products/sample/snapshots/{snapshot.id}/restore",
        data={"confirmation_token": token, "confirm": "yes", **untrusted},
        follow_redirects=False,
    )


def test_ready_dword_executes_and_exact_readback_succeeds() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.succeeded_count == 1
    assert writer.calls[0][1:] == (RegistryDataType.REG_DWORD, 99)
    assert (reader.registry_type, reader.value) == ("REG_DWORD", 99)


def test_explicit_string_type_remains_distinct_from_integer_type() -> None:
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(
        plugin, desired_type="REG_SZ", desired_value="1"
    )
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.succeeded_count == 1
    assert writer.calls[0][1:] == (RegistryDataType.REG_SZ, "1")
    assert (reader.registry_type, reader.value) == ("REG_SZ", "1")


def test_stale_current_value_prevents_write() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)
    reader.value = 8

    result = plugin.execute_preset(configuration, intent)

    assert result.stale_count == 1
    assert writer.calls == []


def test_configuration_location_drift_prevents_write() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)
    changed = registry_configuration(
        plugin,
        value_target={**VALUE_TARGET, "key_path": "Software\\Other"},
    )

    result = plugin.execute_preset(changed, intent)

    assert result.stale_count == 1
    assert writer.calls == []


def test_removed_target_prevents_write() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)
    removed = plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json="[]",
        branch_targets_json="[]",
        presets_json="[]",
    )

    result = plugin.execute_preset(removed, intent)

    assert result.stale_count == 1
    assert writer.calls == []


def test_one_time_intent_can_only_be_taken_once() -> None:
    reader = MutableReader("REG_DWORD", 7)
    plugin = WindowsRegistry(reader, FakeWriter(reader))
    store, intent = execution_intent(plugin, registry_configuration(plugin))

    assert store.take_intent(intent.token, "sample", "qa") is intent
    assert store.take_intent(intent.token, "sample", "qa") is None


def test_mixed_value_and_visible_to_hidden_execute_without_mutating_preset() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    settings_before = dict(configuration.settings)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.succeeded_count == 2
    assert result.unsupported_count == 0
    assert len(writer.calls) == 1
    assert writer.branch_calls[0][1] is RegistryBranchVisibility.HIDDEN
    assert (reader.original_exists, reader.hidden_exists) == (False, True)
    assert dict(configuration.settings) == settings_before


def test_hidden_to_visible_rename_succeeds_with_exact_readback() -> None:
    reader = MutableReader(
        "REG_DWORD", 99, original_exists=False, hidden_exists=True
    )
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(
        plugin, include_branch=True, branch_visibility="visible"
    )
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.succeeded_count == 1
    assert writer.branch_calls[0][1] is RegistryBranchVisibility.VISIBLE
    assert (reader.original_exists, reader.hidden_exists) == (True, False)


def test_visible_destination_appearing_before_restore_blocks_without_rename() -> None:
    reader = MutableReader(
        "REG_DWORD", 99, original_exists=False, hidden_exists=True
    )
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(
        plugin, include_branch=True, branch_visibility="visible"
    )
    _, intent = execution_intent(plugin, configuration)
    reader.original_exists = True

    result = plugin.execute_preset(configuration, intent)

    assert result.blocked_count == 1
    assert writer.branch_calls == []


@pytest.mark.parametrize(
    ("changed_state", "expected_status", "expected_message"),
    [
        ((True, True), "blocked", "Both visible and hidden Registry branches exist."),
        ((False, False), "blocked", "source is missing"),
        ((False, True), "stale", "changed after preview"),
    ],
)
def test_branch_state_change_after_preview_never_renames(
    changed_state: tuple[bool, bool],
    expected_status: str,
    expected_message: str,
) -> None:
    reader = MutableReader("REG_DWORD", 99)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    _, intent = execution_intent(plugin, configuration)
    reader.original_exists, reader.hidden_exists = changed_state

    result = plugin.execute_preset(configuration, intent)

    branch_result = next(
        entry for entry in result.entries if entry.target_id == "feature"
    )
    assert branch_result.status.value == expected_status
    assert expected_message in branch_result.message
    assert writer.branch_calls == []


def test_branch_configuration_drift_and_removal_never_rename() -> None:
    reader = MutableReader("REG_DWORD", 99)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    _, drift_intent = execution_intent(plugin, configuration)
    changed = registry_configuration(
        plugin,
        include_branch=True,
        branch_target={**BRANCH_TARGET, "key_path": "Software\\Other\\Feature"},
    )

    drift_result = plugin.execute_preset(changed, drift_intent)
    _, removal_intent = execution_intent(plugin, configuration)
    removed = plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps([VALUE_TARGET]),
        branch_targets_json="[]",
        presets_json="[]",
    )
    removal_result = plugin.execute_preset(removed, removal_intent)

    assert drift_result.stale_count == 2
    assert removal_result.stale_count == 2
    assert writer.branch_calls == []


def test_branch_validation_failure_reverse_renames_and_surfaces_rollback() -> None:
    reader = MutableReader("REG_DWORD", 99)
    writer = FakeWriter(reader, corrupt_first_branch_read=True)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    branch_result = next(
        entry for entry in result.entries if entry.target_id == "feature"
    )
    assert branch_result.rollback_status is RegistryRollbackStatus.SUCCEEDED
    assert [call[1] for call in writer.branch_calls] == [
        RegistryBranchVisibility.HIDDEN,
        RegistryBranchVisibility.VISIBLE,
    ]


def test_branch_rollback_failure_is_surfaced() -> None:
    reader = MutableReader("REG_DWORD", 99)
    writer = FakeWriter(
        reader,
        corrupt_first_branch_read=True,
        fail_branch_rollback=True,
    )
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    branch_result = next(
        entry for entry in result.entries if entry.target_id == "feature"
    )
    assert branch_result.rollback_status is RegistryRollbackStatus.FAILED


def test_native_windows_error_is_surfaced_in_execution_result() -> None:
    reader = MutableReader("REG_DWORD", 99)
    writer = FakeWriter(reader, branch_error=OSError(5, "Access is denied"))
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin, include_branch=True)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    branch_result = next(
        entry for entry in result.entries if entry.target_id == "feature"
    )
    assert branch_result.status.value == "failed"
    assert "Windows error 5" in branch_result.message
    assert "Access is denied" in branch_result.message
    assert branch_result.rollback_status is RegistryRollbackStatus.NOT_REQUIRED


def test_failed_post_write_validation_rolls_back_previous_exact_value() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader, corrupt_first_write=True)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.failed_count == 1
    assert result.entries[0].rollback_status is RegistryRollbackStatus.SUCCEEDED
    assert (reader.registry_type, reader.value) == ("REG_DWORD", 7)


def test_rollback_failure_is_surfaced() -> None:
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader, corrupt_first_write=True, fail_rollback=True)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(plugin)
    _, intent = execution_intent(plugin, configuration)

    result = plugin.execute_preset(configuration, intent)

    assert result.failed_count == 1
    assert result.entries[0].rollback_status is RegistryRollbackStatus.FAILED


@dataclass
class _FakeKey:
    handle: int = 123

    def __int__(self) -> int:
        return self.handle

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _FakeWinreg:
    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    KEY_SET_VALUE = 4
    KEY_QUERY_VALUE = 8
    KEY_WRITE = 16
    KEY_CREATE_SUB_KEY = 32
    REG_SZ = 10
    REG_EXPAND_SZ = 11
    REG_DWORD = 12
    REG_QWORD = 13
    REG_MULTI_SZ = 14
    REG_BINARY = 15

    def __init__(self) -> None:
        self.writes: list[tuple[object, str, int, object]] = []
        self.opened: list[tuple[object, str, int]] = []
        self.queried: list[str] = []

    def OpenKey(self, hive: object, path: str, _reserved: int, access: int):
        self.opened.append((hive, path, access))
        return _FakeKey()

    def SetValueEx(
        self,
        _key: object,
        name: str,
        _reserved: int,
        native_type: int,
        value: object,
    ) -> None:
        self.writes.append((_key, name, native_type, value))

    def QueryValueEx(self, _key: object, name: str) -> tuple[object, int]:
        self.queried.append(name)
        return "existing", self.REG_SZ


def test_production_writer_supports_all_typed_values_without_key_creation() -> None:
    native = _FakeWinreg()
    writer = WindowsRegistryWriter(native)
    target = RegistryValueTarget.from_dict(VALUE_TARGET)
    values = (
        (RegistryDataType.REG_SZ, "text"),
        (RegistryDataType.REG_EXPAND_SZ, "%TEMP%"),
        (RegistryDataType.REG_DWORD, 1),
        (RegistryDataType.REG_QWORD, 2),
        (RegistryDataType.REG_MULTI_SZ, ("a", "b")),
        (RegistryDataType.REG_BINARY, "00A1FF"),
    )

    for registry_type, value in values:
        writer.set_value(target, registry_type, value)

    assert len(native.writes) == 6
    assert native.writes[-2][-1] == ["a", "b"]
    assert native.writes[-1][-1] == b"\x00\xa1\xff"
    expected_access = native.KEY_QUERY_VALUE | native.KEY_SET_VALUE
    assert all(item[2] == expected_access for item in native.opened)
    assert native.queried == ["Mode"] * 6
    assert not hasattr(native, "CreateKey")


def test_production_writer_uses_native_parent_relative_rename() -> None:
    native = _FakeWinreg()
    renames: list[tuple[int, str, str]] = []
    writer = WindowsRegistryWriter(
        native,
        lambda parent, source, destination: (
            renames.append((parent, source, destination)) or 0
        ),
    )
    target = RegistryBranchTarget.from_dict(BRANCH_TARGET)

    writer.rename_branch(target, RegistryBranchVisibility.HIDDEN)
    writer.rename_branch(target, RegistryBranchVisibility.VISIBLE)

    assert [item[1:] for item in renames] == [
        ("Feature", "Feature.__qa_deck_hidden__"),
        ("Feature.__qa_deck_hidden__", "Feature"),
    ]
    assert native.opened == [
        (
            native.HKEY_CURRENT_USER,
            "Software\\Example",
            native.KEY_CREATE_SUB_KEY,
        ),
        (_FakeKey(), "Feature", 0x00010000),
        (
            native.HKEY_CURRENT_USER,
            "Software\\Example",
            native.KEY_CREATE_SUB_KEY,
        ),
        (_FakeKey(), "Feature.__qa_deck_hidden__", 0x00010000),
    ]
    assert [item[0] for item in renames] == [123, 123]
    assert all("\\" not in name for item in renames for name in item[1:])
    assert not hasattr(native, "DeleteKey")


def test_native_rename_nonzero_status_is_surfaced_with_windows_diagnostic() -> None:
    native = _FakeWinreg()
    writer = WindowsRegistryWriter(native, lambda _parent, _source, _dest: 5)

    with pytest.raises(OSError) as captured:
        writer.rename_branch(
            RegistryBranchTarget.from_dict(BRANCH_TARGET),
            RegistryBranchVisibility.HIDDEN,
        )

    error_code = getattr(captured.value, "winerror", captured.value.errno)
    assert error_code == 5
    assert "5" in str(captured.value)


def test_native_loader_prefers_reg_rename_key_w(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    import qa_deck.plugins.builtin.windows_registry.writer as writer_module

    class FakeFunction:
        argtypes: object = None
        restype: object = None

        def __call__(self, _parent: int, _source: str, _destination: str) -> int:
            return 0

    class FakeAdvapi32:
        RegRenameKeyW = FakeFunction()

        @property
        def RegRenameKey(self) -> object:
            pytest.fail("unsuffixed fallback must not be selected when W exists")

    monkeypatch.setattr(writer_module.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeAdvapi32(),
        raising=False,
    )

    loaded = WindowsRegistryWriter._native_rename_key()  # noqa: SLF001

    assert loaded is FakeAdvapi32.RegRenameKeyW
    assert loaded.argtypes is not None
    assert loaded.restype is not None


def test_branch_target_rejects_hive_root_child_without_parent() -> None:
    with pytest.raises(ValueError, match="parent and leaf"):
        RegistryBranchTarget.from_dict({**BRANCH_TARGET, "key_path": "Feature"})


def test_non_windows_writer_import_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    import qa_deck.plugins.builtin.windows_registry.writer as writer_module

    monkeypatch.setattr(writer_module.sys, "platform", "linux")
    monkeypatch.setattr(
        writer_module.importlib,
        "import_module",
        lambda _name: pytest.fail("winreg must not be imported"),
    )
    writer = WindowsRegistryWriter()

    with pytest.raises(OSError):
        writer.set_value(
            RegistryValueTarget.from_dict(VALUE_TARGET),
            RegistryDataType.REG_DWORD,
            1,
        )
    with pytest.raises(OSError):
        writer.rename_branch(
            RegistryBranchTarget.from_dict(BRANCH_TARGET),
            RegistryBranchVisibility.HIDDEN,
        )


def test_web_apply_is_prg_one_time_server_resolved_and_log_is_redacted(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_SZ", "raw-before-secret")
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(
        plugin,
        desired_type="REG_SZ",
        desired_value="raw-after-secret",
        include_branch=True,
    )
    configurations(app).upsert(configuration)
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    manager._plugins[WindowsRegistry.identifier] = plugin  # noqa: SLF001
    client = app.test_client()

    preview = client.post(
        "/products/sample/plugins/windows-registry/presets/preview",
        data={"preset_id": "qa"},
    ).get_data(as_text=True)
    token_match = re.search(r'name="execution_token" value="([^"]+)"', preview)
    assert token_match is not None
    token = token_match.group(1)
    applied = client.post(
        "/products/sample/plugins/windows-registry/presets/qa/apply",
        data={
            "execution_token": token,
            "confirm": "yes",
            "hive": "HKLM",
            "key_path": "Software\\Attacker",
            "value_name": "Injected",
        },
    )

    assert applied.status_code == 302
    assert applied.location.endswith("#windows-registry")
    assert len(writer.calls) == 1
    assert writer.calls[0][0].key_path == VALUE_TARGET["key_path"]
    assert len(writer.branch_calls) == 1
    assert writer.branch_calls[0][0].key_path == BRANCH_TARGET["key_path"]
    result_page = client.get(applied.location)
    assert result_page.status_code == 200
    result_html = result_page.get_data(as_text=True)
    assert "Успішно" in result_html
    assert ">SUCCEEDED<" not in result_html
    assert preset_change_count(result_html, "QA mode") == 0
    assert "Поточний стан уже відповідає цьому preset." in result_html
    assert len(writer.calls) == 1
    assert len(writer.branch_calls) == 1
    repeated = client.post(
        "/products/sample/plugins/windows-registry/presets/qa/apply",
        data={"execution_token": token, "confirm": "yes"},
    )
    assert repeated.status_code == 302
    assert len(writer.calls) == 1
    assert len(writer.branch_calls) == 1
    log = operation_logs(app).list_for_product("sample")[0]
    assert log.action_identifier == "apply-registry-preset-values"
    assert "raw-before-secret" not in log.summary
    assert "raw-after-secret" not in log.summary


def test_product_get_automatically_inspects_and_compares_without_writing(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 7)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configurations(app).upsert(registry_configuration(plugin, include_branch=True))
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    manager._plugins[WindowsRegistry.identifier] = plugin  # noqa: SLF001

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "2 змін" in page
    assert "REG_DWORD" in page and "7" in page and "99" in page
    assert "Видима → Прихована" in page
    assert "Оновити поточний стан" in page
    assert writer.calls == [] and writer.branch_calls == []


def preset_change_count(page: str, preset_name: str) -> int:
    match = re.search(
        rf"<h5>{re.escape(preset_name)}</h5>"
        r'<span class="state-badge">(\d+) змін</span>',
        page,
    )
    assert match is not None
    return int(match.group(1))


def test_product_get_recomputes_all_presets_from_one_fresh_inspection(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    presets = [
        {
            "id": "value-one",
            "name": "Value one",
            "values": [
                {
                    "target_id": "mode",
                    "registry_type": "REG_DWORD",
                    "value": 1,
                }
            ],
        },
        {
            "id": "value-99",
            "name": "Value 99",
            "values": [
                {
                    "target_id": "mode",
                    "registry_type": "REG_DWORD",
                    "value": 99,
                }
            ],
        },
        {
            "id": "branch-visible",
            "name": "Branch visible",
            "branches": [{"target_id": "feature", "visibility": "visible"}],
        },
        {
            "id": "branch-hidden",
            "name": "Branch hidden",
            "branches": [{"target_id": "feature", "visibility": "hidden"}],
        },
    ]
    configuration = plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps([VALUE_TARGET]),
        branch_targets_json=json.dumps([BRANCH_TARGET]),
        presets_json=json.dumps(presets),
    )
    install_registry(app, plugin, configuration)
    client = app.test_client()

    before = client.get("/products/sample").get_data(as_text=True)
    reader.value = 99
    reader.original_exists = False
    reader.hidden_exists = True
    after = client.get("/products/sample").get_data(as_text=True)
    refreshed = client.get("/products/sample").get_data(as_text=True)

    assert preset_change_count(before, "Value one") == 0
    assert preset_change_count(before, "Value 99") == 1
    assert preset_change_count(before, "Branch visible") == 0
    assert preset_change_count(before, "Branch hidden") == 1
    assert preset_change_count(after, "Value one") == 1
    assert preset_change_count(after, "Value 99") == 0
    assert preset_change_count(after, "Branch visible") == 1
    assert preset_change_count(after, "Branch hidden") == 0
    assert preset_change_count(refreshed, "Value 99") == 0
    assert (reader.value_reads, reader.branch_reads) == (3, 3)
    assert writer.calls == [] and writer.branch_calls == []
    preset_names = [
        "Value one",
        "Value 99",
        "Branch visible",
        "Branch hidden",
    ]
    positions = [after.index(name) for name in preset_names]
    assert positions == sorted(positions)
    assert not any("active" in key for key in configuration.settings)


def test_saved_snapshot_to_current_comparison_rereads_registry_each_get(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, registry_configuration(plugin, desired_value=1))
    snapshot = capture_snapshot(app)
    client = app.test_client()
    reader.value = 99

    changed = client.get(
        f"/products/sample?snapshot={snapshot.id}"
    ).get_data(as_text=True)
    reader.value = 1
    unchanged = client.get(
        f"/products/sample?snapshot={snapshot.id}"
    ).get_data(as_text=True)

    assert "Змінено</span><strong>1</strong>" in changed
    assert "Змінено</span><strong>0</strong>" in unchanged
    assert reader.value_reads == 5
    assert writer.calls == [] and writer.branch_calls == []


def test_snapshot_restore_executes_value_and_hidden_branch_without_presets(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader(
        "REG_DWORD",
        1,
        original_exists=True,
        hidden_exists=False,
    )
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    configuration = registry_configuration(
        plugin,
        desired_value=255,
        include_branch=True,
    )
    install_registry(app, plugin, configuration)
    snapshot = capture_snapshot(app)
    settings_before = dict(configuration.settings)
    reader.value = 99
    reader.original_exists = False
    reader.hidden_exists = True
    client = app.test_client()

    token, plan_html = snapshot_restore_token(client, snapshot)
    response = execute_snapshot_restore(
        client,
        snapshot,
        token,
        hive="HKLM",
        key_path="Software\\Attacker",
        value="untrusted",
    )
    result_html = client.get(response.location).get_data(as_text=True)
    calls_after_result = (len(writer.calls), len(writer.branch_calls))
    refreshed_html = client.get(response.location).get_data(as_text=True)
    duplicate = execute_snapshot_restore(client, snapshot, token)

    assert "REG_DWORD 99 -&gt; REG_DWORD 1" in plan_html
    assert "Прихована -&gt; Видима" in plan_html
    assert response.status_code == 302
    assert response.location.endswith("#snapshot-restore-result")
    assert (reader.registry_type, reader.value) == ("REG_DWORD", 1)
    assert (reader.original_exists, reader.hidden_exists) == (True, False)
    assert writer.calls[0][0].key_path == VALUE_TARGET["key_path"]
    assert writer.branch_calls[0][0].key_path == BRANCH_TARGET["key_path"]
    assert dict(configurations(app).get("sample", plugin.identifier).settings) == (
        settings_before
    )
    assert "РЕЗУЛЬТАТ ВІДНОВЛЕННЯ" in result_html
    assert "Успішно" in result_html and "Прихована -&gt; Видима" in result_html
    assert "SUCCESS" not in result_html
    assert refreshed_html == result_html
    assert (len(writer.calls), len(writer.branch_calls)) == calls_after_result
    assert duplicate.status_code == 302
    assert (len(writer.calls), len(writer.branch_calls)) == calls_after_result
    logs = operation_logs(app).list_for_product("sample")
    registry_logs = [
        item for item in logs if item.action_identifier == "restore-snapshot-resource"
    ]
    assert len(registry_logs) == 2
    summaries = " ".join(item.summary for item in registry_logs)
    assert "99" not in summaries and "untrusted" not in summaries


def test_snapshot_restore_executes_visible_to_hidden_branch(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    reader = MutableReader(
        "REG_DWORD",
        1,
        original_exists=False,
        hidden_exists=True,
    )
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(
        app,
        plugin,
        registry_configuration(
            plugin,
            desired_value=1,
            include_branch=True,
            branch_visibility="visible",
        ),
    )
    snapshot = capture_snapshot(app)
    reader.original_exists = True
    reader.hidden_exists = False
    client = app.test_client()

    token, plan_html = snapshot_restore_token(client, snapshot)
    response = execute_snapshot_restore(client, snapshot, token)

    assert "Видима -&gt; Прихована" in plan_html
    assert response.status_code == 302
    assert (reader.original_exists, reader.hidden_exists) == (False, True)
    assert writer.branch_calls[-1][1] is RegistryBranchVisibility.HIDDEN


def test_snapshot_restore_preserves_exact_snapshot_value_type(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_SZ", "1")
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, registry_configuration(plugin))
    snapshot = capture_snapshot(app)
    reader.registry_type = "REG_DWORD"
    reader.value = 1
    client = app.test_client()

    token, _ = snapshot_restore_token(client, snapshot)
    response = execute_snapshot_restore(client, snapshot, token)

    assert response.status_code == 302
    assert writer.calls[-1][1:] == (RegistryDataType.REG_SZ, "1")
    assert (reader.registry_type, reader.value) == ("REG_SZ", "1")


def test_snapshot_restore_stale_value_and_branch_do_not_mutate(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(
        app,
        plugin,
        registry_configuration(plugin, desired_value=1, include_branch=True),
    )
    snapshot = capture_snapshot(app)
    reader.value = 99
    reader.original_exists = False
    reader.hidden_exists = True
    client = app.test_client()
    token, _ = snapshot_restore_token(client, snapshot)
    reader.value = 123
    reader.original_exists = True
    reader.hidden_exists = True

    response = execute_snapshot_restore(client, snapshot, token)
    result_html = client.get(response.location).get_data(as_text=True)

    assert writer.calls == [] and writer.branch_calls == []
    assert "Стан змінився" in result_html


def test_snapshot_restore_reuses_value_and_branch_rollback(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(
        reader,
        corrupt_first_write=True,
        corrupt_first_branch_read=True,
    )
    plugin = WindowsRegistry(reader, writer)
    install_registry(
        app,
        plugin,
        registry_configuration(plugin, desired_value=1, include_branch=True),
    )
    snapshot = capture_snapshot(app)
    reader.value = 99
    reader.original_exists = False
    reader.hidden_exists = True
    client = app.test_client()

    token, _ = snapshot_restore_token(client, snapshot)
    response = execute_snapshot_restore(client, snapshot, token)
    result_html = client.get(response.location).get_data(as_text=True)

    assert (reader.registry_type, reader.value) == ("REG_DWORD", 99)
    assert (reader.original_exists, reader.hidden_exists) == (False, True)
    assert len(writer.calls) == 2 and len(writer.branch_calls) == 2
    assert result_html.count("Rollback: complete") == 2


def test_snapshot_restore_configuration_drift_never_uses_snapshot_path(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, registry_configuration(plugin, desired_value=1))
    snapshot = capture_snapshot(app)
    reader.value = 99
    client = app.test_client()
    token, _ = snapshot_restore_token(client, snapshot)
    configurations(app).upsert(
        registry_configuration(
            plugin,
            desired_value=1,
            value_target={**VALUE_TARGET, "key_path": "Software\\Changed"},
        )
    )

    response = execute_snapshot_restore(client, snapshot, token)
    result_html = client.get(response.location).get_data(as_text=True)

    assert writer.calls == []
    assert reader.value == 99
    assert "Стан змінився" in result_html


def test_mixed_license_and_registry_snapshot_restore_execute_independently(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    license_directory = tmp_path / "licenses"
    license_directory.mkdir()
    active_license = license_directory / "license.dat"
    hidden_license = license_directory / "license.dat.hidden"
    active_license.write_text("license", encoding="utf-8")
    configurations(app).upsert(
        license_configuration(license_directory, ["license.dat"])
    )
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, registry_configuration(plugin, desired_value=1))
    snapshot = capture_snapshot(app, "Mixed baseline")
    active_license.rename(hidden_license)
    reader.value = 99
    client = app.test_client()

    token, plan_html = snapshot_restore_token(client, snapshot)
    response = execute_snapshot_restore(client, snapshot, token)
    result_html = client.get(response.location).get_data(as_text=True)

    assert "License Manager" in plan_html and "Windows Registry" in plan_html
    assert response.status_code == 302
    assert active_license.exists() and not hidden_license.exists()
    assert reader.value == 1
    assert result_html.count("Успішно") >= 2
