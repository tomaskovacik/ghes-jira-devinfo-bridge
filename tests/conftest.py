"""Shared test fixtures."""

from __future__ import annotations

import pytest

from bridge.config import Settings


@pytest.fixture
def base_env() -> dict[str, str]:
    """Minimal valid environment for ``Settings.from_env``."""
    return {
        "GHES_BASE_URL": "https://ghe.example.com",
        "GHES_TOKEN": "test-token",
        "GHES_REPOS": "octo/repo",
        "JIRA_OAUTH_CLIENT_ID": "client-id",
        "JIRA_OAUTH_CLIENT_SECRET": "client-secret",
        "JIRA_CLOUD_ID": "00000000-0000-0000-0000-000000000000",
    }


@pytest.fixture
def settings(base_env: dict[str, str]) -> Settings:
    return Settings.from_env(base_env)
