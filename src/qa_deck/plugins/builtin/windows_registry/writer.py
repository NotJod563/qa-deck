"""Minimal write boundary for configured Registry values and branch renames."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from typing import Protocol

from qa_deck.plugins.builtin.windows_registry.models import (
    RegistryBranchTarget,
    RegistryBranchVisibility,
    RegistryDataType,
    RegistryHive,
    RegistryValueTarget,
)

_DELETE_ACCESS = 0x00010000


class RegistryWriter(Protocol):
    """Mutate only a server-resolved configured Registry target."""

    def set_value(
        self,
        target: RegistryValueTarget,
        registry_type: RegistryDataType,
        value: object,
    ) -> None: ...

    def rename_branch(
        self,
        target: RegistryBranchTarget,
        desired_visibility: RegistryBranchVisibility,
    ) -> None: ...


class WindowsRegistryWriter:
    """Use only existing-key value writes and stay import-safe off Windows."""

    def __init__(
        self,
        winreg_module: object | None = None,
        rename_key: Callable[[int, str, str], int] | None = None,
    ) -> None:
        self._winreg = winreg_module or (
            importlib.import_module("winreg") if sys.platform == "win32" else None
        )
        self._rename_key = rename_key

    def set_value(
        self,
        target: RegistryValueTarget,
        registry_type: RegistryDataType,
        value: object,
    ) -> None:
        if self._winreg is None:
            raise OSError("Windows Registry is unavailable on this platform")
        native_type = {
            RegistryDataType.REG_SZ: self._winreg.REG_SZ,
            RegistryDataType.REG_EXPAND_SZ: self._winreg.REG_EXPAND_SZ,
            RegistryDataType.REG_DWORD: self._winreg.REG_DWORD,
            RegistryDataType.REG_QWORD: self._winreg.REG_QWORD,
            RegistryDataType.REG_MULTI_SZ: self._winreg.REG_MULTI_SZ,
            RegistryDataType.REG_BINARY: self._winreg.REG_BINARY,
        }[registry_type]
        native_value = self._native_value(registry_type, value)
        with self._winreg.OpenKey(
            self._hive(target.hive),
            target.key_path,
            0,
            self._winreg.KEY_QUERY_VALUE | self._winreg.KEY_SET_VALUE,
        ) as registry_key:
            self._winreg.QueryValueEx(registry_key, target.value_name)
            self._winreg.SetValueEx(
                registry_key,
                target.value_name,
                0,
                native_type,
                native_value,
            )

    def rename_branch(
        self,
        target: RegistryBranchTarget,
        desired_visibility: RegistryBranchVisibility,
    ) -> None:
        if self._winreg is None:
            raise OSError("Windows Registry is unavailable on this platform")
        parent, separator, visible_name = target.key_path.rpartition("\\")
        if not separator or not parent or not visible_name:
            raise ValueError("Registry branch target requires a parent and leaf key")
        hidden_name = target.hidden_name
        source_name, destination_name = (
            (visible_name, hidden_name)
            if desired_visibility is RegistryBranchVisibility.HIDDEN
            else (hidden_name, visible_name)
        )
        rename_key = self._rename_key or self._native_rename_key()
        with self._winreg.OpenKey(
            self._hive(target.hive),
            parent,
            0,
            self._winreg.KEY_CREATE_SUB_KEY,
        ) as parent_key:
            with self._winreg.OpenKey(
                parent_key,
                source_name,
                0,
                _DELETE_ACCESS,
            ):
                error_code = rename_key(
                    int(parent_key),
                    source_name,
                    destination_name,
                )
        if error_code:
            raise self._windows_error(error_code)

    @staticmethod
    def _native_rename_key() -> Callable[[int, str, str], int]:
        if sys.platform != "win32":
            raise OSError("Native Registry branch rename is unavailable")
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        try:
            rename_key = advapi32.RegRenameKeyW
        except AttributeError:
            # RegRenameKey is the documented Unicode-only export on supported
            # Windows versions; some SDK descriptions refer to it with a W suffix.
            rename_key = advapi32.RegRenameKey
        rename_key.argtypes = (wintypes.HKEY, wintypes.LPCWSTR, wintypes.LPCWSTR)
        rename_key.restype = wintypes.LONG
        return rename_key

    @staticmethod
    def _windows_error(error_code: int) -> OSError:
        if sys.platform == "win32":
            import ctypes

            return ctypes.WinError(error_code)
        return OSError(error_code, f"Windows error {error_code}")

    def _hive(self, hive: RegistryHive) -> object:
        return {
            RegistryHive.HKCU: self._winreg.HKEY_CURRENT_USER,
            RegistryHive.HKLM: self._winreg.HKEY_LOCAL_MACHINE,
        }[hive]

    @staticmethod
    def _native_value(registry_type: RegistryDataType, value: object) -> object:
        if registry_type is RegistryDataType.REG_BINARY:
            if not isinstance(value, str):  # typed domain should prevent this
                raise ValueError("REG_BINARY value is invalid")
            return bytes.fromhex(value)
        if registry_type is RegistryDataType.REG_MULTI_SZ:
            if not isinstance(value, tuple):  # typed domain should prevent this
                raise ValueError("REG_MULTI_SZ value is invalid")
            return list(value)
        return value
