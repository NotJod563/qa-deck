"""Atomic JSON repository for Product-scoped Environment Profiles."""

from pathlib import Path

from qa_deck.domain.environment_profile import EnvironmentProfile
from qa_deck.storage.json_file import read_json_list, write_json_list_atomic


class EnvironmentProfileRepository:
    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def add(self, profile: EnvironmentProfile) -> None:
        profiles = self._list_all()
        if any(
            existing.product_id == profile.product_id
            and existing.id == profile.id
            for existing in profiles
        ):
            raise ValueError("Environment Profile id already exists for Product")
        profiles.append(profile)
        self._write(profiles)

    def update(self, profile: EnvironmentProfile) -> None:
        profiles = self._list_all()
        found = False
        updated: list[EnvironmentProfile] = []
        for existing in profiles:
            if (
                existing.product_id == profile.product_id
                and existing.id == profile.id
            ):
                updated.append(profile)
                found = True
            else:
                updated.append(existing)
        if not found:
            raise ValueError("Environment Profile does not exist for Product")
        self._write(updated)

    def get(self, product_id: str, profile_id: str) -> EnvironmentProfile | None:
        return next(
            (
                profile
                for profile in self._list_all()
                if profile.product_id == product_id and profile.id == profile_id
            ),
            None,
        )

    def list_for_product(self, product_id: str) -> list[EnvironmentProfile]:
        return [
            profile
            for profile in self._list_all()
            if profile.product_id == product_id
        ]

    def remove(
        self, product_id: str, profile_id: str
    ) -> EnvironmentProfile | None:
        profiles = self._list_all()
        removed = next(
            (
                profile
                for profile in profiles
                if profile.product_id == product_id and profile.id == profile_id
            ),
            None,
        )
        if removed is None:
            return None
        self._write(
            [
                profile
                for profile in profiles
                if not (
                    profile.product_id == product_id and profile.id == profile_id
                )
            ]
        )
        return removed

    def delete_for_product(self, product_id: str) -> list[EnvironmentProfile]:
        profiles = self._list_all()
        removed = [item for item in profiles if item.product_id == product_id]
        self._write([item for item in profiles if item.product_id != product_id])
        return removed

    def _list_all(self) -> list[EnvironmentProfile]:
        return [
            EnvironmentProfile.from_dict(item)
            for item in read_json_list(self._file_path)
        ]

    def _write(self, profiles: list[EnvironmentProfile]) -> None:
        write_json_list_atomic(
            self._file_path,
            [profile.to_dict() for profile in profiles],
        )
