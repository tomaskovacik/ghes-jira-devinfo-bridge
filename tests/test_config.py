from __future__ import annotations

import pytest

from bridge.config import DEFAULT_ISSUE_KEY_REGEX, ConfigError, Settings


def test_from_env_minimal(base_env: dict[str, str]) -> None:
    s = Settings.from_env(base_env)
    assert s.ghes_api_url == "https://ghe.example.com/api/v3"
    assert s.ghes_repos == ["octo/repo"]
    assert s.interval_seconds == 0
    assert s.include_prs is True
    assert s.issue_key_regex == DEFAULT_ISSUE_KEY_REGEX


def test_explicit_api_url_wins(base_env: dict[str, str]) -> None:
    base_env["GHES_API_URL"] = "https://ghe.example.com/custom/v3/"
    assert Settings.from_env(base_env).ghes_api_url == "https://ghe.example.com/custom/v3"


def test_missing_base_url(base_env: dict[str, str]) -> None:
    del base_env["GHES_BASE_URL"]
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)


def test_missing_repo_selectors(base_env: dict[str, str]) -> None:
    del base_env["GHES_REPOS"]
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)


def test_ghes_org_prefixes_bare_repos(base_env: dict[str, str]) -> None:
    base_env["GHES_ORG"] = "octo"
    base_env["GHES_REPOS"] = "api,web"
    s = Settings.from_env(base_env)
    assert s.ghes_org == "octo"
    assert s.ghes_repos == ["api", "web"]


def test_bare_repo_without_org_rejected(base_env: dict[str, str]) -> None:
    base_env["GHES_REPOS"] = "api"
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)


def test_ghes_org_alone_is_a_valid_selector(base_env: dict[str, str]) -> None:
    del base_env["GHES_REPOS"]
    base_env["GHES_ORG"] = "octo"
    assert Settings.from_env(base_env).ghes_org == "octo"


def test_log_entities_toggle(base_env: dict[str, str]) -> None:
    assert Settings.from_env(base_env).log_entities is False
    base_env["SYNC_LOG_ENTITIES"] = "1"
    assert Settings.from_env(base_env).log_entities is True


def test_graphql_url_derived_and_toggle(base_env: dict[str, str]) -> None:
    s = Settings.from_env(base_env)
    assert s.ghes_graphql_url == "https://ghe.example.com/api/graphql"
    assert s.use_graphql is False
    base_env["GHES_USE_GRAPHQL"] = "true"
    assert Settings.from_env(base_env).use_graphql is True


def test_jira_creds_required_unless_dry_run(base_env: dict[str, str]) -> None:
    del base_env["JIRA_OAUTH_CLIENT_ID"]
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)
    base_env["DRY_RUN"] = "true"
    assert Settings.from_env(base_env).dry_run is True


def test_cloud_id_or_site_required(base_env: dict[str, str]) -> None:
    del base_env["JIRA_CLOUD_ID"]
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)
    base_env["JIRA_SITE_URL"] = "https://site.atlassian.net/"
    assert Settings.from_env(base_env).jira_site_url == "https://site.atlassian.net"


def test_project_keys_build_a_restricted_regex(base_env: dict[str, str]) -> None:
    import re

    base_env["JIRA_PROJECT_KEYS"] = "jra, ABC"
    rx = re.compile(Settings.from_env(base_env).issue_key_regex)
    assert rx.findall("fix JRA-4143 and ABC-9 not UTF-8 nor SHA-1") == ["JRA-4143", "ABC-9"]


def test_project_keys_override_custom_regex(base_env: dict[str, str]) -> None:
    base_env["JIRA_ISSUE_KEY_REGEX"] = r"\bZZZ-\d+\b"
    base_env["JIRA_PROJECT_KEYS"] = "JRA"
    assert "JRA" in Settings.from_env(base_env).issue_key_regex


def test_bad_int(base_env: dict[str, str]) -> None:
    base_env["SYNC_INTERVAL_SECONDS"] = "soon"
    with pytest.raises(ConfigError):
        Settings.from_env(base_env)
