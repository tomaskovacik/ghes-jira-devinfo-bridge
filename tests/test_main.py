from __future__ import annotations

import pytest

from bridge import __main__ as m
from bridge.config import Settings

_ENV = {
    "GHES_BASE_URL": "https://ghe.example.com",
    "GHES_TOKEN": "test-token",
    "GHES_REPOS": "octo/repo",
    "JIRA_OAUTH_CLIENT_ID": "id",
    "JIRA_OAUTH_CLIENT_SECRET": "secret",
    "JIRA_CLOUD_ID": "00000000-0000-0000-0000-000000000000",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = Settings.from_env(_ENV)
    monkeypatch.setattr(m.Settings, "from_env", staticmethod(lambda *a, **k: fixed))
    monkeypatch.setattr(m, "_configure_logging", lambda *a, **k: None)


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    called: dict[str, object] = {}

    def _mark(key):
        def _fn(*args, **_kw):
            called["cmd"] = key
            called["args"] = args[0] if args else None
            return 0

        return _fn

    monkeypatch.setattr(m, "_run_sync", _mark("sync"))
    monkeypatch.setattr(m, "_run_inspect", _mark("inspect"))
    monkeypatch.setattr(m, "_run_delete_repo", _mark("delete"))
    monkeypatch.setattr(m, "_run_reprocess", _mark("reprocess"))
    return called


def test_default_and_explicit_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _capture(monkeypatch)
    assert m.main([]) == 0
    assert called["cmd"] == "sync"
    called.clear()
    assert m.main(["sync"]) == 0
    assert called["cmd"] == "sync"


def test_inspect_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _capture(monkeypatch)
    assert m.main(["inspect", "--repo", "octo/db", "--json"]) == 0
    assert called["cmd"] == "inspect"
    assert called["args"].repo == "octo/db" and called["args"].json is True


def test_delete_repo_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _capture(monkeypatch)
    assert m.main(["delete-repo", "--repo-id", "2484", "--yes"]) == 0
    assert called["cmd"] == "delete"
    assert called["args"].repo_id == "2484" and called["args"].yes is True


def test_reprocess_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _capture(monkeypatch)
    assert m.main(["reprocess", "--all"]) == 0
    assert called["cmd"] == "reprocess"
    assert called["args"].all is True
    called.clear()
    assert m.main(["reprocess", "--repo", "octo/db"]) == 0
    assert called["args"].repo == "octo/db"


def test_reprocess_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        m._parse_args(["reprocess"])


def test_inspect_full_flag_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _capture(monkeypatch)
    assert m.main(["inspect", "--repo", "octo/db", "--full"]) == 0
    assert called["args"].full is True and called["args"].json is False


def test_inspect_json_and_full_are_exclusive() -> None:
    with pytest.raises(SystemExit):
        m._parse_args(["inspect", "--repo", "x/y", "--json", "--full"])


def test_print_full_renders_hashes(capsys: pytest.CaptureFixture[str]) -> None:
    doc = {
        "commits": [
            {
                "id": "a" * 40,
                "authorTimestamp": "2026-08-01T00:00:00Z",
                "issueKeys": ["JRA-1"],
                "message": "JRA-1 do it\n\nbody",
            },
            {"id": "b" * 40, "authorTimestamp": "", "issueKeys": [], "message": ""},
        ],
        "branches": [{"id": "release-1", "issueKeys": ["JRA-1"]}],
        "pullRequests": [],
    }
    m._print_full("octo/db", "2484", doc)
    out = capsys.readouterr().out
    assert "commits in dev-info for repo octo/db: 2" in out
    assert "a" * 40 in out and "b" * 40 in out
    assert "JRA-1 do it" in out
    assert "branches: 1" in out and "release-1" in out


def test_inspect_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        m._parse_args(["inspect"])


def test_delete_repo_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        m._parse_args(["delete-repo"])
