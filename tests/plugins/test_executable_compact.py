"""Executable Inspector representative behavior."""

from pathlib import Path
from unittest.mock import patch

from qa_deck.domain import Product
from qa_deck.plugins.builtin import ExecutableInspectionStatus, ExecutableInspector
from tests.helpers import make_app, products


def test_existing_executable_returns_metadata(tmp_path: Path) -> None:
    executable = tmp_path / "Sample.EXE"
    executable.write_bytes(b"executable")

    result = ExecutableInspector().inspect(str(executable))

    assert result.status is ExecutableInspectionStatus.AVAILABLE
    assert (result.file_name, result.extension, result.size_bytes) == (
        "Sample.EXE",
        ".EXE",
        10,
    )
    assert result.modified_at is not None


def test_missing_directory_and_filesystem_errors_are_controlled(
    tmp_path: Path,
) -> None:
    inspector = ExecutableInspector()
    results = [
        inspector.inspect(None),
        inspector.inspect(str(tmp_path / "missing.exe")),
        inspector.inspect(str(tmp_path)),
    ]
    with patch.object(Path, "stat", side_effect=PermissionError("private")):
        results.append(inspector.inspect(str(tmp_path / "restricted.exe")))

    assert [result.status for result in results] == [
        ExecutableInspectionStatus.NOT_CONFIGURED,
        ExecutableInspectionStatus.NOT_FOUND,
        ExecutableInspectionStatus.NOT_A_FILE,
        ExecutableInspectionStatus.ERROR,
    ]
    assert all("private" not in (result.message or "") for result in results)


def test_web_inspection_is_read_only_for_product(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    executable = tmp_path / "sample.exe"
    executable.write_bytes(b"unchanged")
    product = Product("executable", "Executable", executable_path=str(executable))
    products(app).add(product)
    before = products(app).get(product.id)

    response = app.test_client().post(
        "/products/executable/inspect-executable"
    )

    assert response.status_code == 200
    assert "Файл доступний" in response.get_data(as_text=True)
    assert products(app).get(product.id) == before
    assert executable.read_bytes() == b"unchanged"
