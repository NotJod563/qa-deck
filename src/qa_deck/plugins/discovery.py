"""Predictable discovery of plugins bundled with QA Deck."""

from collections.abc import Iterable

from qa_deck.plugins.builtin import (
    create_executable_inspector,
    create_license_manager,
    create_log_collector,
    create_windows_registry,
)
from qa_deck.plugins.manager import PluginFactory, PluginManager

# Built-in plugin factories are added here as official plugins are implemented.
BUILTIN_PLUGIN_FACTORIES: tuple[PluginFactory, ...] = (
    create_executable_inspector,
    create_license_manager,
    create_log_collector,
    create_windows_registry,
)


def discover_builtin_plugins(
    manager: PluginManager,
    factories: Iterable[PluginFactory] | None = None,
) -> None:
    """Load explicitly registered built-in plugin factories."""
    manager.load_all(
        BUILTIN_PLUGIN_FACTORIES if factories is None else factories
    )
