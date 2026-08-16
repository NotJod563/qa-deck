"""Shared read-only Registry planning for presets and Snapshot Restore."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from qa_deck.domain import Product
from qa_deck.domain.snapshot import SnapshotResource
from qa_deck.plugins.builtin.windows_registry import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryPlanOperation,
    RegistryPlanStatus,
    RegistryPresetBranch,
    RegistryPresetValue,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
    WindowsRegistry,
)

VALUE_TARGET = {
    "id": "debug-mode",
    "hive": "HKCU",
    "key_path": "Software\\Example",
    "value_name": "DebugMode",
    "display_name": "Debug mode",
    "enabled": True,
}
BRANCH_TARGET = {
    "id": "feature-branch",
    "hive": "HKCU",
    "key_path": "Software\\Example\\FeatureBranch",
    "display_name": "Feature branch",
    "enabled": True,
}


class FakeReader:
    def __init__(
        self,
        *,
        value_type: str = "REG_DWORD",
        value: object = 255,
        value_status: RegistryValueStatus = RegistryValueStatus.AVAILABLE,
        branch_status: RegistryBranchStatus = RegistryBranchStatus.VISIBLE,
        failing_ids: set[str] | None = None,
    ) -> None:
        self.value_type = value_type
        self.value = value
        self.value_status = value_status
        self.branch_status = branch_status
        self.failing_ids = failing_ids or set()

    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        if target.id in self.failing_ids:
            raise OSError("isolated failure")
        return RegistryValueInspection(
            target,
            self.value_status is RegistryValueStatus.AVAILABLE,
            self.value_type,
            self.value,
            self.value_status,
            "read-only",
        )

    def inspect_branch(
        self, target: RegistryBranchTarget
    ) -> RegistryBranchInspection:
        if target.id in self.failing_ids:
            raise OSError("isolated failure")
        return RegistryBranchInspection(
            target,
            self.branch_status is RegistryBranchStatus.VISIBLE,
            self.branch_status is RegistryBranchStatus.HIDDEN,
            self.branch_status,
            "read-only",
        )


def configuration(
    plugin: WindowsRegistry,
    *,
    values: list[object] | None = None,
    branches: list[object] | None = None,
    presets: list[object] | None = None,
):
    return plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps(values or [VALUE_TARGET]),
        branch_targets_json=json.dumps(branches or [BRANCH_TARGET]),
        presets_json=json.dumps(presets or []),
    )


def value_desired(type_name: str, value: object) -> RegistryPresetValue:
    return RegistryPresetValue.from_dict(
        {
            "target_id": "debug-mode",
            "registry_type": type_name,
            "value": value,
        }
    )


def branch_desired(visibility: str) -> RegistryPresetBranch:
    return RegistryPresetBranch.from_dict(
        {"target_id": "feature-branch", "visibility": visibility}
    )


def test_dword_preset_previews_255_to_2_with_fingerprint() -> None:
    plugin = WindowsRegistry(FakeReader(value=255))
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_value(  # noqa: SLF001
        typed.value_targets[0], value_desired("REG_DWORD", 2)
    )

    assert entry.status is RegistryPlanStatus.READY
    assert entry.current_state.value == 255
    assert entry.desired_state.value == 2
    assert entry.operation is RegistryPlanOperation.SET_VALUE
    assert len(entry.expected_fingerprint) == 64


def test_existing_dword_7_to_10_produces_executable_change() -> None:
    plugin = WindowsRegistry(FakeReader(value_type="REG_DWORD", value=7))
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_value(  # noqa: SLF001
        typed.value_targets[0], value_desired("REG_DWORD", 10)
    )

    assert entry.status is RegistryPlanStatus.READY
    assert entry.operation is RegistryPlanOperation.SET_VALUE
    assert entry.current_state.value == 7
    assert entry.desired_state.value == 10


@pytest.mark.parametrize(
    ("current_type", "current_value", "desired_type", "desired_value"),
    [
        ("REG_SZ", "QA", "REG_SZ", "TEST"),
        ("REG_DWORD", 1, "REG_SZ", "1"),
    ],
)
def test_value_and_type_changes_are_detected(
    current_type: str,
    current_value: object,
    desired_type: str,
    desired_value: object,
) -> None:
    plugin = WindowsRegistry(
        FakeReader(value_type=current_type, value=current_value)
    )
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_value(  # noqa: SLF001
        typed.value_targets[0], value_desired(desired_type, desired_value)
    )

    assert entry.status is RegistryPlanStatus.READY


def test_value_with_matching_explicit_type_and_value_is_no_change() -> None:
    plugin = WindowsRegistry(FakeReader(value_type="REG_SZ", value="QA"))
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_value(  # noqa: SLF001
        typed.value_targets[0], value_desired("REG_SZ", "QA")
    )

    assert entry.status is RegistryPlanStatus.NO_CHANGE
    assert entry.operation is RegistryPlanOperation.NONE


@pytest.mark.parametrize(
    ("current", "desired", "operation"),
    [
        (RegistryBranchStatus.VISIBLE, "hidden", RegistryPlanOperation.HIDE_BRANCH),
        (
            RegistryBranchStatus.HIDDEN,
            "visible",
            RegistryPlanOperation.RESTORE_BRANCH,
        ),
    ],
)
def test_branch_preview_uses_reversible_rename_semantics(
    current: RegistryBranchStatus,
    desired: str,
    operation: RegistryPlanOperation,
) -> None:
    plugin = WindowsRegistry(FakeReader(branch_status=current))
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_branch(  # noqa: SLF001
        typed.branch_targets[0], branch_desired(desired)
    )

    assert entry.status is RegistryPlanStatus.READY
    assert entry.operation is operation
    assert "rename" in entry.message


def test_branch_conflict_is_blocked() -> None:
    plugin = WindowsRegistry(
        FakeReader(branch_status=RegistryBranchStatus.CONFLICT)
    )
    typed = plugin.typed_configuration(configuration(plugin))
    assert typed is not None

    entry = plugin._planner.plan_branch(  # noqa: SLF001
        typed.branch_targets[0], branch_desired("hidden")
    )

    assert entry.status is RegistryPlanStatus.BLOCKED
    assert entry.operation is RegistryPlanOperation.NONE


def test_one_target_failure_is_isolated_from_other_preset_entries() -> None:
    second_target = {**VALUE_TARGET, "id": "region", "value_name": "Region"}
    preset = {
        "id": "qa",
        "name": "QA",
        "values": [
            {
                "target_id": "debug-mode",
                "registry_type": "REG_DWORD",
                "value": 2,
            },
            {"target_id": "region", "registry_type": "REG_DWORD", "value": 2},
        ],
    }
    plugin = WindowsRegistry(FakeReader(failing_ids={"debug-mode"}))
    plan = plugin.preview_preset(
        configuration(plugin, values=[VALUE_TARGET, second_target], presets=[preset]),
        "qa",
    )

    assert [entry.status for entry in plan.entries] == [
        RegistryPlanStatus.ERROR,
        RegistryPlanStatus.READY,
    ]


def test_snapshot_restore_direction_is_current_to_snapshot_value() -> None:
    plugin = WindowsRegistry(FakeReader(value=255))
    config = configuration(plugin)
    desired = SnapshotResource(
        "windows-registry",
        "registry-value",
        "debug-mode",
        state={
            "exists": True,
            "registry_type": "REG_DWORD",
            "value": 1,
            "status": "available",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "DebugMode",
        },
    )
    current = SnapshotResource(
        "windows-registry",
        "registry-value",
        "debug-mode",
        state={
            "exists": True,
            "registry_type": "REG_DWORD",
            "value": 255,
            "status": "available",
        },
    )

    preparation = plugin.prepare_restore(
        Product("sample", "Sample"), desired, current, config
    )

    assert "255" in preparation.action_description
    assert "-> REG_DWORD 1" in preparation.action_description
    assert preparation.blocking_error is None


def test_snapshot_restore_direction_is_current_hidden_to_snapshot_visible() -> None:
    plugin = WindowsRegistry(
        FakeReader(branch_status=RegistryBranchStatus.HIDDEN)
    )
    config = configuration(plugin)
    desired = SnapshotResource(
        "windows-registry",
        "registry-branch",
        "feature-branch",
        state={
            "visibility": "visible",
            "status": "visible",
            "hive": "HKCU",
            "key_path": "Software\\Example\\FeatureBranch",
            "hidden_name": "FeatureBranch.__qa_deck_hidden__",
        },
    )
    current = SnapshotResource(
        "windows-registry",
        "registry-branch",
        "feature-branch",
        state={"visibility": "hidden", "status": "hidden"},
    )

    preparation = plugin.prepare_restore(
        Product("sample", "Sample"), desired, current, config
    )

    assert "Прихована -> Видима" in preparation.action_description
    assert preparation.blocking_error is None


def test_preset_and_restore_share_planner_and_preserve_preset_configuration() -> None:
    plugin = WindowsRegistry(FakeReader())
    preset = {
        "id": "qa",
        "name": "QA",
        "values": [
            {
                "target_id": "debug-mode",
                "registry_type": "REG_DWORD",
                "value": 2,
            }
        ],
    }
    config = configuration(plugin, presets=[preset])
    settings_before = dict(config.settings)
    desired = SnapshotResource(
        "windows-registry",
        "registry-value",
        "debug-mode",
        state={
            "exists": True,
            "registry_type": "REG_DWORD",
            "value": 1,
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "DebugMode",
        },
    )
    current = SnapshotResource(
        "windows-registry",
        "registry-value",
        "debug-mode",
        state={"exists": True, "registry_type": "REG_DWORD", "value": 255},
    )

    with (
        patch.object(
            plugin._planner,  # noqa: SLF001
            "plan_preset",
            wraps=plugin._planner.plan_preset,  # noqa: SLF001
        ) as preset_call,
        patch.object(
            plugin._planner,  # noqa: SLF001
            "plan_value",
            wraps=plugin._planner.plan_value,  # noqa: SLF001
        ) as value_call,
        patch.object(
            plugin._planner,  # noqa: SLF001
            "plan_inspected_value",
            wraps=plugin._planner.plan_inspected_value,  # noqa: SLF001
        ) as inspected_value_call,
    ):
        plugin.preview_preset(config, "qa")
        plugin.prepare_restore(Product("sample", "Sample"), desired, current, config)

    assert preset_call.call_count == 1
    assert value_call.call_count == 1
    assert inspected_value_call.call_count == 1
    assert dict(config.settings) == settings_before
    assert plugin.typed_configuration(config).presets[0].id == "qa"
    assert callable(plugin.execute_restore)
    restore_source = Path("src/qa_deck/snapshot/restore.py").read_text(
        encoding="utf-8"
    )
    assert "windows-registry" not in restore_source
    plugin_source = Path(
        "src/qa_deck/plugins/builtin/windows_registry"
    ).joinpath("plugin.py").read_text(encoding="utf-8")
    assert "PluginConfigurationRepository" not in plugin_source
    assert "self._executor.execute_entry" in plugin_source
    assert all(
        forbidden not in plugin_source
        for forbidden in ("SetValueEx", "RegRenameKey", "DeleteKey", "DeleteValue")
    )


def test_snapshot_restore_blocks_target_missing_from_current_configuration() -> None:
    plugin = WindowsRegistry(FakeReader())
    resource = SnapshotResource(
        "windows-registry",
        "registry-value",
        "removed-target",
        state={
            "exists": True,
            "registry_type": "REG_DWORD",
            "value": 1,
            "hive": "HKCU",
            "key_path": "Software\\Old",
            "value_name": "DebugMode",
        },
    )

    preparation = plugin.prepare_restore(
        Product("sample", "Sample"), resource, None, configuration(plugin)
    )

    assert (
        preparation.blocking_error
        == "Ресурс зі snapshot не вдалося безпечно зіставити з поточною "
        "конфігурацією Windows Registry."
    )


def test_snapshot_restore_blocks_value_location_drift_before_planning() -> None:
    plugin = WindowsRegistry(FakeReader())
    resource = SnapshotResource(
        "windows-registry",
        "registry-value",
        "debug-mode",
        state={
            "exists": True,
            "registry_type": "REG_DWORD",
            "value": 1,
            "hive": "HKCU",
            "key_path": "Software\\OldLocation",
            "value_name": "DebugMode",
        },
    )

    with patch.object(plugin._planner, "plan_value") as planner:  # noqa: SLF001
        preparation = plugin.prepare_restore(
            Product("sample", "Sample"), resource, resource, configuration(plugin)
        )

    assert preparation.blocking_error == (
        "Налаштування ресурсу реєстру змінилися після створення Snapshot."
    )
    planner.assert_not_called()


def test_snapshot_restore_blocks_branch_identity_drift() -> None:
    plugin = WindowsRegistry(FakeReader())
    resource = SnapshotResource(
        "windows-registry",
        "registry-branch",
        "feature-branch",
        state={
            "visibility": "visible",
            "status": "visible",
            "hive": "HKCU",
            "key_path": "Software\\Example\\FeatureBranch",
            "hidden_name": "unexpected-hidden-name",
        },
    )

    preparation = plugin.prepare_restore(
        Product("sample", "Sample"), resource, resource, configuration(plugin)
    )

    assert preparation.blocking_error == (
        "Налаштування ресурсу реєстру змінилися після створення Snapshot."
    )
