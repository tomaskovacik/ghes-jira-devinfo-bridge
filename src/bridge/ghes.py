"""GitHub Enterprise Server REST client (read-only).

Contract consumed by :mod:`bridge.sync`. Implemented by agent A.

All methods talk to ``settings.ghes_api_url`` with a bearer ``settings.ghes_token``.
Every request must carry a timeout (``settings.http_timeout``) and retry on
429/5xx/connection errors with exponential backoff + jitter, honouring
``Retry-After`` and ``X-RateLimit-Reset``. Never mutate anything on GHES.
"""

from __future__ import annotations

import logging
import random
import time

import httpx

from bridge.config import Settings
from bridge.models import (
    PR_DECLINED,
    PR_MERGED,
    PR_OPEN,
    Author,
    Branch,
    BranchCommits,
    BranchScan,
    Commit,
    CompareResult,
    PullRequest,
    RepoMeta,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_API_VERSION = "2022-11-28"

# One page of branches (100), each with its head date and up to 100 commits since
# the cutoff. Commas separate fields so the query survives whitespace mangling.
_REFS_QUERY = (
    "query($owner:String!,$name:String!,$since:GitTimestamp!,$cursor:String){"
    "repository(owner:$owner,name:$name){url,"
    'refs(refPrefix:"refs/heads/",first:100,after:$cursor){'
    "pageInfo{hasNextPage,endCursor},"
    "nodes{name,target{... on Commit{oid,committedDate,"
    "history(since:$since,first:100){pageInfo{hasNextPage},"
    "nodes{oid,message,committedDate,url,author{name,email}}}}}}}}}"
)


class GhesError(RuntimeError):
    """Non-retryable GHES failure (auth, unexpected schema, exhausted retries)."""


def _backoff(attempt: int) -> float:
    """Exponential backoff (capped at 30s) plus a little jitter."""
    return min(2**attempt, 30) + random.uniform(0, 1)


def _should_retry(resp: httpx.Response) -> bool:
    ratelimited = resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0"
    return resp.status_code in _RETRYABLE_STATUS or ratelimited


class GhesClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        """``http`` is an optional ``httpx.Client`` for tests."""
        self._settings = settings
        self._owns_http = http is None
        if http is None:
            http = httpx.Client(
                base_url=settings.ghes_api_url,
                timeout=settings.http_timeout,
                headers={
                    "Authorization": f"Bearer {settings.ghes_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": _API_VERSION,
                    "User-Agent": settings.user_agent,
                },
            )
        self._http = http
        self._repo_html_url: dict[str, str] = {}

    # -- HTTP plumbing -----------------------------------------------------

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset is not None:
                try:
                    return max(0.0, float(reset) - time.time())
                except ValueError:
                    pass
        return _backoff(attempt)

    def _request(
        self, method: str, url: str, *, params: dict | None = None, json: object | None = None
    ) -> httpx.Response:
        retries = self._settings.max_retries
        last_error = "no response"
        for attempt in range(retries + 1):
            try:
                resp = self._http.request(method, url, params=params, json=json)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
                if attempt >= retries:
                    break
                time.sleep(_backoff(attempt))
                continue
            if attempt < retries and _should_retry(resp):
                time.sleep(self._retry_delay(resp, attempt))
                continue
            return resp
        raise GhesError(f"{method} {url} failed after {retries} retries ({last_error})")

    def _json_or_raise(self, resp: httpx.Response) -> object:
        if resp.status_code // 100 != 2:
            raise GhesError(f"{resp.request.method} {resp.request.url}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise GhesError(f"invalid JSON from {resp.request.url}: {exc}") from exc

    def _paginate(self, url: str, params: dict) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        next_params: dict | None = dict(params)
        while next_url is not None:
            resp = self._request("GET", next_url, params=next_params)
            data = self._json_or_raise(resp)
            if not isinstance(data, list):
                raise GhesError(f"expected a JSON array from {resp.request.url}")
            items.extend(data)
            link = resp.links.get("next")
            next_url = link["url"] if link else None
            next_params = None
        return items

    # -- helpers ---------------------------------------------------------

    def _html_url_for(self, full_name: str) -> str:
        cached = self._repo_html_url.get(full_name)
        if cached is not None:
            return cached
        meta = self.get_repo(full_name)
        url = meta.url if meta is not None else f"{self._settings.ghes_base_url}/{full_name}"
        self._repo_html_url[full_name] = url
        return url

    @staticmethod
    def _commit_from_node(node: dict, file_count: int | None) -> Commit:
        commit = node.get("commit") or {}
        author = commit.get("author") or {}
        if file_count is None:
            files = node.get("files")
            file_count = len(files) if isinstance(files, list) else 0
        return Commit(
            sha=node.get("sha", ""),
            message=commit.get("message", ""),
            author=Author(author.get("name", ""), author.get("email", "")),
            authored_date=author.get("date", ""),
            url=node.get("html_url", ""),
            file_count=file_count,
        )

    @staticmethod
    def _pr_from_node(node: dict) -> PullRequest:
        if node.get("state") == "open":
            state = PR_OPEN
        elif node.get("merged_at"):
            state = PR_MERGED
        else:
            state = PR_DECLINED
        user = node.get("user") or {}
        head = node.get("head") or {}
        base = node.get("base") or {}
        return PullRequest(
            number=node["number"],
            title=node.get("title", ""),
            state=state,
            url=node.get("html_url", ""),
            author=Author(user.get("login", ""), ""),
            source_branch=head.get("ref", ""),
            destination_branch=base.get("ref", ""),
            last_update=node.get("updated_at", ""),
            body=node.get("body") or "",
            comment_count=node.get("comments") or 0,
            review_count=0,
        )

    # -- public API ----------------------------------------------------

    def resolve_repos(self) -> list[str]:
        """Return the concrete ``["owner/repo", ...]`` list to sync.

        ``settings.ghes_repos`` entries are used verbatim when they contain an
        owner (``owner/repo``) and prefixed with ``settings.ghes_org`` otherwise.
        When no explicit repos are given, every non-archived repo under
        ``settings.ghes_org`` and each of ``settings.ghes_orgs`` is discovered.
        De-duplicated, order stable.
        """
        org = self._settings.ghes_org
        result: list[str] = []
        seen: set[str] = set()
        for name in self._settings.ghes_repos:
            full = name if "/" in name else f"{org}/{name}"
            if full not in seen:
                seen.add(full)
                result.append(full)

        discover = list(self._settings.ghes_orgs)
        if org and not self._settings.ghes_repos and org not in discover:
            discover.append(org)
        for discover_org in discover:
            path = f"/orgs/{discover_org}/repos"
            for repo in self._paginate(path, {"type": "all", "per_page": 100}):
                if repo.get("archived"):
                    continue
                full = repo.get("full_name")
                if full and full not in seen:
                    seen.add(full)
                    result.append(full)
        return result

    def get_repo(self, full_name: str) -> RepoMeta | None:
        """``GET /repos/{full_name}``. Return ``None`` on 403/404, raise on other errors."""
        resp = self._request("GET", f"/repos/{full_name}")
        if resp.status_code in (403, 404):
            return None
        data = self._json_or_raise(resp)
        if not isinstance(data, dict):
            raise GhesError(f"expected a JSON object from {resp.request.url}")
        html_url = data["html_url"]
        self._repo_html_url[full_name] = html_url
        return RepoMeta(
            full_name=data["full_name"],
            repo_id=str(data["id"]),
            url=html_url,
            default_branch=data["default_branch"],
        )

    def list_branches(self, full_name: str) -> list[Branch]:
        """``GET /repos/{full_name}/branches`` (paginated).

        The branch list only carries the head sha. ``last_commit`` is left
        minimal here; :mod:`bridge.sync` fills it in (from commits it already
        fetched, or via :meth:`head_commit`) only for branches it actually
        pushes.
        """
        repo_html_url = self._html_url_for(full_name)
        branches: list[Branch] = []
        for node in self._paginate(f"/repos/{full_name}/branches", {"per_page": 100}):
            name = node.get("name", "")
            commit = node.get("commit") or {}
            sha = commit.get("sha", "")
            branches.append(
                Branch(
                    name=name,
                    head_sha=sha,
                    url=f"{repo_html_url}/tree/{name}",
                    last_commit=Commit(
                        sha=sha,
                        message="",
                        author=Author("", ""),
                        authored_date="",
                        url=commit.get("html_url") or f"{repo_html_url}/commit/{sha}",
                    ),
                )
            )
        return branches

    # -- GraphQL branch scan -------------------------------------------------

    def graphql(self, query: str, variables: dict) -> dict:
        """POST to ``settings.ghes_graphql_url``. Raises :class:`GhesError` on
        transport failure or a non-empty ``errors`` array."""
        resp = self._request(
            "POST", self._settings.ghes_graphql_url, json={"query": query, "variables": variables}
        )
        if resp.status_code // 100 != 2:
            raise GhesError(f"graphql: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise GhesError(f"graphql: invalid JSON ({exc})") from exc
        if body.get("errors"):
            raise GhesError(f"graphql errors: {body['errors']}")
        return body.get("data") or {}

    @staticmethod
    def _gql_commit(node: dict) -> Commit:
        author = node.get("author") or {}
        return Commit(
            sha=node.get("oid", ""),
            message=node.get("message", ""),
            author=Author(author.get("name", ""), author.get("email", "")),
            authored_date=node.get("committedDate", ""),
            url=node.get("url", ""),
        )

    def active_branches(self, full_name: str, since_iso: str) -> BranchScan:
        """One GraphQL pass over every branch of ``full_name``.

        Returns:
          * ``active``: branches whose head commit is at/after ``since_iso``,
            each with the commits on it since that cutoff (oldest first);
          * ``heads``: ``{branch name -> head sha}`` for *every* branch, so the
            caller can still detect deletions.

        Independent of any trunk/default branch. ``history`` is capped at 100
        commits per branch since the cutoff; a branch that exceeds that is
        logged and its newest 100 are used.
        """
        owner, _, repo = full_name.partition("/")
        repo_html_url = f"{self._settings.ghes_base_url}/{full_name}"
        heads: dict[str, str] = {}
        active: list[BranchCommits] = []
        cursor: str | None = None

        while True:
            data = self.graphql(
                _REFS_QUERY,
                {"owner": owner, "name": repo, "since": since_iso, "cursor": cursor},
            )
            repository = data.get("repository") or {}
            repo_html_url = repository.get("url") or repo_html_url
            refs = repository.get("refs") or {}
            for node in refs.get("nodes") or []:
                target = node.get("target") or {}
                oid = target.get("oid")
                if not oid:
                    continue
                name = node.get("name", "")
                heads[name] = oid
                if (target.get("committedDate") or "") < since_iso:
                    continue
                history = target.get("history") or {}
                hnodes = history.get("nodes") or []
                if (history.get("pageInfo") or {}).get("hasNextPage"):
                    logger.warning(
                        "branch %s in %s has >100 commits since the cutoff; using newest 100",
                        name,
                        full_name,
                    )
                commits = [self._gql_commit(h) for h in reversed(hnodes)]
                last_commit = (
                    self._gql_commit(hnodes[0])
                    if hnodes
                    else Commit(
                        sha=oid,
                        message="",
                        author=Author("", ""),
                        authored_date=target.get("committedDate", ""),
                        url=f"{repo_html_url}/commit/{oid}",
                    )
                )
                branch = Branch(
                    name=name,
                    head_sha=oid,
                    url=f"{repo_html_url}/tree/{name}",
                    last_commit=last_commit,
                )
                active.append(BranchCommits(branch=branch, commits=commits))

            page = refs.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")

        return BranchScan(active=active, heads=heads)

    def head_commit(self, full_name: str, sha: str) -> Commit | None:
        """``GET /repos/{full_name}/commits/{sha}`` -> full :class:`Commit`, or None."""
        if not sha:
            return None
        resp = self._request("GET", f"/repos/{full_name}/commits/{sha}")
        if resp.status_code // 100 != 2:
            return None
        data = self._json_or_raise(resp)
        if not isinstance(data, dict):
            return None
        return self._commit_from_node(data, None)

    def compare(self, full_name: str, base: str, head: str) -> CompareResult:
        """``GET /repos/{full_name}/compare/{base}...{head}``.

        ``commits`` are those unique to ``head``, oldest first, mapped to
        :class:`bridge.models.Commit` with ``file_count`` from the compare
        ``files`` array when present. If the range exceeds the API cap, fall
        back to :meth:`commits_since` from the merge base.
        """
        resp = self._request("GET", f"/repos/{full_name}/compare/{base}...{head}")
        data = self._json_or_raise(resp)
        if not isinstance(data, dict):
            raise GhesError(f"expected a JSON object from {resp.request.url}")
        files = data.get("files")
        file_count = len(files) if isinstance(files, list) else 0
        raw_commits = data.get("commits") or []
        commits = [self._commit_from_node(node, file_count) for node in raw_commits]
        total = data.get("total_commits")
        if isinstance(total, int) and total > len(commits):
            logger.warning(
                "compare %s %s...%s truncated: got %d of %d commits (API cap)",
                full_name,
                base,
                head,
                len(commits),
                total,
            )
        merge_base = (data.get("merge_base_commit") or {}).get("sha", "")
        return CompareResult(
            status=data.get("status", ""),
            merge_base_sha=merge_base,
            commits=commits,
        )

    def commits_since(self, full_name: str, branch: str, since_iso: str) -> list[Commit]:
        """``GET /repos/{full_name}/commits?sha={branch}&since={since_iso}``.

        Paginated, returned oldest first.
        """
        raw = self._paginate(
            f"/repos/{full_name}/commits",
            {"sha": branch, "since": since_iso, "per_page": 100},
        )
        commits = [self._commit_from_node(node, None) for node in raw]
        commits.reverse()
        return commits

    def pull_requests_since(self, full_name: str, since_iso: str) -> list[PullRequest]:
        """``GET /repos/{full_name}/pulls?state=all&sort=updated&direction=desc`` (paginated).

        Stop paging once ``updated_at <= since_iso``. Map ``state``:
        open -> OPEN, merged_at set -> MERGED, closed & not merged -> DECLINED.
        """
        results: list[PullRequest] = []
        next_url: str | None = f"/repos/{full_name}/pulls"
        next_params: dict | None = {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
        }
        stop = False
        while next_url is not None and not stop:
            resp = self._request("GET", next_url, params=next_params)
            data = self._json_or_raise(resp)
            if not isinstance(data, list):
                raise GhesError(f"expected a JSON array from {resp.request.url}")
            for node in data:
                if node.get("updated_at", "") <= since_iso:
                    stop = True
                    break
                results.append(self._pr_from_node(node))
            link = resp.links.get("next")
            next_url = link["url"] if (link and not stop) else None
            next_params = None
        return results

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
