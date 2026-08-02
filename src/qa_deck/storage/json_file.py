"""Small helpers for safe local JSON persistence."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_json_list(file_path: Path) -> list[dict[str, object]]:
    if not file_path.exists():
        return []

    text = file_path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("JSON storage root must be a list")
    return data


def write_json_list_atomic(
    file_path: Path,
    items: list[dict[str, object]],
) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            json.dump(items, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)

        os.replace(temporary_path, file_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
