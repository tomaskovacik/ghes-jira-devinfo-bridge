"""Map :class:`bridge.models.RepoChanges` to a Jira devinfo bulk payload.

Implemented by agent B. Pure function, no I/O.

Reference schema: developer.atlassian.com/cloud/jira/software/rest/api-group-development-information
Endpoint body shape (one repository):

    {
      "repositories": [{
        "id": "<repo_id>",
        "name": "org/repo",
        "url": "<repo web url>",
        "commits": [{
          "id": "<sha>", "hash": "<sha>", "displayId": "<sha[:7]>",
          "message": "...", "issueKeys": ["ABC-1"],
          "author": {"name": "...", "email": "..."},
          "authorTimestamp": "<iso8601>", "url": "<commit web url>",
          "fileCount": 0, "updateSequenceId": <int>
        }],
        "branches": [{
          "id": "<name>", "name": "<name>", "issueKeys": ["ABC-1"],
          "url": "<branch web url>",
          "lastCommit": { <commit object, issueKeys required> },
          "updateSequenceId": <int>
        }],
        "pullRequests": [{
          "id": "<number>", "issueKeys": ["ABC-1"], "status": "OPEN|MERGED|DECLINED",
          "title": "...", "url": "<pr web url>",
          "author": {"name": "...", "email": "..."},
          "sourceBranch": "...", "destinationBranch": "...",
          "lastUpdate": "<iso8601>", "commentCount": 0, "reviewers": [],
          "updateSequenceId": <int>
        }],
        "updateSequenceId": <int>
      }],
      "preventTransitions": <bool>,
      "providerMetadata": {"product": "ghes-jira-devinfo-bridge/<version>"}
    }

Entities whose issue-key set is empty are omitted. Return ``None`` when nothing
in ``changes`` carries an issue key (caller then skips the push).
"""

from __future__ import annotations

import re

import bridge
from bridge.issuekeys import extract, extract_many
from bridge.models import Branch, Commit, PullRequest, RepoChanges

_ID_ALLOWED = re.compile(r"[A-Za-z0-9\-._~]")


def branch_id(name: str) -> str:
    """Jira entity ids must match ``[A-Za-z0-9\\-._~]+``; branch names routinely
    contain ``/``. Map every other character to ``~<hex>`` so the id stays
    unique and stable (used both when pushing and when deleting a branch)."""
    return "".join(c if _ID_ALLOWED.match(c) else f"~{ord(c):02x}" for c in name)


def _author_obj(author) -> dict:
    return {"name": author.name, "email": author.email}


def _commit_obj(commit: Commit, issue_keys: set[str], update_sequence_id: int) -> dict:
    return {
        "id": commit.sha,
        "hash": commit.sha,
        "displayId": commit.sha[:7],
        "message": commit.message,
        "author": _author_obj(commit.author),
        "authorTimestamp": commit.authored_date,
        "url": commit.url,
        "fileCount": commit.file_count,
        "issueKeys": sorted(issue_keys),
        "updateSequenceId": update_sequence_id,
    }


def _branch_obj(
    branch: Branch,
    issue_keys: set[str],
    update_sequence_id: int,
    pattern: re.Pattern[str],
) -> dict:
    last = branch.last_commit
    message = last.message if last else ""
    last_keys = extract(message, pattern) if message else set(issue_keys)

    if last is not None:
        last_commit = _commit_obj(last, last_keys, update_sequence_id)
    else:
        last_commit = {
            "id": branch.head_sha,
            "hash": branch.head_sha,
            "displayId": branch.head_sha[:7],
            "message": "",
            "author": {"name": "", "email": ""},
            "authorTimestamp": "",
            "url": branch.url,
            "fileCount": 0,
            "issueKeys": sorted(last_keys),
            "updateSequenceId": update_sequence_id,
        }

    return {
        "id": branch_id(branch.name),
        "name": branch.name,
        "url": branch.url,
        "issueKeys": sorted(issue_keys),
        "lastCommit": last_commit,
        "updateSequenceId": update_sequence_id,
    }


def _pull_request_obj(pr: PullRequest, issue_keys: set[str], update_sequence_id: int) -> dict:
    return {
        "id": str(pr.number),
        "issueKeys": sorted(issue_keys),
        "status": pr.state,
        "title": pr.title,
        "url": pr.url,
        "author": _author_obj(pr.author),
        "sourceBranch": pr.source_branch,
        "destinationBranch": pr.destination_branch,
        "lastUpdate": pr.last_update,
        "commentCount": pr.comment_count,
        "reviewers": [],
        "updateSequenceId": update_sequence_id,
    }


def build_devinfo_payload(
    changes: RepoChanges,
    *,
    prevent_transitions: bool,
    update_sequence_id: int,
    pattern: re.Pattern[str],
) -> dict | None:
    commits: list[dict] = []
    for commit in changes.commits:
        keys = extract(commit.message, pattern)
        if not keys:
            continue
        commits.append(_commit_obj(commit, keys, update_sequence_id))

    branches: list[dict] = []
    for branch in changes.branches:
        last_message = branch.last_commit.message if branch.last_commit else ""
        keys = extract_many(pattern, branch.name, last_message)
        if not keys:
            continue
        branches.append(_branch_obj(branch, keys, update_sequence_id, pattern))

    pull_requests: list[dict] = []
    for pr in changes.pull_requests:
        keys = extract_many(pattern, pr.title, pr.body, pr.source_branch)
        if not keys:
            continue
        pull_requests.append(_pull_request_obj(pr, keys, update_sequence_id))

    if not commits and not branches and not pull_requests:
        return None

    repository: dict = {
        "id": changes.repo_id,
        "name": changes.full_name,
        "url": changes.url,
        "updateSequenceId": update_sequence_id,
    }
    if commits:
        repository["commits"] = commits
    if branches:
        repository["branches"] = branches
    if pull_requests:
        repository["pullRequests"] = pull_requests

    return {
        "repositories": [repository],
        "preventTransitions": prevent_transitions,
        "providerMetadata": {"product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"},
    }
