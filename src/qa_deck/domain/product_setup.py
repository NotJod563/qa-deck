"""Portable Product Setup Package models."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from pathlib import PurePosixPath
from typing import Self, cast

PRODUCT_SETUP_SCHEMA_VERSION = 1
PRODUCT_SETUP_BUNDLE_SCHEMA_VERSION = 1
MAX_PRODUCT_SETUP_BUNDLE_ENTRIES = 100
PRODUCT_SETUP_BUNDLE_TYPE = "product_setup_bundle"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class PortablePath:
    """Keep an original path hint and an optional install-relative path."""

    original: str
    relative_to_install: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.original, str) or not self.original.strip():
            raise ValueError("Portable path original value is required")
        if self.original != self.original.strip() or any(
            ord(character) < 32 for character in self.original
        ):
            raise ValueError("Portable path original value is invalid")
        relative = self.relative_to_install
        if relative is None:
            return
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
        ):
            raise ValueError("Portable relative path is invalid")
        path = PurePosixPath(relative)
        if path.is_absolute() or relative != str(path) or ".." in path.parts:
            raise ValueError("Portable relative path is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "original": self.original,
            "relative_to_install": self.relative_to_install,
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict) or set(data) != {
            "original",
            "relative_to_install",
        }:
            raise ValueError("Portable path must contain known fields")
        relative = data.get("relative_to_install")
        if relative is not None and not isinstance(relative, str):
            raise ValueError("Portable relative path must be a string or null")
        return cls(cast(str, data.get("original")), relative)


@dataclass(frozen=True, slots=True)
class ProductSetupProduct:
    name: str
    description: str
    install_directory_hint: str | None
    executable_path: PortablePath | None
    working_directory: PortablePath | None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Product Setup name is required")
        if not isinstance(self.description, str):
            raise ValueError("Product Setup description must be a string")
        if self.install_directory_hint is not None and (
            not isinstance(self.install_directory_hint, str)
            or not self.install_directory_hint.strip()
        ):
            raise ValueError("Install directory hint is invalid")
        object.__setattr__(self, "name", self.name.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "install_directory_hint": self.install_directory_hint,
            "executable_path": (
                self.executable_path.to_dict() if self.executable_path else None
            ),
            "working_directory": (
                self.working_directory.to_dict() if self.working_directory else None
            ),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        expected = {
            "name",
            "description",
            "install_directory_hint",
            "executable_path",
            "working_directory",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("Product Setup product fields are invalid")
        install_hint = data.get("install_directory_hint")
        if install_hint is not None and not isinstance(install_hint, str):
            raise ValueError("Install directory hint must be a string or null")
        executable = data.get("executable_path")
        working = data.get("working_directory")
        return cls(
            name=cast(str, data.get("name")),
            description=cast(str, data.get("description")),
            install_directory_hint=install_hint,
            executable_path=(
                PortablePath.from_dict(executable) if executable is not None else None
            ),
            working_directory=(
                PortablePath.from_dict(working) if working is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PluginSetupSection:
    plugin_identifier: str
    schema_version: int
    data: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_identifier, str) or not _IDENTIFIER.fullmatch(
            self.plugin_identifier
        ):
            raise ValueError("Product Setup plugin identifier is invalid")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("Product Setup plugin schema version is invalid")
        if not isinstance(self.data, dict):
            raise ValueError("Product Setup plugin data must be an object")
        _validate_json(self.data, "Product Setup plugin data")
        object.__setattr__(self, "data", deepcopy(self.data))

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_identifier": self.plugin_identifier,
            "schema_version": self.schema_version,
            "data": deepcopy(self.data),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict) or set(data) != {
            "plugin_identifier",
            "schema_version",
            "data",
        }:
            raise ValueError("Product Setup plugin section is invalid")
        return cls(
            cast(str, data.get("plugin_identifier")),
            cast(int, data.get("schema_version")),
            cast(dict[str, object], data.get("data")),
        )


@dataclass(frozen=True, slots=True)
class ProductSetupPackage:
    product: ProductSetupProduct
    plugin_sections: tuple[PluginSetupSection, ...] = ()
    omitted_plugins: tuple[str, ...] = ()
    schema_version: int = PRODUCT_SETUP_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PRODUCT_SETUP_SCHEMA_VERSION
        ):
            raise ValueError("Product Setup schema version is unsupported")
        if not isinstance(self.product, ProductSetupProduct):
            raise ValueError("Product Setup product is invalid")
        if not isinstance(self.plugin_sections, tuple) or not all(
            isinstance(item, PluginSetupSection) for item in self.plugin_sections
        ):
            raise ValueError("Product Setup plugin sections are invalid")
        identifiers = [item.plugin_identifier for item in self.plugin_sections]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Product Setup plugin identifiers must be unique")
        if not isinstance(self.omitted_plugins, tuple) or not all(
            isinstance(item, str) and _IDENTIFIER.fullmatch(item)
            for item in self.omitted_plugins
        ):
            raise ValueError("Product Setup omitted plugins are invalid")
        if len(self.omitted_plugins) != len(set(self.omitted_plugins)):
            raise ValueError("Product Setup omitted plugins must be unique")
        if set(identifiers) & set(self.omitted_plugins):
            raise ValueError("Product Setup plugin identity is duplicated")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product": self.product.to_dict(),
            "plugin_sections": [item.to_dict() for item in self.plugin_sections],
            "omitted_plugins": list(self.omitted_plugins),
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "product",
            "plugin_sections",
            "omitted_plugins",
        }:
            raise ValueError("Product Setup Package structure is invalid")
        sections = data.get("plugin_sections")
        omitted = data.get("omitted_plugins")
        if not isinstance(sections, list) or not isinstance(omitted, list):
            raise ValueError("Product Setup plugin collections must be lists")
        return cls(
            product=ProductSetupProduct.from_dict(data.get("product")),
            plugin_sections=tuple(PluginSetupSection.from_dict(x) for x in sections),
            omitted_plugins=tuple(cast(list[str], omitted)),
            schema_version=cast(int, data.get("schema_version")),
        )


@dataclass(frozen=True, slots=True)
class ProductSetupBundle:
    """A versioned, ordered container of Product Setup Packages."""

    packages: tuple[ProductSetupPackage, ...]
    schema_version: int = PRODUCT_SETUP_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PRODUCT_SETUP_BUNDLE_SCHEMA_VERSION
        ):
            raise ValueError("Product Setup Bundle schema version is unsupported")
        if (
            not isinstance(self.packages, tuple)
            or not self.packages
            or len(self.packages) > MAX_PRODUCT_SETUP_BUNDLE_ENTRIES
            or not all(isinstance(item, ProductSetupPackage) for item in self.packages)
        ):
            raise ValueError("Product Setup Bundle packages are invalid")
        names = [item.product.name.casefold() for item in self.packages]
        if len(names) != len(set(names)):
            raise ValueError("Product Setup Bundle product names must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "document_type": PRODUCT_SETUP_BUNDLE_TYPE,
            "schema_version": self.schema_version,
            "packages": [item.to_dict() for item in self.packages],
        }

    @classmethod
    def from_dict(cls, data: object) -> Self:
        if not isinstance(data, dict) or set(data) != {
            "document_type",
            "schema_version",
            "packages",
        }:
            raise ValueError("Product Setup Bundle structure is invalid")
        if data.get("document_type") != PRODUCT_SETUP_BUNDLE_TYPE:
            raise ValueError("Product Setup Bundle type is invalid")
        packages = data.get("packages")
        if not isinstance(packages, list):
            raise ValueError("Product Setup Bundle packages must be a list")
        return cls(
            packages=tuple(ProductSetupPackage.from_dict(item) for item in packages),
            schema_version=cast(int, data.get("schema_version")),
        )


def _validate_json(value: object, path: str) -> None:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            _validate_json(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} must be JSON-compatible")
