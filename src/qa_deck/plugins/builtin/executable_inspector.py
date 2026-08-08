"""Read-only inspection of a product executable path."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from stat import S_ISREG

from qa_deck.domain import PluginConfiguration, Product
from qa_deck.domain.snapshot import SnapshotCaptureResult, SnapshotResource
from qa_deck.plugins.api import Plugin, PluginAction, RiskLevel


class ExecutableInspectionStatus(StrEnum):
    """Outcome of an executable path inspection."""

    NOT_CONFIGURED = "not_configured"
    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExecutableInspectionResult:
    """Safe metadata collected for an executable path."""

    original_path: str | None
    status: ExecutableInspectionStatus
    exists: bool | None
    is_file: bool | None
    file_name: str | None = None
    extension: str | None = None
    size_bytes: int | None = None
    modified_at: datetime | None = None
    message: str | None = None


class ExecutableInspector:
    """Inspect executable file metadata without reading or running the file."""

    identifier = "qa_deck.executable_inspector"
    display_name = "Executable Inspector"
    description = "Перевіряє шлях і базові метадані executable файла."
    version = "0.1.0"

    def get_actions(self) -> list[PluginAction]:
        return [
            PluginAction(
                identifier="inspect_executable",
                display_name="Перевірити executable",
                description="Перевірити наявність файла та прочитати його метадані.",
                risk_level=RiskLevel.SAFE,
            )
        ]

    def inspect(self, original_path: str | None) -> ExecutableInspectionResult:
        """Inspect a path without reading file contents or changing the file."""
        if original_path is None or not original_path.strip():
            return ExecutableInspectionResult(
                original_path=original_path,
                status=ExecutableInspectionStatus.NOT_CONFIGURED,
                exists=False,
                is_file=False,
                message="Шлях до executable не вказано.",
            )

        path = Path(original_path)
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return ExecutableInspectionResult(
                original_path=original_path,
                status=ExecutableInspectionStatus.NOT_FOUND,
                exists=False,
                is_file=False,
                file_name=path.name or None,
                extension=path.suffix or None,
                message="Executable файл не знайдено за вказаним шляхом.",
            )
        except OSError:
            return ExecutableInspectionResult(
                original_path=original_path,
                status=ExecutableInspectionStatus.ERROR,
                exists=None,
                is_file=None,
                file_name=path.name or None,
                extension=path.suffix or None,
                message="Не вдалося прочитати метадані executable файла.",
            )

        if not S_ISREG(metadata.st_mode):
            return ExecutableInspectionResult(
                original_path=original_path,
                status=ExecutableInspectionStatus.NOT_A_FILE,
                exists=True,
                is_file=False,
                file_name=path.name or None,
                extension=path.suffix or None,
                message="Вказаний шлях не є звичайним файлом.",
            )

        try:
            modified_at = datetime.fromtimestamp(metadata.st_mtime, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return ExecutableInspectionResult(
                original_path=original_path,
                status=ExecutableInspectionStatus.ERROR,
                exists=True,
                is_file=True,
                file_name=path.name or None,
                extension=path.suffix or None,
                size_bytes=metadata.st_size,
                message="Не вдалося прочитати метадані executable файла.",
            )

        return ExecutableInspectionResult(
            original_path=original_path,
            status=ExecutableInspectionStatus.AVAILABLE,
            exists=True,
            is_file=True,
            file_name=path.name,
            extension=path.suffix or None,
            size_bytes=metadata.st_size,
            modified_at=modified_at,
        )

    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        try:
            result = self.inspect(product.executable_path)
        except Exception:  # pragma: no cover
            return SnapshotCaptureResult(
                resources=(
                    SnapshotResource(
                        source=self.identifier,
                        resource_type="executable",
                        identifier="primary-executable",
                        state={"status": "error"},
                    ),
                ),
                warnings=("Executable inspection failed.",),
            )

        warnings: tuple[str, ...] = ()
        if result.status != ExecutableInspectionStatus.AVAILABLE:
            warnings = (f"Executable inspection returned {result.status.value}.",)

        return SnapshotCaptureResult(
            resources=(
                SnapshotResource(
                    source=self.identifier,
                    resource_type="executable",
                    identifier="primary-executable",
                    state={
                        "original_path": result.original_path,
                        "status": result.status.value,
                        "exists": result.exists,
                        "is_file": result.is_file,
                        "file_name": result.file_name,
                        "extension": result.extension,
                        "size_bytes": result.size_bytes,
                        "modified_at": result.modified_at.isoformat()
                        if result.modified_at is not None
                        else None,
                        "message": result.message,
                    },
                ),
            ),
            warnings=warnings,
        )


def create_executable_inspector() -> Plugin:
    """Create the built-in Executable Inspector plugin."""
    return ExecutableInspector()
