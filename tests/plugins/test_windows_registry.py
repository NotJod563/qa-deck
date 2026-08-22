"""Read-only Windows Registry plugin behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from flask import Flask

from qa_deck.domain import Product, Snapshot
from qa_deck.domain.snapshot import SnapshotResource
from qa_deck.plugins import PluginManager
from qa_deck.plugins.builtin.windows_registry import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryHive,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
    WindowsRegistry,
    WindowsRegistryReader,
)
from qa_deck.snapshot import SnapshotDiffer, SnapshotDiffStatus
from tests.helpers import configurations, make_app

VALUE_TARGET = {
    "id": "theme",
    "hive": "HKCU",
    "key_path": "Software\\Example\\Client",
    "value_name": "Theme",
    "display_name": "Theme",
    "enabled": True,
}
BRANCH_TARGET = {
    "id": "feature-branch",
    "hive": "HKCU",
    "key_path": "Software\\Example\\Feature",
    "enabled": True,
}


class FakeRegistryReader:
    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        return RegistryValueInspection(
            target,
            True,
            "REG_BINARY",
            "00A1FF",
            RegistryValueStatus.AVAILABLE,
            "available",
        )

    def inspect_branch(
        self, target: RegistryBranchTarget
    ) -> RegistryBranchInspection:
        return RegistryBranchInspection(
            target,
            True,
            False,
            RegistryBranchStatus.VISIBLE,
            "visible",
        )


class DwordRegistryReader(FakeRegistryReader):
    def __init__(
        self,
        value: int | None,
        status: RegistryValueStatus = RegistryValueStatus.AVAILABLE,
    ) -> None:
        self.value = value
        self.status = status

    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        return RegistryValueInspection(
            target,
            self.status is RegistryValueStatus.AVAILABLE,
            "REG_DWORD" if self.status is RegistryValueStatus.AVAILABLE else None,
            self.value,
            self.status,
            self.status.value,
        )

def configuration(plugin: WindowsRegistry, *, presets: list[object] | None = None):
    return plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps([VALUE_TARGET]),
        branch_targets_json=json.dumps([BRANCH_TARGET]),
        presets_json=json.dumps(presets or []),
    )


def install_registry_plugin(
    app: Flask,
    plugin: WindowsRegistry,
    presets: list[object],
) -> None:
    configurations(app).upsert(configuration(plugin, presets=presets))
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    manager._plugins[WindowsRegistry.identifier] = plugin  # noqa: SLF001


def test_configuration_has_typed_targets_and_deterministic_hidden_sibling() -> None:
    typed = WindowsRegistry().typed_configuration(configuration(WindowsRegistry()))

    assert typed is not None
    assert typed.value_targets[0].hive is RegistryHive.HKCU
    assert typed.branch_targets[0].hidden_key_path == (
        "Software\\Example\\Feature.__qa_deck_hidden__"
    )


@pytest.mark.parametrize(
    ("values", "branches", "message"),
    [
        ([VALUE_TARGET, {**VALUE_TARGET, "id": "other"}], [], "physical"),
        ([], [BRANCH_TARGET, {**BRANCH_TARGET, "id": "other"}], "collision"),
        ([VALUE_TARGET], [{**BRANCH_TARGET, "id": "THEME"}], "IDs"),
    ],
)
def test_duplicate_logical_and_physical_targets_are_rejected(
    values: list[object], branches: list[object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        WindowsRegistry().create_configuration(
            product_id="sample",
            enabled=True,
            value_targets_json=json.dumps(values),
            branch_targets_json=json.dumps(branches),
            presets_json="[]",
        )


def test_invalid_hive_path_and_wildcard_are_rejected() -> None:
    for target in (
        {**VALUE_TARGET, "hive": "HKCR"},
        {**VALUE_TARGET, "key_path": ""},
        {**VALUE_TARGET, "key_path": "Software\\Example\\*"},
    ):
        with pytest.raises(ValueError):
            WindowsRegistry().create_configuration(
                product_id="sample",
                enabled=True,
                value_targets_json=json.dumps([target]),
                branch_targets_json="[]",
                presets_json="[]",
            )


def test_presets_require_known_targets_and_explicit_supported_types() -> None:
    plugin = WindowsRegistry()
    preset = {
        "id": "dark",
        "name": "Dark mode",
        "values": [
            {"target_id": "theme", "registry_type": "REG_BINARY", "value": "00 a1"}
        ],
        "branches": [
            {"target_id": "feature-branch", "visibility": "hidden"}
        ],
    }
    preview = plugin.preview_preset(configuration(plugin, presets=[preset]), "dark")

    assert [
        (
            entry.desired_state.registry_type.value
            if hasattr(entry.desired_state, "registry_type")
            else "visibility",
            entry.desired_state.value
            if hasattr(entry.desired_state, "value")
            else entry.desired_state.visibility.value,
        )
        for entry in preview.entries
    ] == [
        ("REG_BINARY", "00A1"),
        ("visibility", "hidden"),
    ]
    preset["values"][0]["target_id"] = "unknown"
    with pytest.raises(ValueError, match="unknown value target"):
        configuration(plugin, presets=[preset])


def test_fake_reader_inspects_only_configured_enabled_targets() -> None:
    plugin = WindowsRegistry(FakeRegistryReader())
    result = plugin.inspect(configuration(plugin))

    assert [item.target.id for item in result.values] == ["theme"]
    assert result.values[0].status is RegistryValueStatus.AVAILABLE
    assert result.branches[0].status is RegistryBranchStatus.VISIBLE


def test_non_windows_reader_is_safe_and_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import qa_deck.plugins.builtin.windows_registry.reader as reader_module

    monkeypatch.setattr(reader_module.sys, "platform", "linux")
    reader = WindowsRegistryReader()
    target = RegistryValueTarget.from_dict(VALUE_TARGET)

    assert reader.inspect_value(target).status is RegistryValueStatus.UNAVAILABLE


def test_registry_snapshot_resources_are_stable_and_generic_diff_detects_change(
) -> None:
    plugin = WindowsRegistry(FakeRegistryReader())
    capture = plugin.capture_snapshot(
        Product("sample", "Sample"), configuration(plugin)
    )

    assert [(item.resource_type, item.identifier) for item in capture.resources] == [
        ("registry-value", "theme"),
        ("registry-branch", "feature-branch"),
    ]
    product = Product("sample", "Sample")
    first = plugin.capture_snapshot(product, configuration(plugin))
    assert first.resources[0].state["value"] == "00A1FF"
    changed_resource = SnapshotResource(
        source="windows-registry",
        resource_type="registry-value",
        identifier="theme",
        state={**dict(first.resources[0].state), "value": "FF"},
    )
    base_snapshot = Snapshot(
        "base", "sample", datetime.now(UTC), None, (first.resources[0],)
    )
    target_snapshot = Snapshot(
        "target", "sample", datetime.now(UTC), None, (changed_resource,)
    )
    diff = SnapshotDiffer().diff(base_snapshot, target_snapshot)

    assert diff.entries[0].status is SnapshotDiffStatus.CHANGED
    # Generic coupling guard: Registry is a provider, not a Snapshot core special case.
    assert "windows-registry" not in Path(
        "src/qa_deck/snapshot/diff.py"
    ).read_text(encoding="utf-8")


def test_registry_snapshot_captures_runtime_only_and_keeps_presets_unchanged() -> None:
    plugin = WindowsRegistry(FakeRegistryReader())
    preset = {
        "id": "dark",
        "name": "Dark mode",
        "values": [
            {"target_id": "theme", "registry_type": "REG_SZ", "value": "dark"}
        ],
    }
    configured = configuration(plugin, presets=[preset])
    settings_before = dict(configured.settings)

    capture = plugin.capture_snapshot(Product("sample", "Sample"), configured)

    forbidden = {"presets", "preset", "enabled", "value_targets", "branch_targets"}
    assert all(forbidden.isdisjoint(resource.state) for resource in capture.resources)
    assert dict(configured.settings) == settings_before
    assert plugin.typed_configuration(configured).presets[0].id == "dark"


def test_registry_routes_save_inspect_and_preview_without_ready_apply(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    preset = {
        "id": "dark",
        "name": "Dark mode",
        "values": [
            {"target_id": "theme", "registry_type": "REG_SZ", "value": "dark"}
        ],
    }
    saved = client.post(
        "/products/sample/plugins/windows-registry/configuration",
        data={
            "enabled": "on",
            "value_targets": json.dumps([VALUE_TARGET]),
            "branch_targets": "[]",
            "presets": json.dumps([preset]),
        },
    )
    inspected = client.post("/products/sample/plugins/windows-registry/inspect")
    previewed = client.post(
        "/products/sample/plugins/windows-registry/presets/preview",
        data={"preset_id": "dark"},
    )

    assert saved.location.endswith("#windows-registry")
    assert inspected.status_code == 200
    preview_html = previewed.get_data(as_text=True)
    assert "Dark mode" in preview_html
    assert (
        "Перевірте заплановані зміни. Збережений preset не змінюється."
        in preview_html
    )
    assert 'class="plugin-result-slot registry-plan registry-full-width"' in (
        preview_html
    )
    assert "ПОПЕРЕДНІЙ ПЕРЕГЛЯД ЗМІН" in preview_html
    assert "Застосувати" not in preview_html
    assert configurations(app).get("sample", "windows-registry") is not None
    assert any(
        rule.rule.endswith("/presets/<preset_id>/apply")
        for rule in app.url_map.iter_rules()
    )


def test_ready_dword_preset_exposes_preview_and_confirmation_path(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    plugin = WindowsRegistry(DwordRegistryReader(7))
    preset = {
        "id": "debug",
        "name": "Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_DWORD", "value": 10}
        ],
    }
    install_registry_plugin(app, plugin, [preset])
    client = app.test_client()

    page = client.get("/products/sample").get_data(as_text=True)
    preview = client.post(
        "/products/sample/plugins/windows-registry/presets/preview",
        data={"preset_id": "debug"},
    ).get_data(as_text=True)

    assert "1 змін" in page
    assert "Застосувати preset" in page
    assert "REG_DWORD</code>: <code>7" in page
    assert "REG_DWORD</code>: <code>10" in page
    assert 'name="execution_token"' in preview
    assert "Підтвердити застосування" in preview


def test_all_no_change_preset_has_no_executable_action(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    plugin = WindowsRegistry(DwordRegistryReader(10))
    preset = {
        "id": "debug",
        "name": "Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_DWORD", "value": 10}
        ],
    }
    install_registry_plugin(app, plugin, [preset])

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "Поточний стан уже відповідає цьому preset." in page
    assert "Застосувати preset" not in page


def test_missing_value_preset_explains_blocked_execution(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    plugin = WindowsRegistry(
        DwordRegistryReader(None, RegistryValueStatus.MISSING_VALUE)
    )
    preset = {
        "id": "debug",
        "name": "Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_DWORD", "value": 10}
        ],
    }
    install_registry_plugin(app, plugin, [preset])

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "Preset не має змін, які можна виконати." in page
    assert "Налаштований Registry value не існує." in page
    assert "Створення нового value не підтримується." in page
    assert "Застосувати preset" not in page


def test_registry_target_forms_explain_configuration_only_boundaries(
    tmp_path: Path,
) -> None:
    page = make_app(tmp_path).test_client().get("/products/sample").get_data(
        as_text=True
    )

    assert "+ Додати ресурс Registry" in page
    assert "Збереження цієї форми не змінює Windows Registry." in page
    assert 'placeholder="Software\\QADeckManualTest"' in page
    assert 'placeholder="TempValue"' in page
    assert "Це не значення параметра." in page
    assert "Бажані тип і значення задаються окремо в preset." in page
    assert "+ Додати ресурс Registry branch" in page
    assert "Бажана видимість гілки задається окремо в preset." in page


def test_value_target_form_creates_edits_and_removes_configuration(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    route = "/products/sample/plugins/windows-registry/value-targets"
    created = client.post(
        route,
        data={
            "id": "theme",
            "display_name": "Theme",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "Theme",
            "enabled": "on",
        },
    )
    edited = client.post(
        route,
        data={
            "original_id": "theme",
            "id": "theme",
            "display_name": "UI theme",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "Theme",
            "enabled": "on",
        },
    )
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )

    assert created.location.endswith("#windows-registry")
    assert edited.location.endswith("#windows-registry")
    assert typed is not None and typed.value_targets[0].display_name == "UI theme"
    removed = client.post(
        route,
        data={"original_id": "theme", "action": "delete"},
    )
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )
    assert removed.location.endswith("#windows-registry")
    assert typed is not None and typed.value_targets == ()


def test_branch_target_form_creates_edits_and_removes_configuration(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    route = "/products/sample/plugins/windows-registry/branch-targets"
    data = {
        "id": "feature",
        "display_name": "Feature",
        "hive": "HKCU",
        "key_path": "Software\\Example\\Feature",
        "enabled": "on",
    }

    assert client.post(route, data=data).status_code == 302
    data.update({"original_id": "feature", "display_name": "Feature branch"})
    assert client.post(route, data=data).status_code == 302
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )
    assert typed is not None
    assert typed.branch_targets[0].display_name == "Feature branch"
    assert client.post(
        route, data={"original_id": "feature", "action": "delete"}
    ).status_code == 302


def test_invalid_registry_form_preserves_persisted_configuration(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    route = "/products/sample/plugins/windows-registry/value-targets"
    valid = {
        "id": "theme",
        "hive": "HKCU",
        "key_path": "Software\\Example",
        "value_name": "Theme",
        "enabled": "on",
    }
    client.post(route, data=valid)
    before = configurations(app).get("sample", "windows-registry")

    response = client.post(
        route,
        data={**valid, "original_id": "theme", "key_path": "Software\\*"},
    )

    assert response.status_code == 400
    assert "Registry key path is invalid" in response.get_data(as_text=True)
    assert configurations(app).get("sample", "windows-registry") == before


def test_preset_form_uses_only_configured_target_ids_and_no_raw_json_ui(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/products/sample/plugins/windows-registry/value-targets",
        data={
            "id": "theme",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "Theme",
            "enabled": "on",
        },
    )
    client.post(
        "/products/sample/plugins/windows-registry/branch-targets",
        data={
            "id": "feature",
            "hive": "HKCU",
            "key_path": "Software\\Example\\Feature",
            "enabled": "on",
        },
    )
    response = client.post(
        "/products/sample/plugins/windows-registry/presets",
        data={
            "id": "dark",
            "name": "Dark mode",
            "include_value__theme": "on",
            "value_type__theme": "REG_SZ",
            "value_data__theme": "dark",
        },
    )
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )
    page = client.get("/products/sample").get_data(as_text=True)

    assert response.status_code == 302
    assert typed is not None and typed.presets[0].values[0].target_id == "theme"
    assert typed.presets[0].branches == ()
    assert 'name="value_targets"' not in page
    assert 'name="branch_targets"' not in page
    assert 'name="presets"' not in page
    assert 'name="include_value__theme"' in page
    assert 'placeholder="debug-mode"' in page
    script = client.get("/static/registry_preset_editor.js").get_data(as_text=True)
    assert "Шістнадцяткові байти, наприклад 01 FF A0 2C" in script


def test_preset_editor_shows_value_and_branch_locations_with_inclusion_copy(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    plugin = WindowsRegistry()
    configurations(app).upsert(configuration(plugin))

    page = client.get("/products/sample").get_data(as_text=True)
    css = client.get("/static/styles.css").get_data(as_text=True)

    assert "HKCU\\Software\\Example\\Client" in page
    assert "Назва значення: <code>Theme</code>" in page
    assert "HKCU\\Software\\Example\\Feature" in page
    assert "Додати до режиму" not in page
    assert page.count("Включити в preset") >= 2
    assert page.count('class="preset-include-control"') >= 2
    assert "<span>Додати target</span>" not in page
    assert 'name="value_type__theme"' in page and "disabled" in page
    assert 'class="registry-editor-actions"' in page
    assert page.index('class="preset-target-list"') < page.index(
        'class="registry-editor-actions"'
    )
    assert '.preset-include-control input[type="checkbox"]' in css
    assert (
        "label:not(.checkbox-field):not(.preset-include-control)" in css
    )
    assert "accent-color: var(--accent)" in css


def test_preset_editor_shows_current_inspection_state_when_available(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    plugin = app.extensions["plugin_manager"].get("windows-registry")
    assert isinstance(plugin, WindowsRegistry)
    plugin._reader = FakeRegistryReader()  # noqa: SLF001
    configurations(app).upsert(configuration(plugin))

    page = app.test_client().post(
        "/products/sample/plugins/windows-registry/inspect"
    ).get_data(as_text=True)

    assert "Поточний стан" in page
    assert "REG_BINARY" in page
    assert "00A1FF" in page
    assert "<code>Видима</code>" in page
    assert "<code>visible</code>" not in page


def test_new_preset_id_is_generated_and_existing_id_survives_rename(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/products/sample/plugins/windows-registry/value-targets",
        data={
            "id": "theme",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "Theme",
            "enabled": "on",
        },
    )
    route = "/products/sample/plugins/windows-registry/presets"
    desired = {
        "name": "QA Debug",
        "include_value__theme": "on",
        "value_type__theme": "REG_SZ",
        "value_data__theme": "QA",
    }
    client.post(route, data=desired)
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )
    assert typed is not None and typed.presets[0].id == "qa-debug"
    assert typed.presets[0].branches == ()

    client.post(
        route,
        data={**desired, "original_id": "qa-debug", "name": "Renamed mode"},
    )
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )
    assert typed is not None and typed.presets[0].id == "qa-debug"
    assert typed.presets[0].name == "Renamed mode"


def test_empty_or_unknown_only_preset_is_rejected_without_persistence(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/products/sample/plugins/windows-registry/value-targets",
        data={
            "id": "theme",
            "hive": "HKCU",
            "key_path": "Software\\Example",
            "value_name": "Theme",
            "enabled": "on",
        },
    )

    response = client.post(
        "/products/sample/plugins/windows-registry/presets",
        data={"name": "Unsafe", "include_value__unknown": "on"},
    )
    typed = WindowsRegistry().typed_configuration(
        configurations(app).get("sample", "windows-registry")
    )

    assert response.status_code == 400
    assert "хоча б один Registry target" in response.get_data(as_text=True)
    assert typed is not None and typed.presets == ()


def test_existing_preset_card_uses_names_desired_states_and_location(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    preset = {
        "id": "dark",
        "name": "QA Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_SZ", "value": "QA"}
        ],
        "branches": [
            {"target_id": "feature-branch", "visibility": "hidden"}
        ],
    }
    configurations(app).upsert(configuration(WindowsRegistry(), presets=[preset]))

    client = app.test_client()
    page = client.get("/products/sample").get_data(as_text=True)

    assert "QA Debug" in page
    assert "Theme" in page and "REG_SZ" in page and "QA" in page
    assert "feature-branch" in page and "Прихована" in page
    assert ">hidden<" not in page and "→ hidden" not in page
    assert "Розташування в реєстрі" in page
    assert "Застосувати" not in page


def test_registry_workspace_has_compact_presets_state_settings_hierarchy(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    preset = {
        "id": "dark",
        "name": "QA Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_SZ", "value": "QA"}
        ],
    }
    configurations(app).upsert(configuration(WindowsRegistry(), presets=[preset]))

    client = app.test_client()
    page = client.get("/products/sample").get_data(as_text=True)

    workspace = page[
        page.index('id="windows-registry"') : page.index('id="operation-logs"')
    ]
    current_section = '<summary><span>Поточний стан</span>'
    settings_section = '<summary><span>Налаштування реєстру</span>'
    assert workspace.index("PRESETS") < workspace.index(current_section)
    assert workspace.index(current_section) < workspace.index(settings_section)
    presets_section = workspace[
        workspace.index("PRESETS") : workspace.index(current_section)
    ]
    assert "Деталі preset" in presets_section
    assert "Редагувати" in presets_section and "Видалити" in presets_section
    assert "Preset management" not in workspace
    assert 'class="registry-presets-header"' in workspace
    assert (
        'class="plugin-settings registry-preset-editor registry-preset-create"'
        in presets_section
    )
    assert 'class="registry-card-list registry-preset-list"' in presets_section
    assert 'class="registry-edit registry-preset-editor"' in presets_section
    assert '<details class="registry-workspace-section"' in workspace
    assert workspace.count("ПОПЕРЕДНІЙ ПЕРЕГЛЯД PRESET") == 0
    assert workspace.count("+ Новий preset") == 1


def test_registry_capability_and_primary_ui_are_localized(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    configurations(app).upsert(configuration(WindowsRegistry()))

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "ЛИШЕ ЧИТАННЯ" not in page
    assert "ЗМІНИ РЕЄСТРУ" in page
    assert "Presets · Поточний стан · Налаштування" in page
    assert "Керування реєстром" in page
    assert "Ресурси Registry value" in page
    assert "Ресурси Registry branch" in page
    assert "REG_BINARY" in page and "HKCU" in page


def test_registry_redirect_and_validation_keep_workspace_and_editor_open(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    plugin = WindowsRegistry(FakeRegistryReader())
    preset = {
        "id": "qa",
        "name": "QA Debug",
        "values": [
            {"target_id": "theme", "registry_type": "REG_SZ", "value": "QA"}
        ],
    }
    configurations(app).upsert(configuration(plugin, presets=[preset]))
    client = app.test_client()
    normal_page = client.get("/products/sample").get_data(as_text=True)

    redirected = client.post(
        "/products/sample/plugins/windows-registry/value-targets",
        data={
            "original_id": "theme",
            **VALUE_TARGET,
            "enabled": "on",
        },
    )
    redirected_page = client.get(redirected.location).get_data(as_text=True)
    invalid = client.post(
        "/products/sample/plugins/windows-registry/presets",
        data={"original_id": "qa", "name": ""},
    )
    invalid_page = invalid.get_data(as_text=True)

    assert "open=windows-registry" in redirected.location
    assert '<details id="windows-registry" class="plugin-workspace" open>' not in (
        normal_page
    )
    assert '<details id="windows-registry" class="plugin-workspace" open>' in (
        redirected_page
    )
    assert invalid.status_code == 400
    assert '<details id="windows-registry" class="plugin-workspace" open>' in (
        invalid_page
    )
    assert '<details class="registry-edit registry-preset-editor" open>' in (
        invalid_page
    )
