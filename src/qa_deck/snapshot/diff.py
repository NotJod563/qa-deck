"""Snapshot diff support for QA Deck."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from qa_deck.domain.snapshot import Snapshot, SnapshotResource


class SnapshotDiffStatus(StrEnum):
    """Categorize how a resource changed between snapshots."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class SnapshotFieldChange:
    path: str
    base_value: object | None
    target_value: object | None
    base_present: bool = True
    target_present: bool = True


@dataclass(frozen=True, slots=True)
class SnapshotDiffEntry:
    source: str
    resource_type: str
    identifier: str
    status: SnapshotDiffStatus
    base_state: dict[str, object] | None = None
    target_state: dict[str, object] | None = None
    field_changes: tuple[SnapshotFieldChange, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    base_snapshot_id: str
    target_snapshot_id: str
    base_label: str | None
    target_label: str | None
    base_metadata: dict[str, object]
    target_metadata: dict[str, object]
    entries: tuple[SnapshotDiffEntry, ...] = field(default_factory=tuple)

    @property
    def added_count(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.status == SnapshotDiffStatus.ADDED
        )

    @property
    def removed_count(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.status == SnapshotDiffStatus.REMOVED
        )

    @property
    def changed_count(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.status == SnapshotDiffStatus.CHANGED
        )

    @property
    def unchanged_count(self) -> int:
        return sum(
            1
            for entry in self.entries
            if entry.status == SnapshotDiffStatus.UNCHANGED
        )


class SnapshotDiffer:
    """Compare two snapshots and produce a diff report."""

    def diff(self, base_snapshot: Snapshot, target_snapshot: Snapshot) -> SnapshotDiff:
        if base_snapshot.product_id != target_snapshot.product_id:
            raise ValueError("Snapshots from different products cannot be compared")

        base_map = self._resource_map(base_snapshot)
        target_map = self._resource_map(target_snapshot)

        all_keys = sorted(set(base_map) | set(target_map))
        entries: list[SnapshotDiffEntry] = []

        for key in all_keys:
            source, resource_type, identifier = key
            base_resource = base_map.get(key)
            target_resource = target_map.get(key)
            field_changes: tuple[SnapshotFieldChange, ...] = ()

            if base_resource is None:
                status = SnapshotDiffStatus.ADDED
                base_state = None
                target_state = self._resource_snapshot(target_resource)
            elif target_resource is None:
                status = SnapshotDiffStatus.REMOVED
                base_state = self._resource_snapshot(base_resource)
                target_state = None
            else:
                base_state = self._resource_snapshot(base_resource)
                target_state = self._resource_snapshot(target_resource)
                if (
                    base_resource.schema_version != target_resource.schema_version
                    or not self._json_values_equal(
                        base_resource.state,
                        target_resource.state,
                    )
                ):
                    status = SnapshotDiffStatus.CHANGED
                    field_changes = self._field_changes(
                        base_resource,
                        target_resource,
                    )
                else:
                    status = SnapshotDiffStatus.UNCHANGED

            entries.append(
                SnapshotDiffEntry(
                    source=source,
                    resource_type=resource_type,
                    identifier=identifier,
                    status=status,
                    base_state=base_state,
                    target_state=target_state,
                    field_changes=field_changes,
                )
            )

        return SnapshotDiff(
            base_snapshot_id=base_snapshot.id,
            target_snapshot_id=target_snapshot.id,
            base_label=base_snapshot.label,
            target_label=target_snapshot.label,
            base_metadata=self._snapshot_metadata(base_snapshot),
            target_metadata=self._snapshot_metadata(target_snapshot),
            entries=tuple(entries),
        )

    @staticmethod
    def _resource_snapshot(resource: SnapshotResource) -> dict[str, object]:
        return resource.to_dict()

    @staticmethod
    def _resource_key(resource: SnapshotResource) -> tuple[str, str, str]:
        return (resource.source, resource.resource_type, resource.identifier)

    @classmethod
    def _resource_map(
        cls,
        snapshot: Snapshot,
    ) -> dict[tuple[str, str, str], SnapshotResource]:
        resources: dict[tuple[str, str, str], SnapshotResource] = {}
        for resource in snapshot.resources:
            if not isinstance(resource, SnapshotResource):
                raise ValueError("Snapshot contains an invalid resource")
            key = cls._resource_key(resource)
            if key in resources:
                raise ValueError("Snapshot contains duplicate resource identities")
            resources[key] = resource
        return resources

    @staticmethod
    def _snapshot_metadata(snapshot: Snapshot) -> dict[str, object]:
        metadata = snapshot.to_dict()["metadata"]
        if not isinstance(metadata, dict):  # pragma: no cover
            raise ValueError("Snapshot metadata is invalid")
        return metadata

    @classmethod
    def _json_values_equal(cls, left: object, right: object) -> bool:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            if set(left) != set(right):
                return False
            return all(cls._json_values_equal(left[key], right[key]) for key in left)
        if cls._is_json_sequence(left) and cls._is_json_sequence(right):
            if len(left) != len(right):
                return False
            return all(
                cls._json_values_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        if type(left) is not type(right):
            return False
        return left == right

    @staticmethod
    def _is_json_sequence(value: object) -> bool:
        return isinstance(value, Sequence) and not isinstance(value, str)

    @classmethod
    def _field_changes(
        cls,
        base_resource: SnapshotResource,
        target_resource: SnapshotResource,
    ) -> tuple[SnapshotFieldChange, ...]:
        changes: list[SnapshotFieldChange] = []
        if base_resource.schema_version != target_resource.schema_version:
            changes.append(
                SnapshotFieldChange(
                    path="schema_version",
                    base_value=base_resource.schema_version,
                    target_value=target_resource.schema_version,
                )
            )

        base_state = base_resource.to_dict()["state"]
        target_state = target_resource.to_dict()["state"]
        if not isinstance(base_state, dict) or not isinstance(target_state, dict):
            raise ValueError("Snapshot resource state is invalid")  # pragma: no cover
        cls._collect_field_changes(base_state, target_state, "", changes)
        return tuple(changes)

    @classmethod
    def _collect_field_changes(
        cls,
        base: object,
        target: object,
        path: str,
        changes: list[SnapshotFieldChange],
        *,
        base_present: bool = True,
        target_present: bool = True,
    ) -> None:
        if base_present and target_present and cls._json_values_equal(base, target):
            return

        base_mapping = base if isinstance(base, Mapping) else None
        target_mapping = target if isinstance(target, Mapping) else None
        should_expand_mapping = (
            base_mapping is not None
            and target_mapping is not None
            or not base_present
            and target_mapping is not None
            or not target_present
            and base_mapping is not None
        )
        if should_expand_mapping:
            base_keys = set(base_mapping or {})
            target_keys = set(target_mapping or {})
            for key in sorted(base_keys | target_keys):
                key_path = f"{path}.{key}" if path else str(key)
                key_in_base = base_mapping is not None and key in base_mapping
                key_in_target = target_mapping is not None and key in target_mapping
                cls._collect_field_changes(
                    base_mapping[key] if key_in_base else None,
                    target_mapping[key] if key_in_target else None,
                    key_path,
                    changes,
                    base_present=key_in_base,
                    target_present=key_in_target,
                )
            return

        changes.append(
            SnapshotFieldChange(
                path=path or "state",
                base_value=base,
                target_value=target,
                base_present=base_present,
                target_present=target_present,
            )
        )
