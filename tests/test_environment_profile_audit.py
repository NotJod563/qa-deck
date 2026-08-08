"""Focused tests for Week 3 Profile provider and authority boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from qa_deck.domain import EnvironmentProfile, Product, Snapshot, SnapshotResource
from qa_deck.environment_profiles import (
    EnvironmentProfileComparator,
    EnvironmentProfileExecutionPlanner,
    EnvironmentProfileExecutionStateStore,
    EnvironmentProfileExecutor,
)
from qa_deck.plugins import PluginManager
from qa_deck.plugins.api import (
    EnvironmentProfileComparisonEntry,
    EnvironmentProfileComparisonSection,
    EnvironmentProfileComparisonStatus,
    EnvironmentProfileExecutionEntry,
    EnvironmentProfileExecutionPreparation,
    EnvironmentProfileExecutionStatus,
    EnvironmentProfileProviderExecution,
)
from qa_deck.plugins.builtin.windows_registry import (
    RegistryExecutionIntent,
    WindowsRegistry,
)
from qa_deck.snapshot import (
    SnapshotBuilder,
    SnapshotDiffer,
    SnapshotRestorePlanner,
)
from qa_deck.storage import (
    EnvironmentProfileRepository,
    OperationLogRepository,
    PluginConfigurationRepository,
)
from tests.helpers import environment_profiles, make_app
from tests.plugins.test_registry_execution import (
    FakeWriter,
    MutableReader,
    registry_configuration,
)


def _profile() -> EnvironmentProfile:
    return EnvironmentProfile("qa", "sample", "QA", "qa")


class _FakeProfileProvider:
    display_name = "Healthy provider"

    def __init__(self, identifier: str = "healthy") -> None:
        self.identifier = identifier
        self.authority = "authority-a"
        self.inspection_calls = 0
        self.execution_calls = 0

    def uses_environment_profile(self, profile: EnvironmentProfile) -> bool:
        del profile
        return True

    def inspect_environment_profile_current(
        self, product: Product, configuration: object
    ) -> object:
        del product, configuration
        self.inspection_calls += 1
        return {"authority": self.authority, "current": "A"}

    def compare_environment_profile(
        self,
        profile: EnvironmentProfile,
        configuration: object,
        current: object,
    ) -> EnvironmentProfileComparisonSection:
        del profile, configuration
        assert isinstance(current, dict)
        return EnvironmentProfileComparisonSection(
            self.identifier,
            self.display_name,
            None,
            (
                EnvironmentProfileComparisonEntry(
                    "resource",
                    "Resource",
                    str(current["current"]),
                    "B",
                    EnvironmentProfileComparisonStatus.CHANGE,
                    "Change required.",
                ),
            ),
        )

    def prepare_environment_profile_execution(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: object,
        current: object,
    ) -> EnvironmentProfileExecutionPreparation:
        section = self.compare_environment_profile(
            profile, configuration, current
        )
        assert isinstance(current, dict)
        return EnvironmentProfileExecutionPreparation(
            self.identifier,
            self.display_name,
            None,
            section.entries,
            str(current["authority"]),
        )

    def execute_environment_profile(
        self,
        profile: EnvironmentProfile,
        product: Product,
        configuration: object,
        expected: EnvironmentProfileExecutionPreparation,
        **context: object,
    ) -> EnvironmentProfileProviderExecution:
        del profile, product, configuration, context
        self.execution_calls += 1
        entry = expected.entries[0]
        return EnvironmentProfileProviderExecution(
            (
                EnvironmentProfileExecutionEntry(
                    entry.resource_id,
                    entry.display_name,
                    entry.current_state,
                    entry.desired_state,
                    EnvironmentProfileExecutionStatus.SUCCESS,
                    "Applied.",
                    changed_count=1,
                ),
            )
        )


class _BrokenParticipationProvider(_FakeProfileProvider):
    display_name = "Broken participation"

    def uses_environment_profile(self, profile: EnvironmentProfile) -> bool:
        del profile
        raise OSError("participation failed")


class _BrokenCapabilityProvider(_FakeProfileProvider):
    display_name = "Broken capability"

    def __getattribute__(self, name: str) -> object:
        if name == "uses_environment_profile":
            raise OSError("capability lookup failed")
        return super().__getattribute__(name)


def test_profile_capability_and_participation_failures_are_isolated(
    tmp_path: Path,
) -> None:
    manager = PluginManager()
    manager.register(_BrokenParticipationProvider("broken-uses"))
    manager.register(_BrokenCapabilityProvider("broken-capability"))
    healthy = _FakeProfileProvider()
    manager.register(healthy)
    configurations = PluginConfigurationRepository(tmp_path / "config.json")
    product = Product("sample", "Sample")
    profile = _profile()

    comparison = EnvironmentProfileComparator(manager, configurations).compare_all(
        product, [profile]
    )[0]
    plan = EnvironmentProfileExecutionPlanner(manager, configurations).prepare(
        product, profile
    )

    comparison_statuses = [
        entry.status
        for section in comparison.sections
        for entry in section.entries
    ]
    plan_statuses = [
        entry.status for section in plan.sections for entry in section.entries
    ]
    assert comparison_statuses.count(EnvironmentProfileComparisonStatus.ERROR) == 2
    assert EnvironmentProfileComparisonStatus.CHANGE in comparison_statuses
    assert plan_statuses.count(EnvironmentProfileComparisonStatus.ERROR) == 2
    assert EnvironmentProfileComparisonStatus.CHANGE in plan_statuses
    assert healthy.inspection_calls == 2

    app = make_app(tmp_path / "web")
    web_manager = app.extensions["plugin_manager"]
    assert isinstance(web_manager, PluginManager)
    web_manager.register(_BrokenParticipationProvider("web-broken-uses"))
    web_manager.register(_BrokenCapabilityProvider("web-broken-capability"))
    web_manager.register(_FakeProfileProvider("web-healthy"))
    environment_profiles(app).add(profile)

    response = app.test_client().get("/products/sample")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Healthy provider" in html
    assert "Не вдалося перевірити поточний стан." in html


def test_generic_authority_contract_allows_unchanged_and_blocks_drift(
    tmp_path: Path,
) -> None:
    manager = PluginManager()
    provider = _FakeProfileProvider("third-party")
    manager.register(provider)
    configurations = PluginConfigurationRepository(tmp_path / "config.json")
    profiles = EnvironmentProfileRepository(tmp_path / "profiles.json")
    logs = OperationLogRepository(tmp_path / "logs.json")
    profile = _profile()
    profiles.add(profile)
    product = Product("sample", "Sample")
    planner = EnvironmentProfileExecutionPlanner(manager, configurations)
    store = EnvironmentProfileExecutionStateStore()
    executor = EnvironmentProfileExecutor(
        planner,
        manager,
        configurations,
        profiles,
        tmp_path / "backups",
        logs,
    )

    first = store.create_intent(profile, planner.prepare(product, profile))
    success = executor.execute(product, first)
    second = store.create_intent(profile, planner.prepare(product, profile))
    provider.authority = "authority-b"
    stale = executor.execute(product, second)

    assert success.succeeded_count == 1
    assert stale.blocked_count == 1
    assert provider.execution_calls == 1


def test_registry_profile_preview_uses_one_current_inspection(
    tmp_path: Path,
) -> None:
    reader = MutableReader("REG_DWORD", 1)
    plugin = WindowsRegistry(reader, FakeWriter(reader))
    manager = PluginManager()
    manager.register(plugin)
    configurations = PluginConfigurationRepository(tmp_path / "config.json")
    configurations.upsert(registry_configuration(plugin, desired_value=99))

    plan = EnvironmentProfileExecutionPlanner(manager, configurations).prepare(
        Product("sample", "Sample"), _profile()
    )

    context = plan.sections[0].provider_context
    assert isinstance(context, RegistryExecutionIntent)
    assert reader.value_reads == 1
    assert plan.sections[0].entries[0].current_state == "REG_DWORD: 1"
    assert context.entries[0].current_state.value == 1


def test_snapshot_restore_preview_uses_captured_current_without_reread(
    tmp_path: Path,
) -> None:
    reader = MutableReader("REG_DWORD", 1)
    plugin = WindowsRegistry(reader, FakeWriter(reader))
    manager = PluginManager()
    manager.register(plugin)
    configurations = PluginConfigurationRepository(tmp_path / "config.json")
    configurations.upsert(registry_configuration(plugin, desired_value=99))
    product = Product("sample", "Sample")
    desired = Snapshot(
        "snapshot",
        product.id,
        datetime.now(UTC),
        None,
        resources=(
            SnapshotResource(
                plugin.identifier,
                "registry-value",
                "mode",
                state={
                    "hive": "HKCU",
                    "key_path": "Software\\Example",
                    "value_name": "Mode",
                    "exists": True,
                    "registry_type": "REG_DWORD",
                    "value": 99,
                    "status": "available",
                },
            ),
        ),
    )
    planner = SnapshotRestorePlanner(
        SnapshotBuilder(manager, configurations),
        SnapshotDiffer(),
        manager,
        configurations,
    )

    plan = planner.prepare(product, desired)

    assert plan.ready_count == 1
    assert reader.value_reads == 1
