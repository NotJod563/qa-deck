"""Registration and safe loading of QA Deck plugins."""

from collections.abc import Callable, Iterable

from qa_deck.plugins.api import Plugin

PluginFactory = Callable[[], Plugin]


class PluginManager:
    """Keep registered plugins and isolate plugin loading failures."""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._load_errors: list[str] = []

    @property
    def load_errors(self) -> tuple[str, ...]:
        """Return errors collected while loading plugins."""
        return tuple(self._load_errors)

    def register(self, plugin: Plugin) -> None:
        """Register one plugin by its unique identifier."""
        if not plugin.identifier.strip():
            raise ValueError("Plugin identifier must not be empty")
        if plugin.identifier in self._plugins:
            raise ValueError(
                f"Plugin with identifier '{plugin.identifier}' is already registered"
            )

        self._plugins[plugin.identifier] = plugin

    def list_all(self) -> list[Plugin]:
        """Return all registered plugins in registration order."""
        return list(self._plugins.values())

    def get(self, identifier: str) -> Plugin | None:
        """Return a plugin by its exact identifier."""
        return self._plugins.get(identifier)

    def load(self, factory: PluginFactory) -> bool:
        """Load one plugin without propagating its failure."""
        try:
            self.register(factory())
        except Exception as error:
            factory_name = getattr(factory, "__name__", factory.__class__.__name__)
            self._load_errors.append(f"{factory_name}: {error}")
            return False

        return True

    def load_all(self, factories: Iterable[PluginFactory]) -> None:
        """Load each plugin factory, continuing after failures."""
        for factory in factories:
            self.load(factory)
