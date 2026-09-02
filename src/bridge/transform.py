"""Map :class:`bridge.models.RepoChanges` to a Jira devinfo bulk payload.

Pure functions, no I/O.

Reference: developer.atlassian.com/cloud/jira/software/rest/api-group-development-information
Endpoint body shape (one repository)::

    {
      "repositories": [{
        "id": "<repo_id>", "name": "org/repo", "url": "<repo web url>",
        "updateSequenceId": <int>,
        "commits": [{
          "id": "<sha>", "hash": "<sha>", "displayId": "<sha[:7]>",
          "message": "...", "issueKeys": ["ABC-1"],
          "associations": [{"associationType": "issueIdOrKeys", "values": ["ABC-1"]}],
          "author": {"name": "...", "email": "..."},
          "authorTimestamp": "<iso8601>", "url": "<commit web url>",
          "fileCount": 0, "flags": ["MERGE_COMMIT"], "updateSequenceId": <int>
        }],
        "branches": [{
          "id": "<escaped name>", "name": "<name>", "url": "<branch web url>",
          "issueKeys": [...], "associations": [...],
          "lastCommit": { <commit object> }, "updateSequenceId": <int>
        }],
        "pullRequests": [{
          "id": "<number>", "issueKeys": [...], "associations": [...],
          "status": "OPEN|MERGED|DECLINED", "title": "...", "url": "<pr web url>",
          "author": {...}, "sourceBranch": "...", "destinationBranch": "...",
          "lastUpdate": "<iso8601>", "commentCount": 0, "reviewers": [],
          "updateSequenceId": <int>
        }]
      }],
      "preventTransitions": <bool>,
      "operationType": "NORMAL" | "BACKFILL",
      "properties": {"repositoryId": "<repo_id>"},
      "providerMetadata": {"product": "ghes-jira-devinfo-bridge/<version>"}
    }

``updateSequenceId``: sampled once as ``base = int(time.time()*1000)`` per payload,
then a dense per-distinct-entity index (see :class:`_Usid`). A commit that appears
both as a standalone ``commits[]`` entry and as a ``branches[].lastCommit`` shares
one id -> one sequence id, so the two copies cannot race each other under Jira's
"replace only if strictly greater" rule.

Entities whose issue-key set is empty are omitted. Return ``None`` when nothing in
``changes`` carries an issue key (caller then skips the push).
"""

from __future__ import annotations

import re
import time

import bridge
from bridge.issuekeys import extract, extract_many
from bridge.models import Branch, Commit, PullRequest, RepoChanges

_ID_ALLOWED = re.compile(r"[A-Za-z0-9\-._~]")

_MERGE_FLAG = "MERGE_COMMIT"
_ASSOC_TYPE = "issueIdOrKeys"  # accepts issue keys or numeric ids; current form

# Field length caps (Jira devinfo schema; the server also truncates, we clip
# client-side to keep the payload small and predictable).
_CAP_MESSAGE = 1024
_CAP_URL = 2000
_CAP_NAME = 255
_CAP_BRANCH_NAME = 512
_CAP_ID = 1024


def branch_id(name: str) -> str:
    """Jira entity ids must match ``[A-Za-z0-9\\-._~]+``; branch names routinely
    contain ``/``. Map every other character to ``~<hex>`` so the id stays unique
    and stable (used both when pushing and when deleting a branch)."""
    escaped = "".join(c if _ID_ALLOWED.match(c) else f"~{ord(c):02x}" for c in name)
    return _clip(escaped, _CAP_ID)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


class _Usid:
    """Hands out ``updateSequenceId`` values: ``base + n`` for the n-th distinct
    ``(kind, ident)`` seen, so every entity gets a unique monotonic id and the two
    copies of a shared commit sha (standalone + ``branches[].lastCommit``) get the
    *same* id."""

    def __init__(self, base: int) -> None:
        self._base = base
        self._next = 0
        self._seen: dict[tuple[str, str], int] = {}

    def get(self, kind: str, ident: str) -> int:
        key = (kind, ident)
        if key not in self._seen:
            self._seen[key] = self._next
            self._next += 1
        return self._base + self._seen[key]


def _linkage(keys: list[str], *, send_issue_keys: bool, send_associations: bool) -> dict:
    """The issue-linkage fragment for an entity.

    Jira rejects a payload that carries BOTH ``issueKeys`` and ``associations`` on
    one entity ("issueKeys and associations are mutually exclusive"), so exactly
    one form is emitted. ``issueKeys`` is the default and what every shipping
    client uses; ``send_issue_keys=False`` switches to ``associations``
    (``issueIdOrKeys``). Never emits neither.
    """
    if send_issue_keys or not send_associations:
        return {"issueKeys": keys}
    return {"associations": [{"associationType": _ASSOC_TYPE, "values": keys}]}


def _author_obj(author) -> dict:
    return {"name": _clip(author.name, _CAP_NAME), "email": _clip(author.email, _CAP_NAME)}


