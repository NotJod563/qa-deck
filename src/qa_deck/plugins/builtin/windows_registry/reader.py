"""Read-only adapter boundary for Windows Registry inspection."""

from __future__ import annotations

import importlib
import sys
from typing import Protocol

from qa_deck.plugins.builtin.windows_registry.models import (
    RegistryBranchInspection,
    RegistryBranchStatus,
    RegistryBranchTarget,
    RegistryHive,
    RegistryValueInspection,
    RegistryValueStatus,
    RegistryValueTarget,
)


class RegistryReader(Protocol):
    """Inspect only explicitly configured Registry targets."""

    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        ...

    def inspect_branch(
        self,
        target: RegistryBranchTarget,
    ) -> RegistryBranchInspection:
        ...


class WindowsRegistryReader:
    """Use winreg read APIs without importing them on non-Windows systems."""

    def __init__(self) -> None:
        self._winreg = (
            importlib.import_module("winreg") if sys.platform == "win32" else None
        )

    def inspect_value(self, target: RegistryValueTarget) -> RegistryValueInspection:
        if self._winreg is None:
            return RegistryValueInspection(
                target,
                False,
                None,
                None,
                RegistryValueStatus.UNAVAILABLE,
                "Windows Registry is unavailable on this platform.",
            )
        try:
            with self._winreg.OpenKey(
                self._hive(target.hive),
                target.key_path,
                0,
                self._winreg.KEY_READ,
            ) as registry_key:
                try:
                    value, native_type = self._winreg.QueryValueEx(
                        registry_key,
                        target.value_name,
                    )
                except FileNotFoundError:
                    return RegistryValueInspection(
                        target,
                        False,
                        None,
                        None,
                        RegistryValueStatus.MISSING_VALUE,
                        "Configured Registry value does not exist.",
                    )
        except FileNotFoundError:
            return RegistryValueInspection(
                target,
                False,
                None,
                None,
                RegistryValueStatus.MISSING_KEY,
                "Configured Registry key does not exist.",
            )
        except PermissionError:
            return RegistryValueInspection(
                target,
                False,
                None,
                None,
                RegistryValueStatus.UNAVAILABLE,
                "Configured Registry value is not accessible.",
            )
        except OSError:
            return RegistryValueInspection(
                target,
                False,
                None,
                None,
                RegistryValueStatus.ERROR,
                "Registry value inspection failed.",
            )

        registry_type = self._type_name(native_type)
        serialized = self._json_value(value, registry_type)
        if registry_type is None or serialized is _INVALID:
            return RegistryValueInspection(
                target,
                True,
                registry_type,
                None,
                RegistryValueStatus.ERROR,
                "Registry value uses an unsupported representation.",
            )
        return RegistryValueInspection(
            target,
            True,
            registry_type,
            serialized,
            RegistryValueStatus.AVAILABLE,
            "Configured Registry value is available.",
        )

    def inspect_branch(
        self,
        target: RegistryBranchTarget,
    ) -> RegistryBranchInspection:
        if self._winreg is None:
            return RegistryBranchInspection(
                target,
                None,
                None,
                RegistryBranchStatus.UNAVAILABLE,
                "Windows Registry is unavailable on this platform.",
            )
        try:
            original_exists = self._key_exists(target.hive, target.key_path)
            hidden_exists = self._key_exists(target.hive, target.hidden_key_path)
        except PermissionError:
            return RegistryBranchInspection(
                target,
                None,
                None,
                RegistryBranchStatus.UNAVAILABLE,
                "Configured Registry branch is not accessible.",
            )
        except OSError:
            return RegistryBranchInspection(
                target,
                None,
                None,
                RegistryBranchStatus.ERROR,
                "Registry branch inspection failed.",
            )

        if original_exists and hidden_exists:
            status = RegistryBranchStatus.CONFLICT
            message = "Visible and hidden Registry branches both exist."
        elif original_exists:
            status = RegistryBranchStatus.VISIBLE
            message = "Configured Registry branch is visible."
        elif hidden_exists:
            status = RegistryBranchStatus.HIDDEN
            message = "Configured Registry branch is hidden."
        else:
            status = RegistryBranchStatus.MISSING
            message = "Configured Registry branch does not exist."
        return RegistryBranchInspection(
            target,
            original_exists,
            hidden_exists,
            status,
            message,
        )

    def _key_exists(self, hive: RegistryHive, key_path: str) -> bool:
        try:
            with self._winreg.OpenKey(
                self._hive(hive),
                key_path,
                0,
                self._winreg.KEY_READ,
            ):
                return True
        except FileNotFoundError:
            return False

    def _hive(self, hive: RegistryHive) -> object:
        return {
            RegistryHive.HKCU: self._winreg.HKEY_CURRENT_USER,
            RegistryHive.HKLM: self._winreg.HKEY_LOCAL_MACHINE,
        }[hive]

    def _type_name(self, native_type: int) -> str | None:
        names = {
            self._winreg.REG_SZ: "REG_SZ",
            self._winreg.REG_EXPAND_SZ: "REG_EXPAND_SZ",
            self._winreg.REG_DWORD: "REG_DWORD",
            self._winreg.REG_QWORD: "REG_QWORD",
            self._winreg.REG_MULTI_SZ: "REG_MULTI_SZ",
            self._winreg.REG_BINARY: "REG_BINARY",
        }
        return names.get(native_type)

    @staticmethod
    def _json_value(value: object, registry_type: str | None) -> object:
        if registry_type == "REG_BINARY":
            return value.hex().upper() if isinstance(value, bytes) else _INVALID
        if registry_type == "REG_MULTI_SZ":
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return list(value)
            return _INVALID
        if registry_type in {"REG_DWORD", "REG_QWORD"}:
            return value if type(value) is int else _INVALID
        if registry_type in {"REG_SZ", "REG_EXPAND_SZ"}:
            return value if isinstance(value, str) else _INVALID
        return _INVALID


_INVALID = object()
