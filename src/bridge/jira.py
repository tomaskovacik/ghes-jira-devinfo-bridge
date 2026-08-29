"""Jira Cloud development-information client (write-only).

Contract consumed by :mod:`bridge.sync`. Implemented by agent B.

Auth: OAuth 2.0 ``client_credentials`` (2LO) against ``settings.jira_token_url``
with ``audience=api.atlassian.com``. Cache the access token and refresh ~60s
before expiry. All calls carry a timeout and retry on 429/5xx.
"""

from __future__ import annotations

import contextlib
import logging
import random
import time

import httpx

from bridge.config import Settings
from bridge.models import DevinfoResult

logger = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_BACKOFF = 30


class JiraError(RuntimeError):
    """Non-retryable Jira failure (auth, 4xx other than 429, exhausted retries)."""


def _ids_from(node) -> list[str]:
    """Defensively pull entity ids out of an ``acceptedDevinfoEntities`` /
    ``failedDevinfoEntities`` value, which may be a mapping keyed by repository
    or a flat list."""
    if node is None:
        return []
    if isinstance(node, list):
        out: list[str] = []
        for item in node:
            if isinstance(item, dict):
                if "id" in item:
                    out.append(str(item["id"]))
                else:
                    out.extend(_ids_from(list(item.values())))
            elif isinstance(item, list):
                out.extend(_ids_from(item))
            else:
                out.append(str(item))
        return out
    if isinstance(node, dict):
        out = []
        for value in node.values():
            out.extend(_ids_from(value))
        return out
    return [str(node)]


