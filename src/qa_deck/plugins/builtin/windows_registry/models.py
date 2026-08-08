"""Typed, JSON-compatible models for configured Registry resources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from qa_deck.domain import PluginConfiguration

PLUGIN_IDENTIFIER = "windows-registry"
MAX_TARGETS = 100
MAX_PRESETS = 25
HIDDEN_SUFFIX = ".__qa_deck_hidden__"


class RegistryHive(StrEnum):
    HKCU = "HKCU"
    HKLM = "HKLM"


class RegistryDataType(StrEnum):
    REG_SZ = "REG_SZ"
    REG_EXPAND_SZ = "REG_EXPAND_SZ"
    REG_DWORD = "REG_DWORD"
    REG_QWORD = "REG_QWORD"
    REG_MULTI_SZ = "REG_MULTI_SZ"
    REG_BINARY = "REG_BINARY"


class RegistryValueStatus(StrEnum):
    AVAILABLE = "available"
    MISSING_KEY = "missing_key"
    MISSING_VALUE = "missing_value"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class RegistryBranchStatus(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    MISSING = "missing"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class RegistryBranchVisibility(StrEnum):
    VISIBLE = "visible"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class RegistryValueTarget:
    id: str
    hive: RegistryHive
    key_path: str
    value_name: str
    display_name: str | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: object) -> Self:
        mapping = _mapping(data, "Registry value target")
        return cls(
            id=_target_id(mapping.get("id")),
            hive=_hive(mapping.get("hive")),
            key_path=_key_path(mapping.get("key_path")),
            value_name=_value_name(mapping.get("value_name", "")),
            display_name=_optional_name(mapping.get("display_name")),
            enabled=_boolean(mapping.get("enabled", True), "target enabled"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "hive": self.hive.value,
            "key_path": self.key_path,
            "value_name": self.value_name,
            "display_name": self.display_name,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class RegistryBranchTarget:
    id: str
    hive: RegistryHive
    key_path: str
    display_name: str | None = None
    enabled: bool = True
    hidden_suffix: str = HIDDEN_SUFFIX

    @classmethod
    def from_dict(cls, data: object) -> Self:
        mapping = _mapping(data, "Registry branch target")
        suffix = mapping.get("hidden_suffix", HIDDEN_SUFFIX)
        if suffix != HIDDEN_SUFFIX:
            raise ValueError("Registry branch hidden suffix is unsupported")
        key_path = _key_path(mapping.get("key_path"))
        parent, separator, leaf = key_path.rpartition("\\")
        if not separator or not parent or not leaf:
            raise ValueError("Registry branch target requires a parent and leaf key")
        return cls(
            id=_target_id(mapping.get("id")),
            hive=_hive(mapping.get("hive")),
            key_path=key_path,
            display_name=_optional_name(mapping.get("display_name")),
            enabled=_boolean(mapping.get("enabled", True), "target enabled"),
        )

    @property
    def hidden_key_path(self) -> str:
        parent, separator, leaf = self.key_path.rpartition("\\")
        hidden_leaf = f"{leaf}{self.hidden_suffix}"
        return f"{parent}{separator}{hidden_leaf}" if separator else hidden_leaf

    @property
    def hidden_name(self) -> str:
        return self.hidden_key_path.rsplit("\\", maxsplit=1)[-1]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "hive": self.hive.value,
            "key_path": self.key_path,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "hidden_suffix": self.hidden_suffix,
        }


@dataclass(frozen=True, slots=True)
class RegistryPresetValue:
    target_id: str
    registry_type: RegistryDataType
    value: object

    @classmethod
    def from_dict(cls, data: object) -> Self:
        mapping = _mapping(data, "Registry preset value")
        target_id = _target_id(mapping.get("target_id"))
        try:
            registry_type = RegistryDataType(mapping.get("registry_type"))
        except (TypeError, ValueError) as error:
            raise ValueError("Registry preset value type is unsupported") from error
        return cls(
            target_id=target_id,
            registry_type=registry_type,
            value=_typed_registry_value(registry_type, mapping.get("value")),
        )

    def to_dict(self) -> dict[str, object]:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {
            "target_id": self.target_id,
            "registry_type": self.registry_type.value,
            "value": value,
        }


@dataclass(frozen=True, slots=True)
class RegistryPresetBranch:
    target_id: str
    visibility: RegistryBranchVisibility

    @classmethod
    def from_dict(cls, data: object) -> Self:
        mapping = _mapping(data, "Registry preset branch")
        try:
            visibility = RegistryBranchVisibility(mapping.get("visibility"))
        except (TypeError, ValueError) as error:
            raise ValueError("Registry preset branch visibility is invalid") from error
        return cls(_target_id(mapping.get("target_id")), visibility)

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "visibility": self.visibility.value,
        }


@dataclass(frozen=True, slots=True)
class RegistryPreset:
    id: str
    name: str
    values: tuple[RegistryPresetValue, ...] = ()
    branches: tuple[RegistryPresetBranch, ...] = ()

    @classmethod
    def from_dict(cls, data: object) -> Self:
        mapping = _mapping(data, "Registry preset")
        name = mapping.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Registry preset name must contain 1-100 characters")
        values = _array(mapping.get("values", []), "Registry preset values")
        branches = _array(mapping.get("branches", []), "Registry preset branches")
        preset = cls(
            id=_target_id(mapping.get("id")),
            name=name.strip(),
            values=tuple(RegistryPresetValue.from_dict(item) for item in values),
            branches=tuple(
                RegistryPresetBranch.from_dict(item) for item in branches
            ),
        )
        desired_ids = [item.target_id for item in (*preset.values, *preset.branches)]
        if len(desired_ids) != len(set(desired_ids)):
            raise ValueError("Registry preset target IDs must be unique")
        return preset

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "values": [item.to_dict() for item in self.values],
            "branches": [item.to_dict() for item in self.branches],
        }


@dataclass(frozen=True, slots=True)
class WindowsRegistryConfiguration:
    enabled: bool
    value_targets: tuple[RegistryValueTarget, ...] = ()
    branch_targets: tuple[RegistryBranchTarget, ...] = ()
    presets: tuple[RegistryPreset, ...] = ()

    @classmethod
    def from_plugin_configuration(
        cls,
        configuration: PluginConfiguration,
    ) -> Self:
        if configuration.plugin_identifier != PLUGIN_IDENTIFIER:
            raise ValueError("Configuration belongs to another plugin")
        settings = _mapping(configuration.settings, "Registry configuration")
        value_data = _array(settings.get("value_targets", []), "Value targets")
        branch_data = _array(
            settings.get("branch_targets", []),
            "Branch targets",
        )
        preset_data = _array(settings.get("presets", []), "Registry presets")
        value_targets = tuple(
            RegistryValueTarget.from_dict(item) for item in value_data
        )
        branch_targets = tuple(
            RegistryBranchTarget.from_dict(item) for item in branch_data
        )
        presets = tuple(RegistryPreset.from_dict(item) for item in preset_data)
        cls._validate_collections(value_targets, branch_targets, presets)
        return cls(configuration.enabled, value_targets, branch_targets, presets)

    @classmethod
    def create(
        cls,
        *,
        enabled: bool,
        value_targets: list[object],
        branch_targets: list[object],
        presets: list[object],
    ) -> Self:
        typed = cls(
            enabled=_boolean(enabled, "Registry enabled"),
            value_targets=tuple(
                RegistryValueTarget.from_dict(item) for item in value_targets
            ),
            branch_targets=tuple(
                RegistryBranchTarget.from_dict(item) for item in branch_targets
            ),
            presets=tuple(RegistryPreset.from_dict(item) for item in presets),
        )
        cls._validate_collections(
            typed.value_targets,
            typed.branch_targets,
            typed.presets,
        )
        return typed

    @staticmethod
    def _validate_collections(
        values: tuple[RegistryValueTarget, ...],
        branches: tuple[RegistryBranchTarget, ...],
        presets: tuple[RegistryPreset, ...],
    ) -> None:
        targets = (*values, *branches)
        if len(targets) > MAX_TARGETS:
            raise ValueError(f"Registry supports at most {MAX_TARGETS} targets")
        if len(presets) > MAX_PRESETS:
            raise ValueError(f"Registry supports at most {MAX_PRESETS} presets")
        logical_ids = [target.id.casefold() for target in targets]
        if len(logical_ids) != len(set(logical_ids)):
            raise ValueError("Registry target IDs must be unique")
        preset_ids = [preset.id.casefold() for preset in presets]
        if len(preset_ids) != len(set(preset_ids)):
            raise ValueError("Registry preset IDs must be unique")

        value_locations = [
            (target.hive, target.key_path.casefold(), target.value_name.casefold())
            for target in values
        ]
        if len(value_locations) != len(set(value_locations)):
            raise ValueError("Duplicate physical Registry value target")
        branch_locations = [
            (target.hive, path.casefold())
            for target in branches
            for path in (target.key_path, target.hidden_key_path)
        ]
        if len(branch_locations) != len(set(branch_locations)):
            raise ValueError("Registry branch targets have a path collision")

        value_ids = {target.id for target in values}
        branch_ids = {target.id for target in branches}
        for preset in presets:
            if any(item.target_id not in value_ids for item in preset.values):
                raise ValueError("Registry preset references an unknown value target")
            if any(item.target_id not in branch_ids for item in preset.branches):
                raise ValueError("Registry preset references an unknown branch target")

    def to_settings(self) -> dict[str, object]:
        return {
            "value_targets": [item.to_dict() for item in self.value_targets],
            "branch_targets": [item.to_dict() for item in self.branch_targets],
            "presets": [item.to_dict() for item in self.presets],
        }


@dataclass(frozen=True, slots=True)
class RegistryValueInspection:
    target: RegistryValueTarget
    exists: bool
    registry_type: str | None
    value: object | None
    status: RegistryValueStatus
    message: str


@dataclass(frozen=True, slots=True)
class RegistryBranchInspection:
    target: RegistryBranchTarget
    original_exists: bool | None
    hidden_exists: bool | None
    status: RegistryBranchStatus
    message: str


@dataclass(frozen=True, slots=True)
class RegistryInspectionResult:
    values: tuple[RegistryValueInspection, ...] = ()
    branches: tuple[RegistryBranchInspection, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistryPresetPreviewEntry:
    target_id: str
    display_name: str
    desired_type: str
    desired_value: object


@dataclass(frozen=True, slots=True)
class RegistryPresetPreview:
    preset_id: str
    preset_name: str
    entries: tuple[RegistryPresetPreviewEntry, ...]


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _array(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false")
    return value


def _target_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Registry target id must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 64
        or not normalized[0].isalnum()
        or any(
            not (character.isalnum() or character in "._-")
            for character in normalized
        )
    ):
        raise ValueError("Registry target id has an invalid format")
    return normalized


def _hive(value: object) -> RegistryHive:
    try:
        return RegistryHive(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Registry hive must be HKCU or HKLM") from error


def _key_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Registry key path must be a string")
    normalized = value.strip().strip("\\")
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 32 for character in normalized)
        or any(character in "*?" for character in normalized)
        or "\\\\" in normalized
        or normalized.casefold().startswith(("hkcu\\", "hklm\\"))
    ):
        raise ValueError("Registry key path is invalid")
    return normalized


def _value_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Registry value name must be a string")
    if len(value) > 255 or any(ord(character) < 32 for character in value):
        raise ValueError("Registry value name is invalid")
    return value


def _optional_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Registry display name must be a string or null")
    normalized = value.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("Registry display name must contain 1-100 characters")
    return normalized


def _typed_registry_value(
    registry_type: RegistryDataType,
    value: object,
) -> object:
    if registry_type in {RegistryDataType.REG_SZ, RegistryDataType.REG_EXPAND_SZ}:
        if not isinstance(value, str):
            raise ValueError(f"{registry_type.value} requires a string")
        return value
    if registry_type in {RegistryDataType.REG_DWORD, RegistryDataType.REG_QWORD}:
        maximum = (
            2**32 - 1
            if registry_type is RegistryDataType.REG_DWORD
            else 2**64 - 1
        )
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"{registry_type.value} requires an unsigned integer")
        return value
    if registry_type is RegistryDataType.REG_MULTI_SZ:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError("REG_MULTI_SZ requires a JSON array of strings")
        return tuple(value)
    if not isinstance(value, str):
        raise ValueError("REG_BINARY requires a hexadecimal string")
    normalized = value.replace(" ", "").upper()
    if len(normalized) % 2 or any(
        character not in "0123456789ABCDEF" for character in normalized
    ):
        raise ValueError("REG_BINARY hexadecimal value is invalid")
    return normalized
