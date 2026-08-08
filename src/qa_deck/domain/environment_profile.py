"""Saved desired state for a small, Product-scoped QA environment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Self, cast

PROFILE_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ProfileLicenseState(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"


@dataclass(frozen=True, slots=True)
class EnvironmentProfileLicense:
    resource_id: str
    desired_state: ProfileLicenseState

    def __post_init__(self) -> None:
        if not _safe_resource_id(self.resource_id):
            raise ValueError("License Profile resource must be a configured filename")
        if not isinstance(self.desired_state, ProfileLicenseState):
            raise ValueError("License Profile state must be active or hidden")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "desired_state": self.desired_state.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, Mapping):
            raise ValueError("Environment Profile license entry must be an object")
        if set(data) != {"resource_id", "desired_state"}:
            raise ValueError("Environment Profile license entry has unknown fields")
        return cls(
            resource_id=cast(str, data.get("resource_id")),
            desired_state=ProfileLicenseState(
                cast(str, data.get("desired_state"))
            ),
        )


@dataclass(frozen=True, slots=True)
class EnvironmentProfile:
    id: str
    product_id: str
    name: str
    registry_preset_id: str | None = None
    license_states: tuple[EnvironmentProfileLicense, ...] = ()
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("Environment Profile id is invalid")
        if (
            not isinstance(self.product_id, str)
            or not self.product_id
            or self.product_id != self.product_id.strip()
        ):
            raise ValueError("Environment Profile product_id is required")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Environment Profile name is required")
        if len(self.name.strip()) > 100:
            raise ValueError("Environment Profile name is too long")
        object.__setattr__(self, "name", self.name.strip())
        if self.registry_preset_id is not None and not _IDENTIFIER.fullmatch(
            self.registry_preset_id
        ):
            raise ValueError("Registry preset reference is invalid")
        if not isinstance(self.license_states, tuple) or not all(
            isinstance(item, EnvironmentProfileLicense)
            for item in self.license_states
        ):
            raise ValueError("Environment Profile licenses are invalid")
        identities = [item.resource_id.casefold() for item in self.license_states]
        if len(identities) != len(set(identities)):
            raise ValueError("Environment Profile license references must be unique")
        if self.registry_preset_id is None and not self.license_states:
            raise ValueError("Environment Profile must include desired state")
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError("Environment Profile schema version is unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "product_id": self.product_id,
            "name": self.name.strip(),
            "registry_preset_id": self.registry_preset_id,
            "license_states": [item.to_dict() for item in self.license_states],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, Mapping):
            raise ValueError("Environment Profile must be an object")
        allowed = {
            "id",
            "product_id",
            "name",
            "registry_preset_id",
            "license_states",
            "schema_version",
        }
        if set(data) - allowed:
            raise ValueError("Environment Profile contains unknown fields")
        raw_licenses = data.get("license_states", [])
        if not isinstance(raw_licenses, list):
            raise ValueError("Environment Profile licenses must be a list")
        registry_preset_id = data.get("registry_preset_id")
        if registry_preset_id is not None and not isinstance(
            registry_preset_id, str
        ):
            raise ValueError("Registry preset reference must be a string")
        return cls(
            id=cast(str, data.get("id")),
            product_id=cast(str, data.get("product_id")),
            name=cast(str, data.get("name")),
            registry_preset_id=registry_preset_id,
            license_states=tuple(
                EnvironmentProfileLicense.from_dict(item)
                for item in raw_licenses
            ),
            schema_version=cast(int, data.get("schema_version", 1)),
        )


def _safe_resource_id(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    return not (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or any(ord(character) < 32 for character in value)
    )
