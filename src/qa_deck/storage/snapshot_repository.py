"""JSON-backed snapshot storage for QA Deck."""

from pathlib import Path

from qa_deck.domain import Snapshot
from qa_deck.storage.json_file import read_json_list, write_json_list_atomic


class SnapshotRepository:
    """Store snapshots in a local JSON file."""

    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def add(self, snapshot: Snapshot) -> None:
        snapshots = self._list_all()
        if any(existing.id == snapshot.id for existing in snapshots):
            raise ValueError(f"Snapshot with id '{snapshot.id}' already exists")
        snapshots.append(snapshot)
        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in snapshots],
        )

    def get(self, snapshot_id: str) -> Snapshot | None:
        return next(
            (item for item in self._list_all() if item.id == snapshot_id),
            None,
        )

    def list_for_product(self, product_id: str) -> list[Snapshot]:
        snapshots = [
            item for item in self._list_all() if item.product_id == product_id
        ]
        snapshots.sort(key=lambda item: item.created_at, reverse=True)
        return snapshots

    def list_all(self) -> list[Snapshot]:
        return self._list_all()

    def remove(self, product_id: str, snapshot_id: str) -> Snapshot | None:
        """Atomically remove one snapshot owned by the given Product."""
        snapshots = self._list_all()
        removed = next(
            (
                item
                for item in snapshots
                if item.id == snapshot_id and item.product_id == product_id
            ),
            None,
        )
        if removed is None:
            return None
        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in snapshots if item.id != snapshot_id],
        )
        return removed

    def delete_for_product(self, product_id: str) -> list[Snapshot]:
        snapshots = self._list_all()
        removed = [item for item in snapshots if item.product_id == product_id]
        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in snapshots if item.product_id != product_id],
        )
        return removed

    def _list_all(self) -> list[Snapshot]:
        return [
            Snapshot.from_dict(item)
            for item in read_json_list(self._file_path)
        ]
