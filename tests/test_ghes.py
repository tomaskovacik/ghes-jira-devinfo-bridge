"""Tests for :mod:`bridge.ghes` using ``httpx.MockTransport``."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bridge.config import Settings
from bridge.ghes import GhesClient, GhesError
from bridge.models import PR_DECLINED, PR_MERGED, PR_OPEN

FIXTURES = Path(__file__).parent / "fixtures" / "ghes"
API = "https://ghe.example.com/api/v3"


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


def make_settings(env: dict[str, str], **extra: str) -> Settings:
    merged = {
        "GHES_BASE_URL": "https://ghe.example.com",
        "GHES_TOKEN": "test-token",
        "GHES_REPOS": "octo/repo",
        "JIRA_OAUTH_CLIENT_ID": "client-id",
        "JIRA_OAUTH_CLIENT_SECRET": "client-secret",
        "JIRA_CLOUD_ID": "00000000-0000-0000-0000-000000000000",
        **extra,
    }
    return Settings.from_env(merged)


def client_with(settings: Settings, handler) -> GhesClient:
    http = httpx.Client(base_url=settings.ghes_api_url, transport=httpx.MockTransport(handler))
    return GhesClient(settings, http=http)


def json_response(
    payload: object, *, status: int = 200, headers: dict | None = None
) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers or {})


# --------------------------------------------------------------------------
# resolve_repos
# --------------------------------------------------------------------------


def test_resolve_repos_merges_orgs_and_dedups(base_env: dict[str, str]) -> None:
    settings = make_settings(base_env, GHES_REPOS="octo/repo", GHES_ORGS="octo-org")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/orgs/octo-org/repos"
        assert request.url.params.get("type") == "all"
        page = request.url.params.get("page")
        if page is None:
            link = f'<{API}/orgs/octo-org/repos?type=all&per_page=100&page=2>; rel="next"'
            return json_response(load("org_repos_page1.json"), headers={"Link": link})
        assert page == "2"
        return json_response(load("org_repos_page2.json"))

    ghes = client_with(settings, handler)
    # octo/repo from GHES_REPOS (also first org entry -> deduped),
    # octo/hello-world + octo/widgets from the org, octo/legacy skipped (archived).
    assert ghes.resolve_repos() == ["octo/repo", "octo/hello-world", "octo/widgets"]


def test_resolve_repos_prefixes_bare_names_with_ghes_org(base_env: dict[str, str]) -> None:
    settings = make_settings(base_env, GHES_ORG="octo", GHES_REPOS="api,octo-team/web")
    ghes = client_with(settings, lambda r: json_response([]))
    assert ghes.resolve_repos() == ["octo/api", "octo-team/web"]


def test_resolve_repos_discovers_ghes_org_when_no_explicit_repos(base_env: dict[str, str]) -> None:
    settings = make_settings(base_env, GHES_ORG="octo-org", GHES_REPOS="")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/orgs/octo-org/repos"
        return json_response([{"full_name": "octo-org/a"}, {"full_name": "octo-org/b"}])

    assert client_with(settings, handler).resolve_repos() == ["octo-org/a", "octo-org/b"]


# --------------------------------------------------------------------------
# get_repo
# --------------------------------------------------------------------------


def test_get_repo_maps_fields(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/repos/octo/repo"
        return json_response(load("repo.json"))

    meta = client_with(settings, handler).get_repo("octo/repo")
    assert meta is not None
    assert meta.full_name == "octo/repo"
    assert meta.repo_id == "1296269"
    assert meta.url == "https://ghe.example.com/octo/repo"
    assert meta.default_branch == "main"


def test_get_repo_404_returns_none(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"message": "Not Found"}, status=404)

    assert client_with(settings, handler).get_repo("octo/missing") is None


def test_get_repo_500_raises(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"message": "boom"}, status=500)

    with pytest.raises(GhesError):
        client_with(settings, handler).get_repo("octo/repo")


# --------------------------------------------------------------------------
# list_branches
# --------------------------------------------------------------------------


def test_list_branches_paginates_via_link_header(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/repos/octo/repo":
            return json_response(load("repo.json"))
        assert path == "/api/v3/repos/octo/repo/branches"  # no per-branch commit fetch here
        if request.url.params.get("page") is None:
            link = f'<{API}/repos/octo/repo/branches?per_page=100&page=2>; rel="next"'
            return json_response(load("branches_page1.json"), headers={"Link": link})
        return json_response(load("branches_page2.json"))

    branches = client_with(settings, handler).list_branches("octo/repo")
    assert [b.name for b in branches] == ["main", "feature/login"]

    main = branches[0]
    assert main.head_sha == "a" * 40
    assert main.url == "https://ghe.example.com/octo/repo/tree/main"
    assert main.last_commit is not None
    # last_commit is minimal here (sync enriches only branches it pushes)
    assert main.last_commit.sha == "a" * 40
    assert main.last_commit.url == "https://ghe.example.com/octo/repo/commit/" + "a" * 40

    feature = branches[1]
    assert feature.url == "https://ghe.example.com/octo/repo/tree/feature/login"
    assert feature.last_commit is not None
    assert feature.last_commit.url == "https://ghe.example.com/octo/repo/commit/" + "b" * 40


def test_head_commit_maps_full_fields(settings: Settings) -> None:
    sha = "c" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v3/repos/octo/repo/commits/{sha}"
        return json_response(
            {
                "sha": sha,
                "html_url": f"https://ghe.example.com/octo/repo/commit/{sha}",
                "commit": {
                    "message": "PROJ-1 fix",
                    "author": {
                        "name": "Dev",
                        "email": "dev@example.com",
                        "date": "2026-06-10T12:00:00Z",
                    },
                },
                "files": [{"filename": "a.py"}, {"filename": "b.py"}],
            }
        )

    commit = client_with(settings, handler).head_commit("octo/repo", sha)
    assert commit is not None
    assert commit.message == "PROJ-1 fix"
    assert commit.author.email == "dev@example.com"
    assert commit.authored_date == "2026-06-10T12:00:00Z"
    assert commit.file_count == 2


def test_head_commit_returns_none_on_error(settings: Settings) -> None:
    handler = lambda r: json_response({"message": "Not Found"}, status=404)  # noqa: E731
    assert client_with(settings, handler).head_commit("octo/repo", "d" * 40) is None


# --------------------------------------------------------------------------
# active_branches (GraphQL)
# --------------------------------------------------------------------------


def _ref_node(name: str, oid: str, date: str, commits: list[dict], has_next: bool = False) -> dict:
    return {
        "name": name,
        "target": {
            "oid": oid,
            "committedDate": date,
            "history": {"pageInfo": {"hasNextPage": has_next}, "nodes": commits},
        },
    }


def _hist(oid: str, msg: str, date: str) -> dict:
    return {
        "oid": oid,
        "message": msg,
        "committedDate": date,
        "url": f"https://ghe.example.com/octo/repo/commit/{oid}",
        "author": {"name": "Dev", "email": "dev@example.com"},
    }


def test_active_branches_pages_filters_and_orders(settings: Settings) -> None:
    since = "2026-05-30T00:00:00Z"
    page1 = {
        "data": {
            "repository": {
                "url": "https://ghe.example.com/octo/repo",
                "refs": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                    "nodes": [
                        _ref_node(
                            "release-2",
                            "p225",
                            "2026-08-28T12:00:00Z",
                            [
                                _hist("n2", "JRA-1 b", "2026-08-28T12:00:00Z"),
                                _hist("n1", "JRA-1 a", "2026-08-20T09:00:00Z"),
                            ],
                        ),
                        _ref_node("old-branch", "oldsha", "2024-01-01T00:00:00Z", []),
                    ],
                },
            }
        }
    }
    page2 = {
        "data": {
            "repository": {
                "refs": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "C2"},
                    "nodes": [
                        _ref_node(
                            "feature/x",
                            "fx",
                            "2026-07-01T00:00:00Z",
                            [_hist("m1", "ABC-9 x", "2026-07-01T00:00:00Z")],
                        ),
                    ],
                }
            }
        }
    }
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/graphql"
        body = json.loads(request.content)
        calls.append(body["variables"])
        return json_response(page1 if body["variables"]["cursor"] is None else page2)

    scan = client_with(settings, handler).active_branches("octo/repo", since)

    assert [v["cursor"] for v in calls] == [None, "C1"]  # paged once
    assert scan.heads == {"release-2": "p225", "old-branch": "oldsha", "feature/x": "fx"}
    assert [bc.branch.name for bc in scan.active] == [
        "release-2",
        "feature/x",
    ]  # old-branch filtered

    patch = scan.active[0]
    assert patch.branch.head_sha == "p225"
    assert patch.branch.url == "https://ghe.example.com/octo/repo/tree/release-2"
    assert [c.sha for c in patch.commits] == ["n1", "n2"]  # oldest first
    assert patch.branch.last_commit.sha == "n2"  # newest
    assert patch.commits[0].author.email == "dev@example.com"


def test_active_branches_raises_on_graphql_errors(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"errors": [{"message": "boom"}]})

    with pytest.raises(GhesError):
        client_with(settings, handler).active_branches("octo/repo", "2026-05-30T00:00:00Z")


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------


def test_compare_ahead_mapping(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/compare/" in request.url.path
        return json_response(load("compare_ahead.json"))

    result = client_with(settings, handler).compare("octo/repo", "a" * 40, "b" * 40)
    assert result.status == "ahead"
    assert result.merge_base_sha == "a" * 40
    assert [c.sha for c in result.commits] == ["1" * 40, "2" * 40]  # oldest first
    assert all(c.file_count == 3 for c in result.commits)
    assert result.commits[0].message == "PROJ-1 first change"
    assert result.commits[0].author.name == "Octo Cat"
    assert result.commits[0].author.email == "octocat@example.com"
    assert result.commits[0].authored_date == "2026-08-20T10:00:00Z"


def test_compare_diverged_mapping(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(load("compare_diverged.json"))

    result = client_with(settings, handler).compare("octo/repo", "a" * 40, "c" * 40)
    assert result.status == "diverged"
    assert result.merge_base_sha == "9" * 40
    assert [c.sha for c in result.commits] == ["3" * 40]


# --------------------------------------------------------------------------
# commits_since
# --------------------------------------------------------------------------


def test_commits_since_orders_oldest_first_and_maps(settings: Settings) -> None:
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/repos/octo/repo/commits"
        seen_params.update(dict(request.url.params))
        return json_response(load("commits_newest_first.json"))

    commits = client_with(settings, handler).commits_since(
        "octo/repo", "main", "2026-08-01T00:00:00Z"
    )
    assert seen_params["sha"] == "main"
    assert seen_params["since"] == "2026-08-01T00:00:00Z"
    assert [c.sha for c in commits] == ["1" * 40, "2" * 40]  # reversed to oldest-first
    assert commits[0].message == "PROJ-3 older commit"
    assert commits[0].file_count == 0  # no files array on the older commit
    assert commits[1].file_count == 2  # files array present on the newer commit
    assert commits[1].url == "https://ghe.example.com/octo/repo/commit/" + "2" * 40


# --------------------------------------------------------------------------
# pull_requests_since
# --------------------------------------------------------------------------


def test_pull_requests_since_state_mapping_and_early_stop(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/repos/octo/repo/pulls"
        if request.url.params.get("page") == "2":
            raise AssertionError("second page must not be fetched after early stop")
        assert request.url.params.get("state") == "all"
        link = f'<{API}/repos/octo/repo/pulls?per_page=100&page=2>; rel="next"'
        return json_response(load("pulls_page1.json"), headers={"Link": link})

    prs = client_with(settings, handler).pull_requests_since("octo/repo", "2026-08-21T00:00:00Z")
    # #4 (open, 08-25) and #3 (merged, 08-22) kept; #2 (08-20) <= since -> stop.
    assert [p.number for p in prs] == [4, 3]
    assert prs[0].state == PR_OPEN
    assert prs[0].source_branch == "feature/open"
    assert prs[0].destination_branch == "main"
    assert prs[0].author.name == "octocat"
    assert prs[0].comment_count == 2
    assert prs[0].body == "still working"
    assert prs[1].state == PR_MERGED
    assert prs[1].body == ""  # null body normalised


def test_pull_requests_since_declined_mapping(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(load("pulls_page1.json"))

    prs = client_with(settings, handler).pull_requests_since("octo/repo", "2026-01-01T00:00:00Z")
    by_number = {p.number: p for p in prs}
    assert by_number[4].state == PR_OPEN
    assert by_number[3].state == PR_MERGED
    assert by_number[2].state == PR_DECLINED
    assert by_number[1].state == PR_MERGED


# --------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------


def test_retries_on_429_then_succeeds(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("bridge.ghes.time.sleep", slept.append)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return json_response({"message": "slow down"}, status=429, headers={"Retry-After": "0"})
        return json_response(load("repo.json"))

    meta = client_with(settings, handler).get_repo("octo/repo")
    assert calls["n"] == 2
    assert slept == [0.0]
    assert meta is not None
    assert meta.repo_id == "1296269"


def test_retries_exhausted_raises(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.ghes.time.sleep", lambda _s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"message": "still down"}, status=503)

    with pytest.raises(GhesError):
        client_with(settings, handler).commits_since("octo/repo", "main", "2026-08-01T00:00:00Z")


# --------------------------------------------------------------------------
# close
# --------------------------------------------------------------------------


def test_close_only_closes_owned_client(settings: Settings) -> None:
    http = httpx.Client(
        base_url=settings.ghes_api_url,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    injected = GhesClient(settings, http=http)
    injected.close()
    assert http.is_closed is False  # not owned -> left open

    owned = GhesClient(settings)
    owned.close()
    assert owned._http.is_closed is True
