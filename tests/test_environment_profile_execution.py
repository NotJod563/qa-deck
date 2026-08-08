"""Environment Profile preview, stale protection, execution, and PRG tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest

from qa_deck.domain import (
    EnvironmentProfile,
    EnvironmentProfileLicense,
    ProfileLicenseState,
)
from qa_deck.plugins import PluginManager
from qa_deck.plugins.builtin.windows_registry import WindowsRegistry
from tests.helpers import (
    configurations,
    environment_profiles,
    license_configuration,
    make_app,
    operation_logs,
)
from tests.plugins.test_registry_execution import (
    FakeWriter,
    MutableReader,
    install_registry,
    registry_configuration,
)


def _profile(
    *,
    preset_id: str | None = "qa",
    licenses: tuple[EnvironmentProfileLicense, ...] = (),
) -> EnvironmentProfile:
    return EnvironmentProfile(
        "qa-debug", "sample", "QA Debug Environment", preset_id, licenses
    )


def _preview(client: object) -> tuple[str, str]:
    response = client.post(  # type: ignore[attr-defined]
        "/products/sample/environment-profiles/qa-debug/apply-preview",
        data={
            "registry_path": "HKLM\\Attacker",
            "desired_entries_json": '[{"value": "attacker"}]',
            "license_path": "C:\\secrets\\license.dat",
        },
    )
    html = response.get_data(as_text=True)
    token = re.search(r'name="confirmation_token" value="([^"]+)"', html)
    assert response.status_code == 200 and token is not None
    return token.group(1), html


def _confirm(client: object, token: str, *, follow: bool = True):
    return client.post(  # type: ignore[attr-defined]
        "/products/sample/environment-profiles/qa-debug/apply",
        data={
            "confirmation_token": token,
            "confirm": "yes",
            "registry_path": "HKLM\\Attacker",
            "desired_entries_json": '[{"value": "attacker"}]',
            "license_path": "C:\\secrets\\license.dat",
        },
        follow_redirects=follow,
    )


def _registry_app(tmp_path: Path, *, include_branch: bool = False):
    app = make_app(tmp_path)
    reader = MutableReader("REG_DWORD", 1)
    writer = FakeWriter(reader)
    plugin = WindowsRegistry(reader, writer)
    install_registry(
        app,
        plugin,
        registry_configuration(
            plugin,
            desired_value=99,
            include_branch=include_branch,
        ),
    )
    environment_profiles(app).add(_profile())
    return app, reader, writer, plugin


def test_preview_is_read_only_and_confirmation_uses_only_opaque_intent(
    tmp_path: Path,
) -> None:
    app, reader, writer, _ = _registry_app(tmp_path)
    saved = environment_profiles(app).get("sample", "qa-debug")
    snapshots_before = app.extensions["snapshot_repository"].list_for_product(
        "sample"
    )

    client = app.test_client()
    profile_page = client.get("/products/sample").get_data(as_text=True)
    _, html = _preview(client)

    assert "ПІДТВЕРДЖЕННЯ ЗАСТОСУВАННЯ" in html
    assert 'id="environment-profile-confirmation"' in html
    assert (
        "/environment-profiles/qa-debug/apply-preview"
        "#environment-profile-confirmation"
    ) in profile_page
    assert "1 зміна" in html
    assert writer.calls == [] and writer.branch_calls == []
    assert reader.value == 1
    assert environment_profiles(app).get("sample", "qa-debug") == saved
    assert app.extensions["snapshot_repository"].list_for_product(
        "sample"
    ) == snapshots_before


def test_registry_value_apply_uses_existing_executor_and_prg_is_refresh_safe(
    tmp_path: Path,
) -> None:
    app, reader, writer, _ = _registry_app(tmp_path)
    client = app.test_client()
    token, _ = _preview(client)

    response = _confirm(client, token, follow=False)
    result_page = client.get(response.headers["Location"])
    refreshed = client.get(response.headers["Location"])
    duplicate = _confirm(client, token)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("#environment-profile-result")
    assert reader.value == 99 and len(writer.calls) == 1
    assert "Успішно" in result_page.get_data(as_text=True)
    assert "0 змін" in result_page.get_data(as_text=True)
    assert "0 змін" in refreshed.get_data(as_text=True)
    assert "вже використане" in duplicate.get_data(as_text=True)
    assert len(writer.calls) == 1


@pytest.mark.parametrize("drift", ["edited", "deleted"])
def test_registry_preset_drift_is_stale_without_write(
    tmp_path: Path, drift: str
) -> None:
    app, reader, writer, plugin = _registry_app(tmp_path)
    client = app.test_client()
    token, _ = _preview(client)
    if drift == "edited":
        configurations(app).upsert(
            registry_configuration(plugin, desired_value=255)
        )
    else:
        changed = registry_configuration(plugin, desired_value=99)
        configurations(app).upsert(
            plugin.create_configuration(
                product_id="sample",
                enabled=True,
                value_targets_json=json.dumps(changed.settings["value_targets"]),
                branch_targets_json="[]",
                presets_json="[]",
            )
        )

    page = _confirm(client, token).get_data(as_text=True)

    assert "План застарів" in page
    assert reader.value == 1 and writer.calls == []


@pytest.mark.parametrize("drift", ["edited", "deleted"])
def test_profile_drift_rejects_entire_old_intent(
    tmp_path: Path, drift: str
) -> None:
    app, reader, writer, _ = _registry_app(tmp_path)
    client = app.test_client()
    token, _ = _preview(client)
    if drift == "edited":
        environment_profiles(app).update(
            EnvironmentProfile("qa-debug", "sample", "Edited", "qa")
        )
    else:
        environment_profiles(app).remove("sample", "qa-debug")

    page = _confirm(client, token).get_data(as_text=True)

    assert "Profile змінився або був видалений" in page
    assert reader.value == 1 and writer.calls == []


def test_registry_current_drift_and_no_change_do_not_write(tmp_path: Path) -> None:
    app, reader, writer, _ = _registry_app(tmp_path)
    client = app.test_client()
    token, _ = _preview(client)
    reader.value = 99

    stale = _confirm(client, token).get_data(as_text=True)
    token, preview = _preview(client)
    unchanged = _confirm(client, token).get_data(as_text=True)

    assert "План застарів" in stale
    assert "Без змін" in preview and "Без змін" in unchanged
    assert writer.calls == []


def test_registry_target_config_drift_never_writes_old_target(tmp_path: Path) -> None:
    app, reader, writer, plugin = _registry_app(tmp_path)
    client = app.test_client()
    token, _ = _preview(client)
    configurations(app).upsert(
        registry_configuration(
            plugin,
            desired_value=99,
            value_target={
                "id": "mode",
                "hive": "HKCU",
                "key_path": "Software\\Changed",
                "value_name": "Mode",
                "display_name": "Mode",
                "enabled": True,
            },
        )
    )

    page = _confirm(client, token).get_data(as_text=True)

    assert "План застарів" in page
    assert reader.value == 1 and writer.calls == []


def test_registry_native_branch_executes_through_profile(tmp_path: Path) -> None:
    app, reader, writer, _ = _registry_app(tmp_path, include_branch=True)
    client = app.test_client()
    token, _ = _preview(client)

    _confirm(client, token)

    assert len(writer.calls) == 1
    assert len(writer.branch_calls) == 1
    assert reader.value == 99
    assert not reader.original_exists and reader.hidden_exists


@pytest.mark.parametrize(
    ("desired", "start_hidden"),
    [
        (ProfileLicenseState.HIDDEN, False),
        (ProfileLicenseState.ACTIVE, True),
    ],
)
def test_license_transitions_execute_through_existing_service(
    tmp_path: Path,
    desired: ProfileLicenseState,
    start_hidden: bool,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    suffix = ".hidden" if start_hidden else ""
    (directory / f"license.dat{suffix}").write_text("license", encoding="utf-8")
    configurations(app).upsert(license_configuration(directory))
    environment_profiles(app).add(
        _profile(
            preset_id=None,
            licenses=(EnvironmentProfileLicense("license.dat", desired),),
        )
    )
    client = app.test_client()
    token, _ = _preview(client)

    page = _confirm(client, token).get_data(as_text=True)

    assert "Успішно" in page
    assert (directory / "license.dat").exists() is (
        desired is ProfileLicenseState.ACTIVE
    )
    assert (directory / "license.dat.hidden").exists() is (
        desired is ProfileLicenseState.HIDDEN
    )


def test_license_config_drift_blocks_old_path_and_keeps_profile_and_snapshots(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    old_directory = tmp_path / "old"
    new_directory = tmp_path / "new"
    old_directory.mkdir()
    new_directory.mkdir()
    (old_directory / "license.dat").write_text("old", encoding="utf-8")
    (new_directory / "license.dat").write_text("new", encoding="utf-8")
    configurations(app).upsert(license_configuration(old_directory))
    saved = _profile(
        preset_id=None,
        licenses=(
            EnvironmentProfileLicense(
                "license.dat", ProfileLicenseState.HIDDEN
            ),
        ),
    )
    environment_profiles(app).add(saved)
    snapshots_before = app.extensions["snapshot_repository"].list_for_product(
        "sample"
    )
    client = app.test_client()
    token, _ = _preview(client)
    configurations(app).upsert(license_configuration(new_directory))

    page = _confirm(client, token).get_data(as_text=True)

    assert "План застарів" in page
    assert (old_directory / "license.dat").exists()
    assert not (old_directory / "license.dat.hidden").exists()
    assert environment_profiles(app).get("sample", "qa-debug") == saved
    assert app.extensions["snapshot_repository"].list_for_product(
        "sample"
    ) == snapshots_before


def test_missing_license_configuration_is_blocked_without_write(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    environment_profiles(app).add(
        _profile(
            preset_id=None,
            licenses=(
                EnvironmentProfileLicense(
                    "license.dat", ProfileLicenseState.HIDDEN
                ),
            ),
        )
    )
    client = app.test_client()

    token, preview = _preview(client)
    result = _confirm(client, token).get_data(as_text=True)

    assert "Заблоковано" in preview and "Заблоковано" in result
    assert not (tmp_path / "license.dat.hidden").exists()


def test_mixed_profile_isolates_provider_failure_and_logs_safe_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, reader, writer, _ = _registry_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    (directory / "license.dat").write_text("secret-license", encoding="utf-8")
    configurations(app).upsert(license_configuration(directory))
    mixed = _profile(
        licenses=(
            EnvironmentProfileLicense(
                "license.dat", ProfileLicenseState.HIDDEN
            ),
        )
    )
    environment_profiles(app).update(mixed)
    client = app.test_client()
    token, _ = _preview(client)
    manager = cast(PluginManager, app.extensions["plugin_manager"])
    license_plugin = manager.get("license-manager")
    assert license_plugin is not None

    def fail_execution(*_args: object, **_kwargs: object) -> object:
        raise OSError("provider failed")

    monkeypatch.setattr(
        license_plugin, "execute_environment_profile", fail_execution
    )

    page = _confirm(client, token).get_data(as_text=True)

    assert reader.value == 99 and len(writer.calls) == 1
    assert (directory / "license.dat").exists()
    assert "Успішно" in page and "Помилка" in page
    aggregate = [
        item
        for item in operation_logs(app).list_for_product("sample", limit=20)
        if item.plugin_identifier == "environment-profile"
    ][0]
    assert aggregate.status.value == "partial"
    assert "secret-license" not in aggregate.summary
    assert "REG_DWORD" not in aggregate.summary and "99" not in aggregate.summary


def test_core_execution_has_no_builtin_coupling_or_active_profile_state() -> None:
    source = Path("src/qa_deck/environment_profiles.py").read_text(
        encoding="utf-8"
    )
    domain_source = Path(
        "src/qa_deck/domain/environment_profile.py"
    ).read_text(encoding="utf-8")

    assert "windows_registry" not in source and "license_manager" not in source
    assert "active_profile_id" not in source + domain_source
