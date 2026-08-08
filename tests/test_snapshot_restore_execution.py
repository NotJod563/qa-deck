"""Snapshot Restore execution and confirmation safety tests."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from flask import Flask

from qa_deck.domain import Product, Snapshot, SnapshotResource
from qa_deck.plugins import PluginManager, RiskLevel
from qa_deck.plugins.api import (
    SnapshotRestoreExecution,
    SnapshotRestoreExecutionStatus,
    SnapshotRestorePreparation,
)
from qa_deck.plugins.builtin import ExecutableInspector
from qa_deck.snapshot import (
    SnapshotBuilder,
    SnapshotDiffer,
    SnapshotRestoreExecutor,
    SnapshotRestorePlanner,
    SnapshotRestoreStateStore,
)
from qa_deck.storage import (
    OperationLogRepository,
    PluginConfigurationRepository,
    SnapshotRepository,
)
from tests.helpers import (
    configurations,
    license_configuration,
    make_app,
    operation_logs,
    products,
)


def _snapshot_repository(app: Flask) -> SnapshotRepository:
    return cast(SnapshotRepository, app.extensions["snapshot_repository"])


def _capture_snapshot(app: Flask, label: str = "Baseline") -> Snapshot:
    product = products(app).get("sample")
    assert product is not None
    snapshot = SnapshotBuilder(
        cast(PluginManager, app.extensions["plugin_manager"]),
        configurations(app),
    ).build_snapshot(product, label=label)
    _snapshot_repository(app).add(snapshot)
    return snapshot


def _confirmation_token(html: str) -> str:
    match = re.search(r'name="confirmation_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _prepare_restore(client: object, snapshot_id: str) -> tuple[str, str]:
    response = client.get(  # type: ignore[attr-defined]
        f"/products/sample/snapshots/{snapshot_id}/restore-plan"
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    return _confirmation_token(html), html


def _post_restore(
    client: object,
    snapshot_id: str,
    token: str,
    **untrusted_fields: str,
) -> object:
    return client.post(  # type: ignore[attr-defined]
        f"/products/sample/snapshots/{snapshot_id}/restore",
        data={
            "confirmation_token": token,
            "confirm": "yes",
            **untrusted_fields,
        },
        follow_redirects=False,
    )


def _configure_licenses(
    app: Flask,
    directory: Path,
    filenames: list[str],
) -> None:
    configurations(app).upsert(
        license_configuration(directory, filenames)
    )


def test_mixed_license_restore_uses_scoped_existing_operations_and_backup(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    (directory / "license.dat").write_text("license", encoding="utf-8")
    (directory / "trial.lic.hidden").write_text("trial", encoding="utf-8")
    _configure_licenses(app, directory, ["license.dat", "trial.lic"])
    snapshot = _capture_snapshot(app)

    (directory / "license.dat").rename(directory / "license.dat.hidden")
    (directory / "trial.lic.hidden").rename(directory / "trial.lic")

    with app.test_client() as client:
        token, plan_html = _prepare_restore(client, snapshot.id)
        unconfirmed = client.post(
            f"/products/sample/snapshots/{snapshot.id}/restore",
            data={"confirmation_token": token},
        )
        assert unconfirmed.status_code == 400
        assert (directory / "license.dat.hidden").exists()
        assert (directory / "trial.lic").exists()
        response = _post_restore(
            client,
            snapshot.id,
            token,
            path=str(tmp_path / "untrusted"),
            desired_state='{"status":"missing"}',
            fingerprint="browser-replacement",
        )
        assert response.status_code == 302
        assert response.location.endswith("#snapshot-restore-result")
        result_html = client.get(response.location).get_data(as_text=True)

    assert "hidden -&gt; active" in plan_html
    assert "active -&gt; hidden" in plan_html
    assert 'name="confirm" value="yes" required' in plan_html
    assert "Відновити snapshot" in plan_html
    assert (directory / "license.dat").read_text(encoding="utf-8") == "license"
    assert (directory / "trial.lic.hidden").read_text(encoding="utf-8") == "trial"
    assert not (directory / "license.dat.hidden").exists()
    assert not (directory / "trial.lic").exists()
    assert "РЕЗУЛЬТАТ ВІДНОВЛЕННЯ" in result_html
    assert result_html.count("Успішно") >= 2
    assert (tmp_path / "backups").exists()
    aggregate_logs = [
        item
        for item in operation_logs(app).list_for_product("sample")
        if item.action_identifier == "restore-snapshot"
    ]
    assert len(aggregate_logs) == 1
    assert aggregate_logs[0].changed_count == 2


def test_restore_revalidates_server_side_and_blocks_stale_state(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    active = directory / "license.dat"
    hidden = directory / "license.dat.hidden"
    active.write_text("license", encoding="utf-8")
    _configure_licenses(app, directory, ["license.dat"])
    snapshot = _capture_snapshot(app)
    active.rename(hidden)

    with app.test_client() as client:
        token, _ = _prepare_restore(client, snapshot.id)
        hidden.rename(active)
        response = _post_restore(client, snapshot.id, token)
        result_html = client.get(response.location).get_data(as_text=True)

    assert active.exists()
    assert not hidden.exists()
    assert "Current state changed after the Restore Plan was prepared." in result_html
    assert not (tmp_path / "backups").exists()


def test_confirmation_token_is_one_time_and_result_refresh_is_read_only(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    hidden = directory / "license.dat.hidden"
    hidden.write_text("license", encoding="utf-8")
    _configure_licenses(app, directory, ["license.dat"])
    snapshot = _capture_snapshot(app)
    hidden.rename(directory / "license.dat")

    with app.test_client() as client:
        token, _ = _prepare_restore(client, snapshot.id)
        first = _post_restore(client, snapshot.id, token)
        first_result = client.get(first.location)
        backup_state = sorted(
            path.relative_to(tmp_path / "backups")
            for path in (tmp_path / "backups").rglob("*")
        )
        refreshed = client.get(first.location)
        duplicate = _post_restore(client, snapshot.id, token)
        duplicate_html = client.get(duplicate.location).get_data(as_text=True)

    assert first_result.status_code == 200
    assert refreshed.status_code == 200
    assert sorted(
        path.relative_to(tmp_path / "backups")
        for path in (tmp_path / "backups").rglob("*")
    ) == backup_state
    assert "already used" in duplicate_html
    assert (directory / "license.dat.hidden").exists()
    assert not (directory / "license.dat").exists()


def test_successful_restore_makes_next_plan_no_change(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    active = directory / "license.dat"
    hidden = directory / "license.dat.hidden"
    active.write_text("license", encoding="utf-8")
    _configure_licenses(app, directory, ["license.dat"])
    snapshot = _capture_snapshot(app)
    active.rename(hidden)

    with app.test_client() as client:
        token, _ = _prepare_restore(client, snapshot.id)
        response = _post_restore(client, snapshot.id, token)
        assert response.status_code == 302
        next_plan = client.get(
            f"/products/sample/snapshots/{snapshot.id}/restore-plan"
        ).get_data(as_text=True)

    assert 'name="confirmation_token"' not in next_plan
    assert "Без змін" in next_plan


def test_operation_log_failure_does_not_mask_license_restore(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    app = make_app(tmp_path)
    directory = tmp_path / "licenses"
    directory.mkdir()
    active = directory / "license.dat"
    hidden = directory / "license.dat.hidden"
    active.write_text("license", encoding="utf-8")
    _configure_licenses(app, directory, ["license.dat"])
    snapshot = _capture_snapshot(app)
    active.rename(hidden)

    def fail_log_append(operation_log: object) -> None:
        raise OSError("log unavailable")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        operation_logs(app),
        "append",
        fail_log_append,
    )
    with app.test_client() as client:
        token, _ = _prepare_restore(client, snapshot.id)
        response = _post_restore(client, snapshot.id, token)
        result_html = client.get(response.location).get_data(as_text=True)

    assert active.exists()
    assert not hidden.exists()
    assert "operation log was not saved" in result_html


class _FixedBuilder:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def build_snapshot(
        self,
        product: Product,
        label: str | None = None,
    ) -> Snapshot:
        return self.snapshot


class _ExecutionProvider:
    identifier = "tests.execution"
    display_name = "Execution provider"
    description = "Test execution isolation"
    version = "1.0"

    def get_actions(self) -> list[object]:
        return []

    def can_restore(self, resource: SnapshotResource) -> bool:
        return True

    def prepare_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: object,
    ) -> SnapshotRestorePreparation:
        identifier = (snapshot_resource or current_resource).identifier  # type: ignore[union-attr]
        return SnapshotRestorePreparation(
            action_description=f"Restore {identifier}",
            risk_level=RiskLevel.CAUTION,
            fingerprint=f"fingerprint-{identifier}",
        )

    def execute_restore(
        self,
        product: Product,
        snapshot_resource: SnapshotResource | None,
        current_resource: SnapshotResource | None,
        configuration: object,
        **context: object,
    ) -> SnapshotRestoreExecution:
        assert snapshot_resource is not None
        if snapshot_resource.identifier == "fails":
            raise RuntimeError("private traceback detail")
        return SnapshotRestoreExecution(
            SnapshotRestoreExecutionStatus.SUCCESS,
            "Restore succeeded.",
            changed_count=1,
        )


def _generic_snapshot(snapshot_id: str, *resources: SnapshotResource) -> Snapshot:
    return Snapshot(
        snapshot_id,
        "sample",
        datetime(2026, 8, 9, tzinfo=UTC),
        "Generic",
        resources,
    )


def test_execution_failure_is_isolated_and_hides_raw_exception(
    tmp_path: Path,
) -> None:
    provider = _ExecutionProvider()
    manager = PluginManager()
    manager.register(provider)
    current = _generic_snapshot(
        "current",
        SnapshotResource(provider.identifier, "test", "fails", state={"v": 1}),
        SnapshotResource(provider.identifier, "test", "works", state={"v": 1}),
    )
    desired = _generic_snapshot(
        "desired",
        SnapshotResource(provider.identifier, "test", "fails", state={"v": 2}),
        SnapshotResource(provider.identifier, "test", "works", state={"v": 2}),
    )
    configurations_repository = PluginConfigurationRepository(
        tmp_path / "configurations.json"
    )
    planner = SnapshotRestorePlanner(
        _FixedBuilder(current),  # type: ignore[arg-type]
        SnapshotDiffer(),
        manager,
        configurations_repository,
    )
    plan = planner.prepare(Product("sample", "Sample"), desired)
    intent = SnapshotRestoreStateStore().create_intent(plan)
    result = SnapshotRestoreExecutor(
        planner,
        manager,
        configurations_repository,
        tmp_path / "backups",
        OperationLogRepository(tmp_path / "operations.json"),
    ).execute(Product("sample", "Sample"), desired, intent)

    statuses = {entry.identifier: entry.status for entry in result.entries}
    assert statuses == {
        "fails": SnapshotRestoreExecutionStatus.FAILED,
        "works": SnapshotRestoreExecutionStatus.SUCCESS,
    }
    assert all(
        "private traceback detail" not in entry.message
        for entry in result.entries
    )


def test_unsupported_and_no_change_entries_are_never_executed(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "sample.exe"
    executable.write_text("untouched", encoding="utf-8")
    plugin = ExecutableInspector()
    manager = PluginManager()
    manager.register(plugin)
    unchanged = SnapshotResource(
        plugin.identifier,
        "executable",
        "same",
        state={"path": str(executable), "size": 9},
    )
    current = _generic_snapshot(
        "current",
        unchanged,
        SnapshotResource(
            plugin.identifier,
            "executable",
            "primary-executable",
            state={"path": str(executable), "size": 9},
        ),
    )
    desired = _generic_snapshot(
        "desired",
        SnapshotResource(
            plugin.identifier,
            "executable",
            "same",
            state={"path": str(executable), "size": 9},
        ),
        SnapshotResource(
            plugin.identifier,
            "executable",
            "primary-executable",
            state={"path": str(executable), "size": 10},
        ),
    )
    configuration_repository = PluginConfigurationRepository(
        tmp_path / "configurations.json"
    )
    planner = SnapshotRestorePlanner(
        _FixedBuilder(current),  # type: ignore[arg-type]
        SnapshotDiffer(),
        manager,
        configuration_repository,
    )
    plan = planner.prepare(Product("sample", "Sample"), desired)
    intent = SnapshotRestoreStateStore().create_intent(plan)
    result = SnapshotRestoreExecutor(
        planner,
        manager,
        configuration_repository,
        tmp_path / "backups",
        OperationLogRepository(tmp_path / "operations.json"),
    ).execute(Product("sample", "Sample"), desired, intent)

    statuses = {entry.identifier: entry.status for entry in result.entries}
    assert statuses == {
        "primary-executable": SnapshotRestoreExecutionStatus.UNSUPPORTED,
        "same": SnapshotRestoreExecutionStatus.SKIPPED,
    }
    assert executable.read_text(encoding="utf-8") == "untouched"
    assert not (tmp_path / "backups").exists()
