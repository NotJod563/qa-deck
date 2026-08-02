"""Append-only JSON repository for operation logs."""

from pathlib import Path

from qa_deck.domain import OperationLog
from qa_deck.storage.json_file import read_json_list, write_json_list_atomic


class OperationLogRepository:
    def __init__(self, file_path: str | Path) -> None:
        self._file_path = Path(file_path)

    def append(self, operation_log: OperationLog) -> None:
        logs = self._list_all()
        logs.append(operation_log)
        write_json_list_atomic(
            self._file_path,
            [item.to_dict() for item in logs],
        )

    def list_for_product(
        self,
        product_id: str,
        limit: int | None = None,
    ) -> list[OperationLog]:
        logs = [
            item for item in self._list_all() if item.product_id == product_id
        ]
        logs.sort(key=lambda item: item.timestamp, reverse=True)
        return logs if limit is None else logs[:limit]

    def _list_all(self) -> list[OperationLog]:
        return [
            OperationLog.from_dict(item)
            for item in read_json_list(self._file_path)
        ]
