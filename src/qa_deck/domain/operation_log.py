"""Operation history model."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self, cast


class OperationStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    NO_CHANGES = "no_changes"


class RollbackStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    NOT_ATTEMPTED = "not_attempted"
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class OperationLog:
    id: str
    timestamp: datetime
    product_id: str
    plugin_identifier: str
    action_identifier: str
    status: OperationStatus
    summary: str
    changed_count: int
    skipped_count: int
    error_count: int
    rollback_status: RollbackStatus | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "product_id": self.product_id,
            "plugin_identifier": self.plugin_identifier,
            "action_identifier": self.action_identifier,
            "status": self.status.value,
            "summary": self.summary,
            "changed_count": self.changed_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "rollback_status": (
                self.rollback_status.value if self.rollback_status else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        rollback_value = cast(str | None, data.get("rollback_status"))
        return cls(
            id=cast(str, data["id"]),
            timestamp=datetime.fromisoformat(cast(str, data["timestamp"])),
            product_id=cast(str, data["product_id"]),
            plugin_identifier=cast(str, data["plugin_identifier"]),
            action_identifier=cast(str, data["action_identifier"]),
            status=OperationStatus(cast(str, data["status"])),
            summary=cast(str, data["summary"]),
            changed_count=cast(int, data["changed_count"]),
            skipped_count=cast(int, data["skipped_count"]),
            error_count=cast(int, data["error_count"]),
            rollback_status=(
                RollbackStatus(rollback_value) if rollback_value else None
            ),
        )
