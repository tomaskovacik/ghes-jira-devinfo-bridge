"""Environment-driven configuration. No secret files, 12-factor only."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_ISSUE_KEY_REGEX = r"\b[A-Z][A-Z0-9]{1,9}-\d+\b"
DEFAULT_TOKEN_URL = "https://api.atlassian.com/oauth/token"
DEFAULT_API_BASE = "https://api.atlassian.com"


class ConfigError(ValueError):
    """Raised when the environment is missing or inconsistent."""


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _list(env: Mapping[str, str], name: str) -> list[str]:
    return [item.strip() for item in env.get(name, "").split(",") if item.strip()]


def _chunk(env: Mapping[str, str], name: str, default: int) -> int:
    """Commits per devinfo bulk POST. 0 = never split; otherwise clamp to the
    Jira spec ceiling of 400."""
    value = _int(env, name, default)
    if value <= 0:
        return 0
    return min(400, value)


@dataclass(frozen=True)
class Settings:
    ghes_base_url: str
    ghes_api_url: str
    ghes_token: str
    ghes_graphql_url: str = ""
    use_graphql: bool = False
    ghes_org: str = ""
    ghes_repos: list[str] = field(default_factory=list)
    ghes_orgs: list[str] = field(default_factory=list)
    ghes_branch_exclude: list[str] = field(default_factory=list)

    jira_client_id: str = ""
    jira_client_secret: str = ""
    jira_cloud_id: str = ""
    jira_site_url: str = ""
    jira_token_url: str = DEFAULT_TOKEN_URL
    jira_api_base: str = DEFAULT_API_BASE

    issue_key_regex: str = DEFAULT_ISSUE_KEY_REGEX
    interval_seconds: int = 0
    lookback_days: int = 14
    include_prs: bool = True
    prevent_transitions: bool = True
    keyed_branches_only: bool = False
    default_branch_only: bool = False
    concurrency: int = 8
    # commits per devinfo bulk POST; 0 = never split. Jira spec ceiling is 400.
    push_chunk_size: int = 400
    # send operationType=BACKFILL the first time a repo is synced
    backfill_on_first_sight: bool = True
    # Issue linkage form. The two are mutually exclusive on one entity (Jira
    # 400s a payload carrying both). `issueKeys` is DEPRECATED in the Cloud API
    # docs, so the default is `associations` (associationType issueIdOrKeys).
    # Set send_issue_keys=True to fall back to the deprecated issueKeys array.
    send_issue_keys: bool = False
    send_associations: bool = True
    issue_key_cap: int = 500  # per-entity cap on issueKeys / association values
    log_entities: bool = False

    state_path: str = "/data/state.json"
    dry_run: bool = False
    log_level: str = "INFO"
    log_format: str = "text"  # "text" or "json"
    http_timeout: float = 30.0
    max_retries: int = 4
    user_agent: str = "ghes-jira-devinfo-bridge"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        base = env.get("GHES_BASE_URL", "").rstrip("/")
        if not base:
            raise ConfigError("GHES_BASE_URL is required")
        api = env.get("GHES_API_URL", "").rstrip("/") or f"{base}/api/v3"
        graphql = env.get("GHES_GRAPHQL_URL", "").rstrip("/") or f"{base}/api/graphql"

        token = env.get("GHES_TOKEN", "")
        if not token:
            raise ConfigError("GHES_TOKEN is required")

        org = env.get("GHES_ORG", "").strip().strip("/")
        repos = _list(env, "GHES_REPOS")
        orgs = _list(env, "GHES_ORGS")
        if not repos and not orgs and not org:
            raise ConfigError("set GHES_REPOS, GHES_ORG, or GHES_ORGS")
        bare = [r for r in repos if "/" not in r]
        if bare and not org:
            raise ConfigError(f"GHES_REPOS entries without an owner need GHES_ORG: {bare}")

        project_keys = [k.upper() for k in _list(env, "JIRA_PROJECT_KEYS")]
        if project_keys:
            alt = "|".join(re.escape(k) for k in project_keys)
            issue_key_regex = rf"\b(?:{alt})-\d+\b"
        else:
            issue_key_regex = env.get("JIRA_ISSUE_KEY_REGEX", "") or DEFAULT_ISSUE_KEY_REGEX

        dry_run = _bool(env, "DRY_RUN", False)
        client_id = env.get("JIRA_OAUTH_CLIENT_ID", "")
        client_secret = env.get("JIRA_OAUTH_CLIENT_SECRET", "")
        cloud_id = env.get("JIRA_CLOUD_ID", "")
        site_url = env.get("JIRA_SITE_URL", "").rstrip("/")
        if not dry_run:
            if not client_id or not client_secret:
                raise ConfigError(
                    "JIRA_OAUTH_CLIENT_ID and JIRA_OAUTH_CLIENT_SECRET are required "
                    "unless DRY_RUN=true"
                )
            if not cloud_id and not site_url:
                raise ConfigError("set JIRA_CLOUD_ID or JIRA_SITE_URL")

        return cls(
            ghes_base_url=base,
            ghes_api_url=api,
            ghes_token=token,
            ghes_graphql_url=graphql,
            use_graphql=_bool(env, "GHES_USE_GRAPHQL", False),
            ghes_org=org,
            ghes_repos=repos,
            ghes_orgs=orgs,
            ghes_branch_exclude=_list(env, "GHES_BRANCH_EXCLUDE"),
            jira_client_id=client_id,
            jira_client_secret=client_secret,
            jira_cloud_id=cloud_id,
            jira_site_url=site_url,
            jira_token_url=env.get("JIRA_TOKEN_URL", "") or DEFAULT_TOKEN_URL,
            jira_api_base=(env.get("JIRA_API_BASE", "") or DEFAULT_API_BASE).rstrip("/"),
            issue_key_regex=issue_key_regex,
            interval_seconds=_int(env, "SYNC_INTERVAL_SECONDS", 0),
            lookback_days=_int(env, "SYNC_LOOKBACK_DAYS", 14),
            include_prs=_bool(env, "SYNC_INCLUDE_PRS", True),
            prevent_transitions=_bool(env, "SYNC_PREVENT_TRANSITIONS", True),
            keyed_branches_only=_bool(env, "SYNC_KEYED_BRANCHES_ONLY", False),
            default_branch_only=_bool(env, "SYNC_DEFAULT_BRANCH_ONLY", False),
            concurrency=max(1, _int(env, "SYNC_CONCURRENCY", 8)),
            push_chunk_size=_chunk(env, "SYNC_PUSH_CHUNK", 400),
            backfill_on_first_sight=_bool(env, "SYNC_BACKFILL_FIRST_SIGHT", True),
            send_issue_keys=_bool(env, "JIRA_SEND_ISSUE_KEYS", False),
            send_associations=_bool(env, "JIRA_SEND_ASSOCIATIONS", True),
            issue_key_cap=max(1, _int(env, "JIRA_ISSUE_KEY_CAP", 500)),
            log_entities=_bool(env, "SYNC_LOG_ENTITIES", False),
            state_path=env.get("STATE_PATH", "") or "/data/state.json",
            dry_run=dry_run,
            log_level=env.get("LOG_LEVEL", "") or "INFO",
            log_format=env.get("LOG_FORMAT", "") or "text",
            http_timeout=_float(env, "HTTP_TIMEOUT", 30.0),
            max_retries=_int(env, "MAX_RETRIES", 4),
        )
