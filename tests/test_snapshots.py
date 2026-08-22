"""Snapshot feature tests for QA Deck."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qa_deck.domain import Product, Snapshot, SnapshotResource
from qa_deck.domain.snapshot import SnapshotCaptureResult
from qa_deck.plugins.builtin import ExecutableInspector, LicenseManager
from qa_deck.plugins.manager import PluginManager
from qa_deck.snapshot import SnapshotBuilder, SnapshotDiffStatus
from qa_deck.storage import PluginConfigurationRepository, SnapshotRepository
from tests.helpers import (
    configurations,
    license_configuration,
    log_configuration,
    make_app,
    products,
)


def test_snapshot_model_validation_and_utc_serialization() -> None:
    snapshot = Snapshot(
        id="snapshot-1",
        product_id="sample",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        label="  Test label  ",
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="C:/app.exe",
                state={"status": "available"},
            ),
        ),
    )

    assert snapshot.label == "Test label"
    assert snapshot.created_at.tzinfo is not None
    assert snapshot.created_at.utcoffset() == UTC.utcoffset(snapshot.created_at)

    roundtrip = Snapshot.from_dict(snapshot.to_dict())
    assert roundtrip == snapshot


def test_snapshot_repository_add_get_list_for_product_and_isolation(
    tmp_path: Path,
) -> None:
    repository = SnapshotRepository(tmp_path / "snapshots.json")
    first = Snapshot(
        id="first",
        product_id="sample",
        created_at=datetime.now(UTC),
        label="First",
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="C:/app.exe",
                state={"status": "available"},
            ),
        ),
    )
    second = Snapshot(
        id="second",
        product_id="other",
        created_at=datetime.now(UTC),
        label="Other",
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="C:/other.exe",
                state={"status": "missing"},
            ),
        ),
    )

    repository.add(first)
    repository.add(second)

    assert repository.get("first") == first
    assert repository.list_for_product("sample") == [first]
    assert repository.list_for_product("other") == [second]


def test_corrupted_snapshot_json_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.json"
    original = "{broken private data"
    path.write_text(original, encoding="utf-8")
    repository = SnapshotRepository(path)
    snapshot = Snapshot(
        id="snapshot-1",
        product_id="sample",
        created_at=datetime.now(UTC),
        label=None,
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="not_configured",
                state={"status": "error"},
            ),
        ),
    )

    with pytest.raises(ValueError):
        repository.get("snapshot-1")
    with pytest.raises(ValueError):
        repository.add(snapshot)
    assert path.read_text(encoding="utf-8") == original


def test_snapshot_builder_collects_executable_license_and_log_sources(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    sample_product = products(app).get("sample")
    assert sample_product is not None

    executable_path = tmp_path / "test.exe"
    executable_path.write_bytes(b"binary")
    sample_product.executable_path = str(executable_path)

    license_dir = tmp_path / "licenses"
    license_dir.mkdir()
    (license_dir / "license.dat").write_bytes(b"license")
    configurations(app).upsert(
        license_configuration(license_dir, ["license.dat"], enabled=True)
    )

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "application.log").write_bytes(b"log data")
    configurations(app).upsert(
        log_configuration(log_dir, enabled=True)
    )

    builder = SnapshotBuilder(
        app.extensions["plugin_manager"],
        configurations(app),
        app.logger,
    )
    snapshot = builder.build_snapshot(sample_product, label="Initial snapshot")

    assert snapshot.product_id == sample_product.id
    assert snapshot.label == "Initial snapshot"
    assert len(snapshot.resources) == 3
    assert any(
        resource.resource_type == "executable"
        for resource in snapshot.resources
    )
    assert any(
        resource.resource_type == "license"
        for resource in snapshot.resources
    )
    assert any(
        resource.resource_type == "log-source"
        for resource in snapshot.resources
    )
    assert snapshot.metadata.get("warnings") is None


def test_create_snapshot_route_and_product_page_snapshot_listing(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/products/sample/snapshots",
        data={"label": "Initial snapshot"},
    )
    assert response.status_code == 302
    assert response.location.endswith(
        "/products/sample?open=snapshots#snapshots"
    )

    page = client.get(response.location).get_data(as_text=True)
    assert 'id="snapshots"' in page
    assert '<details id="snapshots" class="state-workspace" open>' in page
    assert "Initial snapshot" in page
    assert "Останні snapshots" in page
    assert "Restore змінює підтримуваний runtime state лише після підтвердження" in page
    assert "Лише читання" not in page


def test_create_snapshot_route_rejects_invalid_label(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/products/sample/snapshots",
        data={"label": "\x00Invalid"},
    )

    assert response.status_code == 400
    assert "заборонені символи" in response.get_data(as_text=True)


def test_create_snapshot_route_returns_404_for_unknown_product(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/products/unknown/snapshots",
        data={"label": "Snapshot"},
    )
    assert response.status_code == 404


def test_snapshot_builder_uses_generic_snapshot_provider_contract(
    tmp_path: Path,
) -> None:
    manager = PluginManager()
    configuration_repository = PluginConfigurationRepository(
        tmp_path / "configurations.json"
    )
    product = Product("sample", "Sample Product")

    class GenericProvider:
        identifier = "tests.generic_provider"
        display_name = "Generic Provider"
        description = "A generic snapshot provider for test."
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(
            self,
            product: Product,
            configuration: object | None,
        ) -> SnapshotCaptureResult:
            return SnapshotCaptureResult(
                resources=(
                    SnapshotResource(
                        source=self.identifier,
                        resource_type="generic",
                        identifier=product.id,
                        state={"value": 1},
                    ),
                ),
            )

    manager.register(GenericProvider())
    snapshot = SnapshotBuilder(
        manager,
        configuration_repository,
    ).build_snapshot(product)

    assert any(
        resource.source == "tests.generic_provider"
        for resource in snapshot.resources
    )
    assert any(
        resource.resource_type == "generic"
        for resource in snapshot.resources
    )


def test_snapshot_builder_ignores_non_snapshot_plugins_and_continues_on_failure(
    tmp_path: Path,
) -> None:
    manager = PluginManager()
    configuration_repository = PluginConfigurationRepository(
        tmp_path / "configurations.json"
    )
    product = Product("sample", "Sample Product")

    class NonSnapshotPlugin:
        identifier = "tests.non_snapshot"
        display_name = "Non snapshot plugin"
        description = "Ignored by SnapshotBuilder."
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

    class BrokenSnapshotProvider:
        identifier = "tests.broken_provider"
        display_name = "Broken Provider"
        description = "Fails during capture_snapshot."
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(
            self,
            product: Product,
            configuration: object | None,
        ) -> SnapshotCaptureResult:
            raise RuntimeError("boom")

    class HealthySnapshotProvider:
        identifier = "tests.healthy_provider"
        display_name = "Healthy Provider"
        description = "Returns a safe resource."
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(
            self,
            product: Product,
            configuration: object | None,
        ) -> SnapshotCaptureResult:
            return SnapshotCaptureResult(
                resources=(
                    SnapshotResource(
                        source=self.identifier,
                        resource_type="healthy",
                        identifier=product.id,
                        state={"ok": True},
                    ),
                ),
            )

    manager.register(NonSnapshotPlugin())
    manager.register(BrokenSnapshotProvider())
    manager.register(HealthySnapshotProvider())

    snapshot = SnapshotBuilder(
        manager,
        configuration_repository,
    ).build_snapshot(product)

    assert any(
        resource.resource_type == "healthy"
        for resource in snapshot.resources
    )
    assert not any(
        resource.resource_type == "non_snapshot"
        for resource in snapshot.resources
    )
    assert snapshot.metadata["warnings"] == (
        "Snapshot provider tests.broken_provider failed.",
    )


def test_snapshot_resource_schema_version_and_backward_compatibility() -> None:
    resource = SnapshotResource(
        source="qa_deck.executable_inspector",
        resource_type="executable",
        identifier="C:/app.exe",
        state={"status": "available"},
    )

    data = resource.to_dict()
    assert data["schema_version"] == 1
    assert "capabilities" not in data
    assert "restore" not in data

    legacy_data = {
        "source": "qa_deck.executable_inspector",
        "resource_type": "executable",
        "identifier": "C:/app.exe",
        "state": {"status": "available"},
    }
    legacy_resource = SnapshotResource.from_dict(legacy_data)
    assert legacy_resource.schema_version == 1


def test_snapshot_differ_detects_changes() -> None:
    from qa_deck.snapshot import SnapshotDiffer

    sample_product_id = "sample"
    base_snapshot = Snapshot(
        id="base",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Base",
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="C:/app.exe",
                state={"status": "available"},
            ),
            SnapshotResource(
                source="qa_deck.license_manager",
                resource_type="license",
                identifier="license.dat",
                state={"present": True},
            ),
        ),
    )
    target_snapshot = Snapshot(
        id="target",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Target",
        resources=(
            SnapshotResource(
                source="qa_deck.executable_inspector",
                resource_type="executable",
                identifier="C:/app.exe",
                state={"status": "missing"},
            ),
            SnapshotResource(
                source="qa_deck.log_collector",
                resource_type="log-source",
                identifier="C:/logs/application.log",
                state={"available": True},
            ),
        ),
    )

    diff = SnapshotDiffer().diff(base_snapshot, target_snapshot)

    assert diff.base_snapshot_id == "base"
    assert diff.target_snapshot_id == "target"
    assert diff.added_count == 1
    assert diff.removed_count == 1
    assert diff.changed_count == 1
    assert diff.unchanged_count == 0
    statuses = {entry.status for entry in diff.entries}
    assert statuses == {
        SnapshotDiffStatus.CHANGED,
        SnapshotDiffStatus.REMOVED,
        SnapshotDiffStatus.ADDED,
    }


def test_snapshot_differ_tracks_schema_version_mismatch() -> None:
    from qa_deck.snapshot import SnapshotDiffer

    sample_product_id = "sample"
    base_snapshot = Snapshot(
        id="base",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Base",
        resources=(
            SnapshotResource(
                source="qa_deck.generic",
                resource_type="config",
                identifier="settings",
                schema_version=1,
                state={"value": 1},
            ),
        ),
    )
    target_snapshot = Snapshot(
        id="target",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Target",
        resources=(
            SnapshotResource(
                source="qa_deck.generic",
                resource_type="config",
                identifier="settings",
                schema_version=2,
                state={"value": 1},
            ),
        ),
    )

    diff = SnapshotDiffer().diff(base_snapshot, target_snapshot)

    assert diff.changed_count == 1
    assert diff.entries[0].status == SnapshotDiffStatus.CHANGED
    assert diff.entries[0].base_state["schema_version"] == 1
    assert diff.entries[0].target_state["schema_version"] == 2


def test_snapshot_differ_handles_nested_state_change() -> None:
    from qa_deck.snapshot import SnapshotDiffer

    sample_product_id = "sample"
    base_snapshot = Snapshot(
        id="base",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Base nested",
        resources=(
            SnapshotResource(
                source="qa_deck.generic",
                resource_type="config",
                identifier="settings",
                state={"nested": {"a": 1, "b": 2}},
            ),
        ),
    )
    target_snapshot = Snapshot(
        id="target",
        product_id=sample_product_id,
        created_at=datetime.now(UTC),
        label="Target nested",
        resources=(
            SnapshotResource(
                source="qa_deck.generic",
                resource_type="config",
                identifier="settings",
                state={"nested": {"a": 1, "b": 3}},
            ),
        ),
    )

    diff = SnapshotDiffer().diff(base_snapshot, target_snapshot)

    assert diff.changed_count == 1
    assert diff.entries[0].status == SnapshotDiffStatus.CHANGED
    assert diff.entries[0].base_state["state"]["nested"]["b"] == 2
    assert diff.entries[0].target_state["state"]["nested"]["b"] == 3


def test_compare_different_product_snapshots_returns_404(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response1 = client.post(
        "/products/sample/snapshots",
        data={"label": "First snapshot"},
    )
    assert response1.status_code == 302

    product = products(app).get("other")
    assert product is not None
    other_snapshot = Snapshot(
        id="external",
        product_id=product.id,
        created_at=datetime.now(UTC),
        label="Other product",
        resources=(
            SnapshotResource(
                source="qa_deck.generic",
                resource_type="config",
                identifier="settings",
                state={"value": 2},
            ),
        ),
    )
    app.extensions["snapshot_repository"].add(other_snapshot)

    repository = app.extensions["snapshot_repository"]
    snapshots = repository.list_for_product("sample")
    assert len(snapshots) == 1
    sample_snapshot_id = snapshots[0].id

    compare_response = client.get(
        f"/products/sample/snapshots/compare?base={sample_snapshot_id}&target={other_snapshot.id}"
    )
    assert compare_response.status_code == 404


def test_selecting_saved_snapshot_automatically_compares_with_current(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/products/sample/snapshots", data={"label": "Baseline clean"})
    repository = app.extensions["snapshot_repository"]
    snapshot = repository.list_for_product("sample")[0]
    before_count = len(repository.list_for_product("sample"))

    response = client.get(f"/products/sample?snapshot={snapshot.id}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Baseline clean → Поточний стан" in html
    assert 'id="snapshot-diff"' in html
    assert len(repository.list_for_product("sample")) == before_count


def test_compare_snapshot_with_current_route_renders_diff(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/products/sample/snapshots",
        data={"label": "Initial snapshot"},
    )
    assert response.status_code == 302

    page = client.get("/products/sample").get_data(as_text=True)
    assert "Initial snapshot" in page

    repository = app.extensions["snapshot_repository"]
    snapshots = repository.list_for_product("sample")
    assert len(snapshots) == 1
    before_count = len(snapshots)
    snapshot_id = snapshots[0].id

    compare_url = (
        f"/products/sample/snapshots/{snapshot_id}/compare/current"
    )
    compare_response = client.get(compare_url)
    assert compare_response.status_code == 200
    html = compare_response.get_data(as_text=True)
    assert "Snapshot Diff" in html
    assert "Initial snapshot → Поточний стан" in html
    assert "Порівняти з поточним станом" in html
    assert len(repository.list_for_product("sample")) == before_count


def test_snapshot_diff_ui_groups_statuses_sources_and_changed_fields(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    base = Snapshot(
        "base-internal-id",
        "sample",
        datetime.now(UTC),
        "Baseline",
        (
            SnapshotResource(
                "qa_deck.executable_inspector",
                "executable",
                "primary-executable",
                state={
                    "size_bytes": 20,
                    "settings": {"mode": "prod"},
                },
            ),
            SnapshotResource(
                "license-manager",
                "license",
                "license.dat",
                state={"status": "active"},
            ),
            SnapshotResource(
                "tests.generic",
                "setting",
                "stable-setting",
                state={"value": "same"},
            ),
        ),
    )
    target = Snapshot(
        "target-internal-id",
        "sample",
        datetime.now(UTC),
        "After change",
        (
            SnapshotResource(
                "qa_deck.executable_inspector",
                "executable",
                "primary-executable",
                state={
                    "size_bytes": 28,
                    "settings": {"mode": "debug"},
                },
            ),
            SnapshotResource(
                "log-collector",
                "log-source",
                "logs",
                state={"file_count": 3},
            ),
            SnapshotResource(
                "tests.generic",
                "setting",
                "stable-setting",
                state={"value": "same"},
            ),
        ),
    )
    repository.add(base)
    repository.add(target)

    response = app.test_client().get(
        "/products/sample/snapshots/compare"
        "?base=base-internal-id&target=target-internal-id"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Baseline → After change" in html
    assert html.index("diff-group-changed") < html.index("diff-group-added")
    assert html.index("diff-group-added") < html.index("diff-group-removed")
    assert html.index("diff-group-removed") < html.index("diff-group-unchanged")
    assert 'diff-group-changed" open' not in html
    assert 'diff-group-added" open' not in html
    assert 'diff-group-removed" open' not in html
    assert 'diff-group-unchanged" open' not in html
    assert "Executable Inspector" in html
    assert "Log Collector" in html
    assert "License Manager" in html
    assert "size_bytes" in html
    assert "settings.mode" in html


def test_snapshot_actions_keep_stable_fragment_anchors(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    client = app.test_client()

    first = client.post(
        "/products/sample/snapshots",
        data={"label": "First"},
    )
    second = client.post(
        "/products/sample/snapshots",
        data={"label": "Second"},
    )
    page = client.get("/products/sample").get_data(as_text=True)

    assert first.location.endswith("/products/sample?open=snapshots#snapshots")
    assert second.location.endswith("/products/sample?open=snapshots#snapshots")
    assert 'action="/products/sample/snapshots#snapshots"' in page
    assert "?snapshot=" in page and "#snapshot-diff" in page
    assert 'action="/products/sample/snapshots/compare#snapshot-diff"' in page
    assert 'id="license-manager"' in page
    assert 'id="log-collector"' in page


def test_snapshot_diff_warnings_and_raw_values_are_collapsed_and_escaped(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    unsafe_value = '<script>alert("unsafe")</script>'
    base = Snapshot(
        "escaped-base",
        "sample",
        datetime.now(UTC),
        "Escaped base",
        (
            SnapshotResource(
                "tests.generic",
                "config",
                "item",
                state={"value": unsafe_value},
            ),
        ),
        metadata={
            "warnings": [
                "Base warning",
                "Traceback (most recent call last): private details",
            ]
        },
    )
    target = Snapshot(
        "escaped-target",
        "sample",
        datetime.now(UTC),
        "Escaped target",
        (SnapshotResource("tests.generic", "config", "item", state={"value": "safe"}),),
        metadata={"warnings": ["Target warning"]},
    )
    repository.add(base)
    repository.add(target)

    response = app.test_client().get(
        "/products/sample/snapshots/compare"
        "?base=escaped-base&target=escaped-target"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Попередження (3)" in html
    assert '<details class="snapshot-warning-details">' in html
    assert "Base warning" in html
    assert "Target warning" in html
    assert "Технічні деталі помилки приховано" in html
    assert "Traceback (most recent call last)" not in html
    assert unsafe_value not in html
    assert "&lt;script&gt;" in html
    assert "Технічні деталі" in html


def test_snapshot_domain_enforces_schema_json_identity_and_immutability() -> None:
    mutable_state = {"nested": {"values": [1]}}
    resource = SnapshotResource(
        source=" provider ",
        resource_type=" config ",
        identifier=" primary ",
        state=mutable_state,
    )
    mutable_state["nested"]["values"].append(2)

    snapshot = Snapshot(
        id="snapshot",
        product_id="sample",
        created_at=datetime.now(UTC),
        label=None,
        resources=(resource,),
        metadata={"warnings": ["one"]},
    )

    assert resource.to_dict()["state"] == {"nested": {"values": [1]}}
    assert resource.source == "provider"
    with pytest.raises(TypeError):
        resource.state["changed"] = True
    with pytest.raises(TypeError):
        snapshot.metadata["changed"] = True

    for invalid_version in (True, 0, "1"):
        with pytest.raises(ValueError):
            SnapshotResource(
                "provider",
                "config",
                "item",
                schema_version=invalid_version,
            )
        with pytest.raises(ValueError):
            Snapshot(
                "snapshot",
                "sample",
                datetime.now(UTC),
                None,
                (),
                schema_version=invalid_version,
            )

    with pytest.raises(ValueError):
        SnapshotResource("provider", "config", "item", state={"bad": object()})
    with pytest.raises(ValueError):
        Snapshot(
            "duplicate",
            "sample",
            datetime.now(UTC),
            None,
            (resource, resource),
        )
    with pytest.raises(ValueError):
        SnapshotResource.from_dict(
            {
                "source": 7,
                "resource_type": "config",
                "identifier": "item",
                "state": {},
            }
        )


def test_license_snapshot_provider_handles_non_ready_inspection(
    tmp_path: Path,
) -> None:
    plugin = LicenseManager()
    result = plugin.capture_snapshot(
        Product("sample", "Sample Product"),
        license_configuration(tmp_path / "missing-licenses"),
    )

    assert len(result.resources) == 1
    assert result.resources[0].resource_type == "license-manager"
    assert result.resources[0].state["status"] == "directory_missing"
    assert result.warnings == (
        "License Manager inspection returned directory_missing.",
    )


def test_snapshot_builder_isolates_configuration_capability_and_result_failures(
    tmp_path: Path,
) -> None:
    manager = PluginManager()

    class SelectiveConfigurationRepository(PluginConfigurationRepository):
        def get(self, product_id: str, plugin_identifier: str):
            if plugin_identifier == "tests.configuration_failure":
                raise ValueError("corrupted configuration")
            return super().get(product_id, plugin_identifier)

    class ConfigurationFailureProvider:
        identifier = "tests.configuration_failure"
        display_name = "Configuration failure"
        description = "Test provider"
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(self, product: Product, configuration: object):
            raise AssertionError("Provider must not run after configuration failure")

    class NonCallableProvider:
        identifier = "tests.non_callable"
        display_name = "Non-callable"
        description = "Test provider"
        version = "0.0.1"
        capture_snapshot = "not callable"

        def get_actions(self) -> list[object]:
            return []

    class MalformedProvider:
        identifier = "tests.malformed"
        display_name = "Malformed"
        description = "Test provider"
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(
            self, product: Product, configuration: object
        ) -> SnapshotCaptureResult:
            return SnapshotCaptureResult(resources=("invalid",))

    class HealthyProvider:
        identifier = "tests.healthy"
        display_name = "Healthy"
        description = "Test provider"
        version = "0.0.1"

        def get_actions(self) -> list[object]:
            return []

        def capture_snapshot(
            self, product: Product, configuration: object
        ) -> SnapshotCaptureResult:
            return SnapshotCaptureResult(
                resources=(
                    SnapshotResource(
                        self.identifier,
                        "test",
                        product.id,
                        state={"ok": True},
                    ),
                )
            )

    manager.register(ConfigurationFailureProvider())
    manager.register(NonCallableProvider())
    manager.register(MalformedProvider())
    manager.register(HealthyProvider())
    repository = SelectiveConfigurationRepository(tmp_path / "configurations.json")

    snapshot = SnapshotBuilder(manager, repository).build_snapshot(
        Product("sample", "Sample Product")
    )

    assert [resource.source for resource in snapshot.resources] == ["tests.healthy"]
    assert snapshot.metadata["warnings"] == (
        "Snapshot provider tests.configuration_failure failed.",
        "Snapshot provider tests.non_callable is unavailable.",
        "Snapshot provider tests.malformed failed.",
    )


def test_snapshot_operation_log_failure_does_not_mask_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_app(tmp_path)

    def fail_to_append(operation_log: object) -> None:
        raise OSError("operation log unavailable")

    monkeypatch.setattr(
        app.extensions["operation_log_repository"],
        "append",
        fail_to_append,
    )

    response = app.test_client().post(
        "/products/sample/snapshots",
        data={"label": "Persisted snapshot"},
    )

    assert response.status_code == 302
    snapshots = app.extensions["snapshot_repository"].list_for_product("sample")
    assert len(snapshots) == 1
    assert snapshots[0].label == "Persisted snapshot"


def test_snapshot_differ_rejects_cross_product_and_defensive_duplicates() -> None:
    resource = SnapshotResource("provider", "config", "item", state={"value": 1})
    base = Snapshot(
        "base",
        "sample",
        datetime.now(UTC),
        None,
        (resource,),
    )
    other_product = Snapshot(
        "other",
        "other",
        datetime.now(UTC),
        None,
        (resource,),
    )

    from qa_deck.snapshot import SnapshotDiffer

    differ = SnapshotDiffer()
    with pytest.raises(ValueError, match="different products"):
        differ.diff(base, other_product)

    malformed = Snapshot(
        "malformed",
        "sample",
        datetime.now(UTC),
        None,
        (resource,),
    )
    object.__setattr__(malformed, "resources", (resource, resource))
    with pytest.raises(ValueError, match="duplicate resource identities"):
        differ.diff(malformed, base)


def test_snapshot_differ_treats_bool_and_int_as_different_json_values() -> None:
    from qa_deck.snapshot import SnapshotDiffer

    def make_snapshot(snapshot_id: str, state: dict[str, object]) -> Snapshot:
        return Snapshot(
            snapshot_id,
            "sample",
            datetime.now(UTC),
            None,
            (SnapshotResource("provider", "config", "item", state=state),),
        )

    diff = SnapshotDiffer().diff(
        make_snapshot("base", {"enabled": True, "disabled": False}),
        make_snapshot("target", {"enabled": 1, "disabled": 0}),
    )

    assert diff.changed_count == 1
    assert diff.entries[0].status == SnapshotDiffStatus.CHANGED


def test_executable_snapshot_identity_is_stable_across_path_changes() -> None:
    plugin = ExecutableInspector()
    first = plugin.capture_snapshot(
        Product("sample", "Sample", executable_path="C:/old/app.exe"),
        None,
    ).resources[0]
    second = plugin.capture_snapshot(
        Product("sample", "Sample", executable_path="D:/new/app.exe"),
        None,
    ).resources[0]

    assert first.identifier == second.identifier == "primary-executable"
    assert first.state["original_path"] != second.state["original_path"]


def test_large_snapshot_diff_values_are_truncated_only_in_html(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    large_value = "x" * 5_000
    base = Snapshot(
        "large-base",
        "sample",
        datetime.now(UTC),
        "Large base",
        (SnapshotResource("provider", "config", "item", state={"value": large_value}),),
    )
    target = Snapshot(
        "large-target",
        "sample",
        datetime.now(UTC),
        "Large target",
        (SnapshotResource("provider", "config", "item", state={"value": "changed"}),),
    )
    repository.add(base)
    repository.add(target)

    response = app.test_client().get(
        "/products/sample/snapshots/compare"
        "?base=large-base&target=large-target"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Значення скорочено" in html
    assert large_value not in html
    assert base.resources[0].state["value"] == large_value
