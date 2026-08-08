"""Snapshot deletion and compact State Tools integration."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qa_deck.domain import Snapshot
from qa_deck.storage import SnapshotRepository
from tests.helpers import make_app


def snapshot(snapshot_id: str, product_id: str = "sample") -> Snapshot:
    return Snapshot(snapshot_id, product_id, datetime.now(UTC), "Baseline", ())


def test_repository_removes_only_requested_snapshot_atomically(tmp_path: Path) -> None:
    repository = SnapshotRepository(tmp_path / "snapshots.json")
    repository.add(snapshot("first"))
    repository.add(snapshot("second"))

    removed = repository.remove("sample", "first")

    assert removed is not None and removed.id == "first"
    assert [item.id for item in repository.list_all()] == ["second"]


def test_repository_unknown_and_cross_product_remove_do_not_rewrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshots.json"
    repository = SnapshotRepository(path)
    repository.add(snapshot("owned"))
    original = path.read_bytes()

    assert repository.remove("sample", "unknown") is None
    assert repository.remove("other", "owned") is None
    assert path.read_bytes() == original


def test_corrupted_repository_is_not_overwritten_during_remove(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshots.json"
    path.write_text("{corrupted", encoding="utf-8")
    repository = SnapshotRepository(path)

    with pytest.raises(ValueError):
        repository.remove("sample", "snapshot")

    assert path.read_text(encoding="utf-8") == "{corrupted"


def test_delete_route_requires_confirmation_and_redirects_to_snapshots(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    repository.add(snapshot("delete-me"))
    client = app.test_client()

    assert client.post("/products/sample/snapshots/delete-me/delete").status_code == 400
    response = client.post(
        "/products/sample/snapshots/delete-me/delete", data={"confirm": "yes"}
    )

    assert response.status_code == 302
    assert response.location.endswith("#snapshots")
    assert repository.get("delete-me") is None


def test_cross_product_delete_is_not_found(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    repository.add(snapshot("other-snapshot", "other"))

    response = app.test_client().post(
        "/products/sample/snapshots/other-snapshot/delete",
        data={"confirm": "yes"},
    )

    assert response.status_code == 404
    assert repository.get("other-snapshot") is not None


def test_operation_log_failure_does_not_mask_successful_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(tmp_path)
    repository = app.extensions["snapshot_repository"]
    repository.add(snapshot("delete-me"))

    def fail_append(_log: object) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(
        app.extensions["operation_log_repository"], "append", fail_append
    )
    response = app.test_client().post(
        "/products/sample/snapshots/delete-me/delete", data={"confirm": "yes"}
    )

    assert response.status_code == 302
    assert repository.get("delete-me") is None


def test_state_tools_snapshot_tile_and_delete_confirmation_are_semantic(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path)
    app.extensions["snapshot_repository"].add(snapshot("visible"))

    page = app.test_client().get("/products/sample").get_data(as_text=True)

    assert "ІНСТРУМЕНТИ СТАНУ" in page
    assert '<details id="snapshots" class="state-workspace"' in page
    assert "1 збережено" in page
    assert "Цю дію не можна скасувати" in page
    delete_action = (
        'method="post" action="/products/sample/snapshots/visible/delete#snapshots"'
    )
    assert delete_action in page
