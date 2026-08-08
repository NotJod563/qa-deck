"""Restore Plan foundation tests for QA Deck."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from qa_deck.domain import Product, Snapshot, SnapshotResource
from qa_deck.plugins import PluginManager, RiskLevel
from qa_deck.plugins.api import SnapshotRestorePreparation
from qa_deck.plugins.builtin.license_manager import LicenseManager
from qa_deck.snapshot import (
    RestorePlanStatus,
    SnapshotDiffer,
    SnapshotRestorePlanner,
)
from qa_deck.storage import PluginConfigurationRepository, SnapshotRepository
from tests.helpers import license_configuration, make_app


def _resource(
    identifier: str,
    status: str,
    *,
    source: str = "tests.restore",
    schema_version: int = 1,
) -> SnapshotResource:
    return SnapshotResource(
        source=source,
        resource_type="test-resource",
        identifier=identifier,
        schema_version=schema_version,
        state={"status": status},
    )


def _snapshot(
    snapshot_id: str,
    *resources: SnapshotResource,
    product_id: str = "sample",
) -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        product_id=product_id,
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        label="Baseline",
        resources=resources,
    )


class _FixedSnapshotBuilder:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def build_snapshot(
        self,
        product: Product,
        label: str | None = None,
    ) -> Snapshot:
        self.calls += 1
        assert product.id == self.snapshot.product_id
        assert label is None
        return self.snapshot


class _RestoreProvider:
    identifier = "tests.restore"
    display_name = "Test Restore Provider"
    description = "Test provider"
    version = "1.0"

    def __init__(self, supported_versions: set[int] | None = None) -> None:
        self.supported_versions = supported_versions or {1}
        self.calls: list[
            tuple[SnapshotResource | None, SnapshotResource | None]
        ] = []

    def get_actions(self) -> list[object]:
        return []

    def can_restore(self, resource: SnapshotResource) -> bool:
        return resource.schema_version in self.supported_versions

    def prepare_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: object,
    ) -> SnapshotRestorePreparation:
        assert product.id == "sample"
        assert configuration is None
        self.calls.append((snapshot_resource, current_resource))
        return SnapshotRestorePreparation(
            action_description="Apply the desired test state.",
            risk_level=RiskLevel.CAUTION,
            fingerprint="current-state-fingerprint",
        )

    def execute_restore(self, **kwargs: object) -> object:
        raise AssertionError("Planning tests must not execute restore")


def _planner(
    tmp_path: Path,
    current: Snapshot,
    *plugins: object,
    configuration_repository: object | None = None,
) -> tuple[SnapshotRestorePlanner, _FixedSnapshotBuilder]:
    manager = PluginManager()
    for plugin in plugins:
        manager.register(plugin)  # type: ignore[arg-type]
    builder = _FixedSnapshotBuilder(current)
    repository = configuration_repository or PluginConfigurationRepository(
        tmp_path / "configurations.json"
    )
    return (
        SnapshotRestorePlanner(
            builder,  # type: ignore[arg-type]
            SnapshotDiffer(),
            manager,
            repository,  # type: ignore[arg-type]
        ),
        builder,
    )


def test_plugin_without_restore_capability_is_unsupported(tmp_path: Path) -> None:
    class CaptureOnlyPlugin:
        identifier = "tests.restore"
        display_name = "Capture only"
        description = "No restore capability"
        version = "1.0"

        def get_actions(self) -> list[object]:
            return []

    planner, _ = _planner(
        tmp_path,
        _snapshot("current", _resource("item", "current")),
        CaptureOnlyPlugin(),
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot("desired", _resource("item", "desired")),
    )

    assert plan.entries[0].status is RestorePlanStatus.UNSUPPORTED
    assert plan.unsupported_count == 1


def test_planning_only_provider_is_not_reported_as_ready(tmp_path: Path) -> None:
    class PlanningOnlyProvider(_RestoreProvider):
        execute_restore = None

    planner, _ = _planner(
        tmp_path,
        _snapshot("current", _resource("item", "current")),
        PlanningOnlyProvider(),
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot("desired", _resource("item", "desired")),
    )

    assert plan.entries[0].status is RestorePlanStatus.UNSUPPORTED
    assert "не підтримує автоматичне відновлення" in (
        plan.entries[0].action_description
    )


def test_supported_provider_receives_current_to_snapshot_direction(
    tmp_path: Path,
) -> None:
    current_resource = _resource("item", "current")
    desired_resource = _resource("item", "desired")
    provider = _RestoreProvider()
    planner, _ = _planner(
        tmp_path,
        _snapshot("transient-current", current_resource),
        provider,
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot("persisted-snapshot", desired_resource),
    )

    assert provider.calls == [(desired_resource, current_resource)]
    assert plan.entries[0].status is RestorePlanStatus.READY
    assert plan.entries[0].current_resource is current_resource
    assert plan.entries[0].desired_resource is desired_resource
    assert plan.entries[0].fingerprint == "current-state-fingerprint"


def test_unsupported_schema_version_does_not_call_provider(tmp_path: Path) -> None:
    provider = _RestoreProvider({1})
    planner, _ = _planner(
        tmp_path,
        _snapshot("current", _resource("item", "current")),
        provider,
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot(
            "desired",
            _resource("item", "desired", schema_version=2),
        ),
    )

    assert plan.entries[0].status is RestorePlanStatus.UNSUPPORTED
    assert provider.calls == []


def test_unchanged_resource_is_reported_without_provider_call(
    tmp_path: Path,
) -> None:
    provider = _RestoreProvider()
    resource = _resource("item", "same")
    planner, _ = _planner(
        tmp_path,
        _snapshot("current", resource),
        provider,
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot("desired", _resource("item", "same")),
    )

    assert plan.entries[0].status is RestorePlanStatus.NO_CHANGE
    assert plan.no_change_count == 1
    assert provider.calls == []


def test_provider_configuration_and_result_failures_are_isolated(
    tmp_path: Path,
) -> None:
    class IsolatedProvider(_RestoreProvider):
        def prepare_restore(
            self,
            product: Product,
            snapshot_resource: SnapshotResource | None,
            current_resource: SnapshotResource | None,
            configuration: object,
        ) -> SnapshotRestorePreparation:
            assert snapshot_resource is not None
            if snapshot_resource.identifier == "raises":
                raise OSError("private provider detail")
            if snapshot_resource.identifier == "invalid":
                return cast(SnapshotRestorePreparation, object())
            return super().prepare_restore(
                product,
                snapshot_resource,
                current_resource,
                configuration,
            )

    class FailingConfigurationRepository:
        def get(self, product_id: str, plugin_identifier: str) -> None:
            assert product_id == "sample"
            if plugin_identifier == "tests.config-failure":
                raise OSError("private configuration detail")
            return None

    provider = IsolatedProvider()
    config_provider = _RestoreProvider()
    config_provider.identifier = "tests.config-failure"
    identifiers = ("raises", "invalid", "healthy")
    planner, _ = _planner(
        tmp_path,
        _snapshot(
            "current",
            *(_resource(identifier, "current") for identifier in identifiers),
            _resource(
                "configuration",
                "current",
                source="tests.config-failure",
            ),
        ),
        provider,
        config_provider,
        configuration_repository=FailingConfigurationRepository(),
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot(
            "desired",
            *(_resource(identifier, "desired") for identifier in identifiers),
            _resource(
                "configuration",
                "desired",
                source="tests.config-failure",
            ),
        ),
    )

    statuses = {entry.identifier: entry.status for entry in plan.entries}
    assert statuses == {
        "configuration": RestorePlanStatus.ERROR,
        "healthy": RestorePlanStatus.READY,
        "invalid": RestorePlanStatus.ERROR,
        "raises": RestorePlanStatus.ERROR,
    }
    assert all(
        "private provider detail" not in item.action_description
        for item in plan.entries
    )


def test_cross_product_snapshot_is_rejected_before_current_capture(
    tmp_path: Path,
) -> None:
    planner, builder = _planner(tmp_path, _snapshot("current"))

    with pytest.raises(ValueError, match="different product"):
        planner.prepare(
            Product("sample", "Sample"),
            _snapshot("desired", product_id="other"),
        )

    assert builder.calls == 0


def test_license_manager_prepare_restore_reuses_read_only_change_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    license_directory = tmp_path / "licenses"
    license_directory.mkdir()
    license_file = license_directory / "license.dat"
    license_file.write_text("unchanged-content", encoding="utf-8")
    configuration = license_configuration(license_directory)
    plugin = LicenseManager()
    original_build_plan = plugin.build_plan
    calls: list[str] = []

    def build_plan_spy(
        product_id: str,
        stored_configuration: object,
        action_identifier: str,
        *,
        inspection: object = None,
    ) -> object:
        calls.append(action_identifier)
        return original_build_plan(
            product_id,
            stored_configuration,  # type: ignore[arg-type]
            action_identifier,
            inspection=inspection,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(plugin, "build_plan", build_plan_spy)
    current = SnapshotResource(
        plugin.identifier,
        "license",
        "license.dat",
        state={"status": "active"},
    )
    desired = SnapshotResource(
        plugin.identifier,
        "license",
        "license.dat",
        state={"status": "hidden"},
    )

    preparation = plugin.prepare_restore(
        Product("sample", "Sample"),
        desired,
        current,
        configuration,
    )

    assert calls == ["hide-licenses"]
    assert preparation.changes_required is True
    assert preparation.blocking_error is None
    assert preparation.fingerprint
    assert license_file.read_text(encoding="utf-8") == "unchanged-content"
    assert not (license_directory / "license.dat.hidden").exists()
    assert not (tmp_path / "backups").exists()


def test_missing_license_configuration_is_explicitly_blocked(
    tmp_path: Path,
) -> None:
    desired = SnapshotResource(
        LicenseManager.identifier,
        "license",
        "license.dat",
        state={"status": "active"},
    )
    planner, _ = _planner(
        tmp_path,
        _snapshot("current"),
        LicenseManager(),
    )

    plan = planner.prepare(
        Product("sample", "Sample"),
        _snapshot("desired", desired),
    )

    assert plan.entries[0].status is RestorePlanStatus.BLOCKED
    assert plan.entries[0].blocking_reason == (
        "Поточна конфігурація каталогу ліцензій недоступна; "
        "автоматичне відновлення неможливе."
    )
    assert not (tmp_path / "backups").exists()


def test_restore_plan_ui_is_read_only_anchored_and_transient(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    repository = cast(
        SnapshotRepository,
        app.extensions["snapshot_repository"],
    )
    snapshot = _snapshot(
        "restore-preview",
        SnapshotResource(
            "executable-inspector",
            "executable",
            "primary-executable",
            state={"status": "missing"},
        ),
    )
    repository.add(snapshot)
    before = repository.list_all()

    with app.test_client() as client:
        product_page = client.get("/products/sample").get_data(as_text=True)
        response = client.get(
            "/products/sample/snapshots/restore-preview/restore-plan"
        )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="snapshot-restore-plan"' in html
    assert "Поточне середовище → Baseline" in html
    assert "ПОПЕРЕДНІЙ ПЕРЕГЛЯД ВІДНОВЛЕННЯ" in html
    assert "Перевірте зміни перед відновленням." in html
    assert "Немає змін, доступних для автоматичного відновлення." in html
    assert "автоматичне відновлення" in html
    assert 'name="confirmation_token"' not in html
    assert "Execute" not in html
    assert "Confirm restore" not in html
    assert "Restore now" not in html
    assert (
        "/products/sample/snapshots/restore-preview/restore-plan"
        "#snapshot-restore-plan"
    ) in product_page
    assert "Відновити зі snapshot" in product_page
    assert repository.list_all() == before
    assert not (tmp_path / "backups").exists()
