from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge.state import STATE_VERSION, RepoState, State


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    state = State.load(str(tmp_path / "does-not-exist.json"))
    assert state.repos == {}


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "state.json"
    state = State()
    state.repo("octo/repo").repo_id = "42"
    state.save(str(path))
    assert path.is_file()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    original = State()
    rs = original.repo("octo/repo")
    rs.repo_id = "123"
    rs.branches = {"main": "aaa", "feature/x": "bbb"}
    rs.pr_high_water = "2026-08-28T10:00:00Z"
    rs.last_success = "2026-08-28T10:05:00Z"
    original.save(str(path))

    reloaded = State.load(str(path))
    assert reloaded == original
    assert reloaded.repo("octo/repo").branches == {"main": "aaa", "feature/x": "bbb"}

    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == STATE_VERSION
    assert on_disk["repos"]["octo/repo"]["repo_id"] == "123"


def test_save_is_atomic_replace(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    first = State()
    first.repo("octo/one").branches = {"main": "sha-old"}
    first.save(str(path))
    assert "octo/one" in json.loads(path.read_text())["repos"]

    second = State()
    second.repo("octo/two").branches = {"main": "sha-new"}
    second.save(str(path))

    data = json.loads(path.read_text())
    assert set(data["repos"]) == {"octo/two"}
    assert data["repos"]["octo/two"]["branches"] == {"main": "sha-new"}
    # no stray temp files left behind
    assert list(tmp_path.iterdir()) == [path]


def test_corrupt_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json ")
    with pytest.raises(ValueError):  # json.JSONDecodeError subclasses ValueError
        State.load(str(path))


def test_non_object_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(RuntimeError):
        State.load(str(path))


def test_newer_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": STATE_VERSION + 1, "repos": {}}))
    with pytest.raises(RuntimeError):
        State.load(str(path))


def test_repo_get_or_create() -> None:
    state = State()
    created = state.repo("octo/repo")
    assert isinstance(created, RepoState)
    assert "octo/repo" in state.repos

    created.repo_id = "7"
    again = state.repo("octo/repo")
    assert again is created
    assert again.repo_id == "7"
