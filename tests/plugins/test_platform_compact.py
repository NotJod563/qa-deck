"""Minimal plugin platform contract."""

from pathlib import Path

import pytest

from qa_deck.plugins import PluginManager, RiskLevel
from qa_deck.plugins.builtin import ExecutableInspector
from qa_deck.plugins.discovery import discover_builtin_plugins
from tests.helpers import make_app


def test_builtin_plugins_load_with_expected_actions(tmp_path: Path) -> None:
    manager = make_app(tmp_path).extensions["plugin_manager"]
    plugins = {plugin.identifier: plugin for plugin in manager.list_all()}

    assert set(plugins) == {
        "qa_deck.executable_inspector",
        "license-manager",
        "log-collector",
    }
    assert {
        action.identifier for action in plugins["license-manager"].get_actions()
    } == {
        "inspect-licenses",
        "hide-licenses",
        "restore-licenses",
        "inspect-license-backup",
    }
    log_actions = {
        action.identifier: action
        for action in plugins["log-collector"].get_actions()
    }
    assert log_actions["collect-logs"].risk_level is RiskLevel.SAFE


def test_duplicate_plugin_identifier_is_rejected() -> None:
    manager = PluginManager()
    manager.register(ExecutableInspector())

    with pytest.raises(ValueError):
        manager.register(ExecutableInspector())


def test_broken_plugin_factory_does_not_block_discovery() -> None:
    manager = PluginManager()

    def broken_factory():  # noqa: ANN202
        raise RuntimeError("broken plugin")

    discover_builtin_plugins(
        manager,
        factories=[broken_factory, ExecutableInspector],
    )

    assert manager.get("qa_deck.executable_inspector") is not None
    assert len(manager.load_errors) == 1
