from __future__ import annotations

import httpx
import pytest

from bridge.config import Settings
from bridge.jira import JiraClient, JiraError
from bridge.models import DevinfoResult

CLOUD_ID = "00000000-0000-0000-0000-000000000000"


def _settings(**overrides) -> Settings:
    env = {
        "GHES_BASE_URL": "https://ghe.example.com",
        "GHES_TOKEN": "test-token",
        "GHES_REPOS": "octo/repo",
        "JIRA_OAUTH_CLIENT_ID": "client-id",
        "JIRA_OAUTH_CLIENT_SECRET": "client-secret",
        "JIRA_CLOUD_ID": CLOUD_ID,
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return Settings.from_env(env)


def _client(handler, **overrides) -> JiraClient:
    transport = httpx.MockTransport(handler)
    return JiraClient(_settings(**overrides), http=httpx.Client(transport=transport))


def test_token_fetched_once_then_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 3600})
        raise AssertionError(f"unexpected {request.url}")

    client = _client(handler)
    assert client._get_token() == "tok-123"
    assert client._get_token() == "tok-123"
    assert calls.count("/oauth/token") == 1


def test_token_request_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad creds"})

    client = _client(handler)
    with pytest.raises(JiraError):
        client._get_token()


def test_cloud_id_from_tenant_info_is_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": "resolved-cloud-id"})
        raise AssertionError(f"unexpected {request.url}")

    client = _client(handler, JIRA_CLOUD_ID=None, JIRA_SITE_URL="https://site.example.com")
    assert client.cloud_id() == "resolved-cloud-id"
    assert client.cloud_id() == "resolved-cloud-id"
    assert calls.count("/_edge/tenant_info") == 1


def test_cloud_id_prefers_configured_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    client = _client(handler)
    assert client.cloud_id() == CLOUD_ID


def test_cloud_id_without_source_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    # dry_run lets config skip the cloud-id/site-url requirement
    client = _client(handler, JIRA_CLOUD_ID=None, DRY_RUN="true")
    with pytest.raises(JiraError):
        client.cloud_id()


_REPRESENTATIVE_BODY = {
    "acceptedDevinfoEntities": {
        "42": {
            "commits": ["abcdef1"],
            "branches": ["feature/x"],
            "pullRequests": ["5"],
        }
    },
    "failedDevinfoEntities": [{"id": "bad-1", "errors": [{"message": "nope"}]}],
    "unknownIssueKeys": ["ZZZ-9"],
    "unknownAssociations": [{"associationType": "issueIdOrKeys", "values": ["QQ-1"]}],
}


def test_push_parses_devinfo_result() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path.endswith("/bulk"):
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(202, json=_REPRESENTATIVE_BODY)
        raise AssertionError(f"unexpected {request.url}")

    client = _client(handler)
    result = client.push({"repositories": []})
    assert isinstance(result, DevinfoResult)
    assert set(result.accepted_devinfo_keys) == {"abcdef1", "feature/x", "5"}
    assert result.failed_devinfo_keys == ["bad-1"]
    assert result.unknown_issue_keys == ["ZZZ-9"]
    assert len(result.unknown_associations) == 1
    assert "QQ-1" in result.unknown_associations[0]
    assert any(p.endswith(f"/cloud/{CLOUD_ID}/bulk") for p in seen)


def test_push_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(400, json={"error": "bad payload"})

    client = _client(handler)
    with pytest.raises(JiraError):
        client.push({"repositories": []})


def test_get_repository_maps_and_handles_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path.endswith("/repository/2484"):
            return httpx.Response(200, json={"name": "octo/db", "commits": [1, 2, 3]})
        return httpx.Response(404, json={})

    client = _client(handler)
    assert client.get_repository("2484")["commits"] == [1, 2, 3]
    assert client.get_repository("9999") == {}


def test_delete_repository_issues_request_and_dry_run_no_ops() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append(request)
        return httpx.Response(202)

    _client(handler, DRY_RUN="true").delete_repository("2484")
    assert seen == []

    _client(handler).delete_repository("2484")
    assert len(seen) == 1
    assert seen[0].method == "DELETE"
    assert seen[0].url.path.endswith(f"/cloud/{CLOUD_ID}/repository/2484")


def test_delete_branch_no_ops_under_dry_run() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("no HTTP call expected in dry-run")

    client = _client(handler, DRY_RUN="true")
    client.delete_branch("42", "feature/ABC-1")
    assert calls == []


def test_delete_branch_issues_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append(request)
        return httpx.Response(204)

    client = _client(handler)
    client.delete_branch("42", "feature/ABC 1")
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "DELETE"
    assert req.url.path.endswith(f"/cloud/{CLOUD_ID}/bulkByProperties")
    assert dict(req.url.params) == {"repositoryId": "42", "branchId": "feature/ABC 1"}
    # the raw slash and space are percent/plus encoded in the query string
    assert "branchId=feature%2FABC" in str(req.url)


def test_retry_on_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.jira.time.sleep", lambda *a, **k: None)
    bulk_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path.endswith("/bulk"):
            bulk_calls["n"] += 1
            if bulk_calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, json={})
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.url}")

    client = _client(handler)
    result = client.push({"repositories": []})
    assert isinstance(result, DevinfoResult)
    assert bulk_calls["n"] == 2


def test_retry_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bridge.jira.time.sleep", lambda *a, **k: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(503, json={})

    client = _client(handler, MAX_RETRIES="2")
    with pytest.raises(JiraError):
        client.push({"repositories": []})


def test_close_only_closes_owned_client() -> None:
    external = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    client = JiraClient(_settings(), http=external)
    client.close()
    assert not external.is_closed
    external.close()

    owned = JiraClient(_settings())
    owned.close()
    assert owned._http.is_closed
