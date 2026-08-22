"""Minimal Environment Profile model, comparison, storage, and UI behavior."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

from qa_deck.domain import (
    EnvironmentProfile,
    EnvironmentProfileLicense,
    Product,
    ProfileLicenseState,
)
from qa_deck.environment_profiles import EnvironmentProfileComparator
from qa_deck.plugins import PluginManager
from qa_deck.plugins.builtin.windows_registry import WindowsRegistry
from qa_deck.storage import (
    EnvironmentProfileRepository,
    PluginConfigurationRepository,
)
from tests.helpers import (
    configurations,
    environment_profiles,
    license_configuration,
    make_app,
)
from tests.plugins.test_registry_execution import (
    BRANCH_TARGET,
    VALUE_TARGET,
    FakeWriter,
    MutableReader,
)


def profile(
    *,
    profile_id: str = "qa-debug",
    product_id: str = "sample",
    preset_id: str | None = "qa",
    licenses: tuple[EnvironmentProfileLicense, ...] = (),
) -> EnvironmentProfile:
    return EnvironmentProfile(
        profile_id,
        product_id,
        "QA Debug",
        preset_id,
        licenses,
    )


def registry_configuration(
    plugin: WindowsRegistry,
    desired_value: int,
    *,
    include_preset: bool = True,
):
    presets = (
        [
            {
                "id": "qa",
                "name": "QA Debug",
                "values": [
                    {
                        "target_id": "mode",
                        "registry_type": "REG_DWORD",
                        "value": desired_value,
                    }
                ],
            }
        ]
        if include_preset
        else []
    )
    return plugin.create_configuration(
        product_id="sample",
        enabled=True,
        value_targets_json=json.dumps([VALUE_TARGET]),
        branch_targets_json=json.dumps([BRANCH_TARGET]),
        presets_json=json.dumps(presets),
    )


def install_registry(app, plugin: WindowsRegistry, desired_value: int) -> None:
    configurations(app).upsert(registry_configuration(plugin, desired_value))
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    manager._plugins[plugin.identifier] = plugin  # noqa: SLF001


def test_profile_serialization_roundtrip_keeps_references_only() -> None:
    original = profile(
        licenses=(
            EnvironmentProfileLicense(
                "license.dat", ProfileLicenseState.ACTIVE
            ),
        )
    )

    restored = EnvironmentProfile.from_dict(original.to_dict())

    assert restored == original
    serialized = json.dumps(original.to_dict(), sort_keys=True)
    assert '"registry_preset_id": "qa"' in serialized
    assert "registry_type" not in serialized and "key_path" not in serialized
    assert "active_profile_id" not in serialized
    with pytest.raises(ValueError):
        EnvironmentProfileLicense(
            "C:\\secrets\\license.dat", ProfileLicenseState.ACTIVE
        )
    with pytest.raises(ValueError):
        EnvironmentProfile.from_dict(
            {**original.to_dict(), "script": "do-dangerous-work"}
        )


def test_repository_is_product_scoped_atomic_updatable_and_removable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.json"
    repository = EnvironmentProfileRepository(path)
    sample = profile()
    other = profile(product_id="other")

    repository.add(sample)
    repository.add(other)
    repository.update(
        EnvironmentProfile(sample.id, sample.product_id, "Renamed", "qa")
    )

    assert repository.get("sample", sample.id).name == "Renamed"  # type: ignore[union-attr]
    assert repository.get("other", other.id) == other
    assert repository.remove("sample", sample.id) is not None
    assert repository.get("sample", sample.id) is None
    assert repository.get("other", other.id) == other
    assert not list(tmp_path.glob(".*.tmp"))
    with pytest.raises(ValueError):
        repository.add(other)


def test_corrupted_profile_storage_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text("{broken", encoding="utf-8")
    repository = EnvironmentProfileRepository(path)

    with pytest.raises(json.JSONDecodeError):
        repository.add(profile())

    assert path.read_text(encoding="utf-8") == "{broken"


def test_registry_profile_reference_is_dynamic_fresh_and_read_only(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, 1)
    environment_profiles(app).add(profile())
    client = app.test_client()

    matching = client.get("/products/sample").get_data(as_text=True)
    configurations(app).upsert(registry_configuration(plugin, 99))
    changed = client.get("/products/sample").get_data(as_text=True)
    reader.value = 99
    refreshed = client.get("/products/sample").get_data(as_text=True)

    assert "Профілі середовища" in matching
    assert "QA Debug preset" in matching
    assert "0 змін" in matching
    profile_workspace = matching[
        matching.index('id="environment-profiles"') : matching.index(
            'id="log-collector"'
        )
    ]
    assert "Застосувати profile" in profile_workspace
    assert '<details class="inline-details"><summary>Деталі</summary>' in (
        profile_workspace
    )
    assert "+ Новий profile" in profile_workspace and "Редагувати" in (
        profile_workspace
    )
    assert "1 зміна" in changed
    assert "REG_DWORD: 1 → REG_DWORD: 99" in changed
    assert "0 змін" in refreshed
    assert reader.value_reads == 3
    assert writer.calls == [] and writer.branch_calls == []
    stored = environment_profiles(app).get("sample", "qa-debug")
    assert stored == profile()


def test_missing_registry_preset_is_blocked_without_replacement(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, 1)
    environment_profiles(app).add(profile())
    configurations(app).upsert(
        registry_configuration(plugin, 1, include_preset=False)
    )

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "1 заблоковано" in page
    assert "Registry preset більше не налаштований." in page
    assert environment_profiles(app).get("sample", "qa-debug") == profile()
    assert writer.calls == []


def test_license_profile_uses_configured_identity_and_blocks_removed_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    (directory / "license.dat").write_text("license", encoding="utf-8")
    configurations(app).upsert(
        license_configuration(directory, ["license.dat"])
    )
    license_profile = profile(
        preset_id=None,
        licenses=(
            EnvironmentProfileLicense(
                "license.dat", ProfileLicenseState.HIDDEN
            ),
        ),
    )
    environment_profiles(app).add(license_profile)
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    license_plugin = manager.get("license-manager")
    assert license_plugin is not None
    monkeypatch.setattr(
        license_plugin,
        "execute",
        lambda **_context: pytest.fail("Profile comparison must not execute"),
    )
    client = app.test_client()

    changed = client.get("/products/sample").get_data(as_text=True)
    (directory / "license.dat").rename(directory / "license.dat.hidden")
    matching = client.get("/products/sample").get_data(as_text=True)
    configurations(app).upsert(license_configuration(directory, ["other.lic"]))
    blocked = client.get("/products/sample").get_data(as_text=True)

    assert "Активна → Прихована" in changed
    assert "Прихована → Прихована" in matching
    assert "Ліцензійний ресурс більше не налаштований." in blocked
    assert not (directory / "license.dat").exists()
    assert (directory / "license.dat.hidden").exists()


def test_profile_crud_keeps_stable_id_and_does_not_mutate_runtime_or_snapshots(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(app, plugin, 1)
    client = app.test_client()
    snapshot_repository = app.extensions["snapshot_repository"]
    client.post("/products/sample/snapshots", data={"label": "Before Profiles"})
    snapshots_before = snapshot_repository.list_for_product("sample")

    rejected = client.post(
        "/products/sample/environment-profiles",
        data={
            "name": "Unsafe",
            "registry_preset_id": "HKLM\\Software\\Attacker",
        },
    )
    created = client.post(
        "/products/sample/environment-profiles",
        data={
            "name": "QA Debug",
            "registry_preset_id": "qa",
            "registry_path": "HKLM\\Attacker",
            "script": "do-dangerous-work",
        },
    )
    created_profile = environment_profiles(app).get("sample", "qa-debug")
    collision = client.post(
        "/products/sample/environment-profiles",
        data={"name": "QA Debug", "registry_preset_id": "qa"},
    )
    updated = client.post(
        "/products/sample/environment-profiles/qa-debug",
        data={"name": "Renamed", "registry_preset_id": "qa"},
    )
    rejected_delete = client.post(
        "/products/sample/environment-profiles/qa-debug/delete"
    )
    deleted = client.post(
        "/products/sample/environment-profiles/qa-debug/delete",
        data={"confirm": "yes"},
    )

    assert rejected.status_code == 400
    assert created.status_code == 302 and collision.status_code == 302
    assert updated.status_code == 302
    assert "open=environment-profiles" in created.headers["Location"]
    assert created.headers["Location"].endswith("#environment-profiles")
    assert "open=environment-profiles" in updated.headers["Location"]
    assert updated.headers["Location"].endswith("#environment-profiles")
    assert created_profile is not None and created_profile.license_states == ()
    assert rejected_delete.status_code == 400 and deleted.status_code == 302
    assert "open=environment-profiles" in deleted.headers["Location"]
    assert deleted.headers["Location"].endswith("#environment-profiles")
    page = client.get(deleted.headers["Location"]).get_data(as_text=True)
    assert (
        '<details id="environment-profiles" class="state-workspace" open>'
        in page
    )
    assert environment_profiles(app).get("sample", "qa-debug") is None
    assert environment_profiles(app).get("sample", "qa-debug-2") is not None
    assert snapshot_repository.list_for_product("sample") == snapshots_before
    assert reader.value == 1 and writer.calls == []


def test_snapshot_restore_execution_does_not_mutate_profile_collection(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    active = directory / "license.dat"
    hidden = directory / "license.dat.hidden"
    active.write_text("license", encoding="utf-8")
    configurations(app).upsert(
        license_configuration(directory, ["license.dat"])
    )
    saved_profile = profile(
        preset_id=None,
        licenses=(
            EnvironmentProfileLicense(
                "license.dat", ProfileLicenseState.ACTIVE
            ),
        ),
    )
    environment_profiles(app).add(saved_profile)
    client = app.test_client()
    client.post("/products/sample/snapshots", data={"label": "Baseline"})
    snapshot = app.extensions["snapshot_repository"].list_for_product("sample")[0]
    active.rename(hidden)
    plan = client.get(
        f"/products/sample/snapshots/{snapshot.id}/restore-plan"
    ).get_data(as_text=True)
    token = re.search(r'name="confirmation_token" value="([^"]+)"', plan)
    assert token is not None

    client.post(
        f"/products/sample/snapshots/{snapshot.id}/restore",
        data={"confirmation_token": token.group(1), "confirm": "yes"},
    )

    assert environment_profiles(app).list_for_product("sample") == [saved_profile]
    assert active.exists() and not hidden.exists()


def test_generic_comparator_enforces_ownership_and_has_no_builtin_coupling(
    tmp_path: Path,
) -> None:
    comparator = EnvironmentProfileComparator(
        PluginManager(),
        PluginConfigurationRepository(tmp_path / "configurations.json"),
    )

    with pytest.raises(ValueError):
        comparator.compare_all(
            Product("sample", "Sample"),
            [profile(product_id="other")],
        )

    source = Path("src/qa_deck/environment_profiles.py").read_text(
        encoding="utf-8"
    )
    assert "windows_registry" not in source
    assert "license_manager" not in source
