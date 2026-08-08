"""Snapshot domain models for QA Deck."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from types import MappingProxyType
from typing import Self

SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SnapshotResource:
    """Neutral resource representation stored by a snapshot."""

    source: str
    resource_type: str
    identifier: str
    schema_version: int = 1
    state: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("SnapshotResource source must not be empty")
        if not isinstance(self.resource_type, str) or not self.resource_type.strip():
            raise ValueError("SnapshotResource resource_type must not be empty")
        if not isinstance(self.identifier, str) or not self.identifier.strip():
            raise ValueError("SnapshotResource identifier must not be empty")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("SnapshotResource schema_version must be an integer >= 1")
        if not isinstance(self.state, dict):
            raise ValueError("SnapshotResource state must be a JSON object")

        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "resource_type", self.resource_type.strip())
        object.__setattr__(self, "identifier", self.identifier.strip())
        _validate_json_value(self.state, "SnapshotResource state")
        object.__setattr__(self, "state", _freeze_json_object(self.state))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "resource_type": self.resource_type,
            "schema_version": self.schema_version,
            "identifier": self.identifier,
            "state": _thaw_json_object(self.state),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        if not isinstance(data, dict):
            raise ValueError("SnapshotResource must be a mapping")
        source = _required_type(data, "source", str, "SnapshotResource")
        resource_type = _required_type(
            data, "resource_type", str, "SnapshotResource"
        )
        identifier = _required_type(data, "identifier", str, "SnapshotResource")
        schema_version = data.get("schema_version", 1)
        if type(schema_version) is not int:
            raise ValueError("SnapshotResource schema_version must be an integer >= 1")
        state = data.get("state", {})
        if not isinstance(state, dict):
            raise ValueError("SnapshotResource state must be a JSON object")
        return cls(
            source=source,
            resource_type=resource_type,
            schema_version=schema_version,
            identifier=identifier,
            state=state,
        )


@dataclass(frozen=True, slots=True)
class SnapshotCaptureResult:
    resources: tuple[SnapshotResource, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Immutable snapshot of Product state and associated read-only resources."""

    id: str
    product_id: str
    created_at: datetime
    label: str | None
    resources: tuple[SnapshotResource, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.id, str):
            raise ValueError("Snapshot id must be a string")
        if not isinstance(self.product_id, str):
            raise ValueError("Snapshot product_id must be a string")
        normalized_id = self.id.strip()
        normalized_product_id = self.product_id.strip()
        if not normalized_id:
            raise ValueError("Snapshot id must not be empty")
        if not normalized_product_id:
            raise ValueError("Snapshot product_id must not be empty")
        object.__setattr__(self, "id", normalized_id)
        object.__setattr__(self, "product_id", normalized_product_id)
        if self.label is not None:
            if not isinstance(self.label, str):
                raise ValueError("Snapshot label must be a string or null")
            normalized_label = self.label.strip()
            if not normalized_label:
                normalized_label = None
            if normalized_label is not None and len(normalized_label) > 100:
                raise ValueError("Snapshot label must be 100 characters or less")
            object.__setattr__(self, "label", normalized_label)
        if not isinstance(self.created_at, datetime):
            raise ValueError("Snapshot created_at must be a datetime")
        if self.created_at.tzinfo is None:
            object.__setattr__(
                self,
                "created_at",
                self.created_at.replace(tzinfo=UTC),
            )
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("Snapshot schema_version must be an integer >= 1")
        if not isinstance(self.resources, (list, tuple)):
            raise ValueError("Snapshot resources must be a list or tuple")
        resources = tuple(self.resources)
        if not all(isinstance(resource, SnapshotResource) for resource in resources):
            raise ValueError("Snapshot resources must contain SnapshotResource values")
        identities = [
            (resource.source, resource.resource_type, resource.identifier)
            for resource in resources
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Snapshot resource identities must be unique")
        if not isinstance(self.metadata, dict):
            raise ValueError("Snapshot metadata must be a JSON object")
        _validate_json_value(self.metadata, "Snapshot metadata")
        object.__setattr__(self, "resources", resources)
        object.__setattr__(self, "metadata", _freeze_json_object(self.metadata))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "created_at": self.created_at.isoformat(),
            "label": self.label,
            "resources": [resource.to_dict() for resource in self.resources],
            "metadata": _thaw_json_object(self.metadata),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        if not isinstance(data, dict):
            raise ValueError("Snapshot must be a mapping")
        snapshot_id = _required_type(data, "id", str, "Snapshot")
        product_id = _required_type(data, "product_id", str, "Snapshot")
        created_at_value = _required_type(data, "created_at", str, "Snapshot")
        try:
            created_at = datetime.fromisoformat(created_at_value)
        except ValueError as error:
            raise ValueError("Snapshot created_at must be an ISO datetime") from error
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        label = data.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError("Snapshot label must be a string or null")
        resources_data = data.get("resources", [])
        if not isinstance(resources_data, list):
            raise ValueError("Snapshot resources must be a JSON array")
        resources = tuple(
            SnapshotResource.from_dict(item) for item in resources_data
        )
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("Snapshot metadata must be a JSON object")
        schema_version = data.get("schema_version", SNAPSHOT_SCHEMA_VERSION)
        if type(schema_version) is not int:
            raise ValueError("Snapshot schema_version must be an integer >= 1")
        return cls(
            id=snapshot_id,
            product_id=product_id,
            created_at=created_at,
            label=label,
            resources=resources,
            metadata=metadata,
            schema_version=schema_version,
        )


def _required_type(
    data: dict[str, object],
    key: str,
    expected_type: type,
    model_name: str,
):
    if key not in data or not isinstance(data[key], expected_type):
        raise ValueError(f"{model_name} {key} has an invalid type")
    return data[key]


def _validate_json_value(value: object, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} must be JSON-compatible")


def _freeze_json_object(value: dict[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {key: _freeze_json_value(item) for key, item in value.items()}
    )


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_json_object(value)
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_object(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _thaw_json_value(item) for key, item in value.items()}


def _thaw_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _thaw_json_object(value)
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value