class JiraClient:
    def __init__(self, settings: Settings, http=None) -> None:
        """``http`` is an optional ``httpx.Client`` for tests."""
        self._settings = settings
        if http is None:
            self._http = httpx.Client(
                timeout=settings.http_timeout,
                headers={"User-Agent": settings.user_agent},
            )
            self._owns_http = True
        else:
            self._http = http
            self._owns_http = False
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._cloud_id: str | None = None

    # -- HTTP with retry ---------------------------------------------------

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = float(min(2**attempt, _MAX_BACKOFF))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, float(retry_after))
        time.sleep(delay + random.uniform(0, 1))

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._settings.max_retries:
                    raise JiraError(f"{method} {url}: transport error: {exc}") from exc
                self._sleep_backoff(attempt, None)
                attempt += 1
                continue
            if resp.status_code in _RETRY_STATUS or resp.status_code >= 500:
                if attempt >= self._settings.max_retries:
                    raise JiraError(
                        f"{method} {url}: giving up after {attempt} retries "
                        f"(last status {resp.status_code})"
                    )
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            return resp

    # -- auth ------------------------------------------------------------

    def _get_token(self) -> str:
        now = time.time()
        if self._token is not None and now < self._token_exp - 60:
            return self._token
        resp = self._request(
            "POST",
            self._settings.jira_token_url,
            json={
                "audience": "api.atlassian.com",
                "grant_type": "client_credentials",
                "client_id": self._settings.jira_client_id,
                "client_secret": self._settings.jira_client_secret,
            },
        )
        if resp.status_code // 100 != 2:
            raise JiraError(f"token request failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 0))
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # -- public API ----------------------------------------------------

    def cloud_id(self) -> str:
        """Return the Jira cloud id.

        ``settings.jira_cloud_id`` if set, else resolve once via
        ``GET {settings.jira_site_url}/_edge/tenant_info`` and cache it.
        """
        if self._settings.jira_cloud_id:
            return self._settings.jira_cloud_id
        if self._cloud_id:
            return self._cloud_id
        if not self._settings.jira_site_url:
            raise JiraError("neither jira_cloud_id nor jira_site_url is configured")
        resp = self._request("GET", f"{self._settings.jira_site_url}/_edge/tenant_info")
        if resp.status_code // 100 != 2:
            raise JiraError(f"tenant_info failed: {resp.status_code} {resp.text}")
        try:
            self._cloud_id = resp.json()["cloudId"]
        except (KeyError, ValueError) as exc:
            raise JiraError(f"tenant_info response missing cloudId: {exc}") from exc
        return self._cloud_id

    def push(self, payload: dict) -> DevinfoResult:
        """``POST {jira_api_base}/jira/devinfo/0.1/cloud/{cloud_id}/bulk``.

        ``payload`` is a full devinfo bulk body (see :mod:`bridge.transform`).
        Parse ``acceptedDevinfoEntities`` / ``unknownIssueKeys`` /
        ``unknownAssociations`` / ``failedDevinfoEntities`` into
        :class:`bridge.models.DevinfoResult`. Raise :class:`JiraError` on non-2xx.
        """
        url = f"{self._settings.jira_api_base}/jira/devinfo/0.1/cloud/{self.cloud_id()}/bulk"
        resp = self._request("POST", url, headers=self._auth_headers(), json=payload)
        if resp.status_code not in (200, 202):
            raise JiraError(f"devinfo push failed: {resp.status_code} {resp.text}")
        body = resp.json()
        if not isinstance(body, dict):
            return DevinfoResult()
        return DevinfoResult(
            accepted_devinfo_keys=_ids_from(body.get("acceptedDevinfoEntities")),
            unknown_issue_keys=[str(k) for k in body.get("unknownIssueKeys") or []],
            unknown_associations=[str(a) for a in body.get("unknownAssociations") or []],
            failed_devinfo_keys=_ids_from(body.get("failedDevinfoEntities")),
        )

    def get_repository(self, repo_id: str) -> dict:
        """``GET .../repository/{repo_id}`` -> the stored devinfo for a repo.

        Returns ``{}`` if Jira has nothing for that id. Read-back works with the
        same write-only credential.
        """
        url = (
            f"{self._settings.jira_api_base}/jira/devinfo/0.1/cloud/"
            f"{self.cloud_id()}/repository/{repo_id}"
        )
        resp = self._request("GET", url, headers=self._auth_headers())
        if resp.status_code == 404:
            return {}
        if resp.status_code // 100 != 2:
            raise JiraError(f"get repository failed: {resp.status_code} {resp.text}")
        body = resp.json()
        return body if isinstance(body, dict) else {}

    def delete_repository(self, repo_id: str) -> None:
        """``DELETE .../repository/{repo_id}`` -> purge all devinfo for a repo.

        Recovery for a repo whose async processing is wedged. No-op in dry-run.
        """
        if self._settings.dry_run:
            logger.info("dry-run: skipping repository delete %s", repo_id)
            return
        url = (
            f"{self._settings.jira_api_base}/jira/devinfo/0.1/cloud/"
            f"{self.cloud_id()}/repository/{repo_id}"
        )
        resp = self._request("DELETE", url, headers=self._auth_headers())
        if resp.status_code not in (202, 204):
            raise JiraError(f"repository delete failed: {resp.status_code} {resp.text}")

    def delete_branch(self, repo_id: str, branch_name: str) -> None:
        """Remove one branch entity.

        ``DELETE {jira_api_base}/jira/devinfo/0.1/cloud/{cloud_id}/bulkByProperties``
        with query params selecting this repo + branch ref. No-op in dry-run.
        """
        if self._settings.dry_run:
            logger.info("dry-run: skipping branch delete %s@%s", branch_name, repo_id)
            return
        url = (
            f"{self._settings.jira_api_base}/jira/devinfo/0.1/cloud/"
            f"{self.cloud_id()}/bulkByProperties"
        )
        resp = self._request(
            "DELETE",
            url,
            headers=self._auth_headers(),
            params={"repositoryId": repo_id, "branchId": branch_name},
        )
        if resp.status_code not in (202, 204):
            raise JiraError(f"branch delete failed: {resp.status_code} {resp.text}")

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
