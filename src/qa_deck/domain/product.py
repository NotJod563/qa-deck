"""Product domain model."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self, cast


def _normalize_path(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {'"', "'"}
    ):
        return normalized[1:-1]

    return normalized


@dataclass
class Product:
    """A software product or system tested with QA Deck."""

    id: str
    name: str
    description: str = ""
    executable_path: str | None = None
    working_directory: str | None = None
    launch_arguments: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Normalize and validate required fields."""
        self.id = self.id.strip()
        self.name = self.name.strip()
        self.executable_path = _normalize_path(self.executable_path)
        self.working_directory = _normalize_path(self.working_directory)

        if not self.id:
            raise ValueError("Product id must not be empty")
        if not self.name:
            raise ValueError("Product name must not be empty")

    def to_dict(self) -> dict[str, object]:
        """Return a serializable representation of the product."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "executable_path": self.executable_path,
            "working_directory": self.working_directory,
            "launch_arguments": self.launch_arguments.copy(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        """Create a product from its dictionary representation."""
        return cls(
            id=cast(str, data["id"]),
            name=cast(str, data["name"]),
            description=cast(str, data.get("description", "")),
            executable_path=cast(str | None, data.get("executable_path")),
            working_directory=cast(str | None, data.get("working_directory")),
            launch_arguments=list(
                cast(list[str], data.get("launch_arguments", []))
            ),
        )