def _commit_obj(
    commit: Commit,
    issue_keys: set[str],
    usid: _Usid,
    *,
    send_issue_keys: bool,
    send_associations: bool,
    key_cap: int,
) -> dict:
    keys = sorted(issue_keys)[:key_cap]
    obj = {
        "id": commit.sha,
        "hash": commit.sha,  # deprecated alias of id; harmless, some consumers still read it
        "displayId": commit.sha[:7],
        "message": _clip(commit.message, _CAP_MESSAGE),
        "author": _author_obj(commit.author),
        "authorTimestamp": commit.authored_date,
        "url": _clip(commit.url, _CAP_URL),
        "fileCount": commit.file_count,
        "updateSequenceId": usid.get("commit", commit.sha),
        **_linkage(keys, send_issue_keys=send_issue_keys, send_associations=send_associations),
    }
    if commit.is_merge:
        obj["flags"] = [_MERGE_FLAG]
    return obj


def _branch_obj(
    branch: Branch,
    issue_keys: set[str],
    usid: _Usid,
    pattern: re.Pattern[str],
    *,
    send_issue_keys: bool,
    send_associations: bool,
    key_cap: int,
) -> dict:
    last = branch.last_commit
    message = last.message if last else ""
    last_keys = extract(message, pattern) if message else set(issue_keys)

    if last is not None:
        # _commit_obj keys its updateSequenceId off the sha, so this lastCommit and
        # any standalone commits[] entry for the same sha resolve to one id.
        last_commit = _commit_obj(
            last,
            last_keys,
            usid,
            send_issue_keys=send_issue_keys,
            send_associations=send_associations,
            key_cap=key_cap,
        )
    else:
        keys = sorted(last_keys)[:key_cap]
        last_commit = {
            "id": branch.head_sha,
            "hash": branch.head_sha,
            "displayId": branch.head_sha[:7],
            "message": "",
            "author": {"name": "", "email": ""},
            "authorTimestamp": "",
            "url": _clip(branch.url, _CAP_URL),
            "fileCount": 0,
            "updateSequenceId": usid.get("commit", branch.head_sha),
            **_linkage(keys, send_issue_keys=send_issue_keys, send_associations=send_associations),
        }

    keys = sorted(issue_keys)[:key_cap]
    return {
        "id": branch_id(branch.name),
        "name": _clip(branch.name, _CAP_BRANCH_NAME),
        "url": _clip(branch.url, _CAP_URL),
        "lastCommit": last_commit,
        "updateSequenceId": usid.get("branch", branch_id(branch.name)),
        **_linkage(keys, send_issue_keys=send_issue_keys, send_associations=send_associations),
    }


def _pull_request_obj(
    pr: PullRequest,
    issue_keys: set[str],
    usid: _Usid,
    *,
    send_issue_keys: bool,
    send_associations: bool,
    key_cap: int,
) -> dict:
    keys = sorted(issue_keys)[:key_cap]
    return {
        "id": str(pr.number),
        "status": pr.state,
        "title": _clip(pr.title, _CAP_MESSAGE),
        "url": _clip(pr.url, _CAP_URL),
        "author": _author_obj(pr.author),
        "sourceBranch": _clip(pr.source_branch, _CAP_NAME),
        "destinationBranch": _clip(pr.destination_branch, _CAP_NAME),
        "lastUpdate": pr.last_update,
        "commentCount": pr.comment_count,
        "reviewers": [],
        "updateSequenceId": usid.get("pr", str(pr.number)),
        **_linkage(keys, send_issue_keys=send_issue_keys, send_associations=send_associations),
    }


def build_devinfo_payload(
    changes: RepoChanges,
    *,
    prevent_transitions: bool,
    operation_type: str = "NORMAL",
    properties: dict[str, str] | None = None,
    pattern: re.Pattern[str],
    send_issue_keys: bool = False,
    send_associations: bool = True,
    key_cap: int = 500,
) -> dict | None:
    base = int(time.time() * 1000)
    usid = _Usid(base)
    repo_usid = usid.get("repository", str(changes.repo_id))  # base + 0
    link_kw = {
        "send_issue_keys": send_issue_keys,
        "send_associations": send_associations,
        "key_cap": key_cap,
    }

    commits: list[dict] = []
    for commit in changes.commits:
        keys = extract(commit.message, pattern)
        if not keys:
            continue
        commits.append(_commit_obj(commit, keys, usid, **link_kw))

    branches: list[dict] = []
    for branch in changes.branches:
        last_message = branch.last_commit.message if branch.last_commit else ""
        keys = extract_many(pattern, branch.name, last_message)
        if not keys:
            continue
        branches.append(_branch_obj(branch, keys, usid, pattern, **link_kw))

    pull_requests: list[dict] = []
    for pr in changes.pull_requests:
        keys = extract_many(pattern, pr.title, pr.body, pr.source_branch)
        if not keys:
            continue
        pull_requests.append(_pull_request_obj(pr, keys, usid, **link_kw))

    if not commits and not branches and not pull_requests:
        return None

    repository: dict = {
        "id": changes.repo_id,
        "name": _clip(changes.full_name, _CAP_NAME),
        "url": _clip(changes.url, _CAP_URL),
        "updateSequenceId": repo_usid,
    }
    if commits:
        repository["commits"] = commits
    if branches:
        repository["branches"] = branches
    if pull_requests:
        repository["pullRequests"] = pull_requests

    payload = {
        "repositories": [repository],
        "preventTransitions": prevent_transitions,
        "operationType": operation_type,
        "providerMetadata": {"product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"},
    }
    if properties:
        payload["properties"] = properties
    return payload
