from __future__ import annotations

import bridge
from bridge.issuekeys import compile_pattern
from bridge.models import Author, Branch, Commit, PullRequest, RepoChanges
from bridge.transform import branch_id, build_devinfo_payload


def test_branch_id_sanitizes_disallowed_chars() -> None:
    assert branch_id("main") == "main"
    assert branch_id("feature/ABC-1") == "feature~2fABC-1"
    assert branch_id("release_1.2~3") == "release_1.2~3"
    assert branch_id("a b") == "a~20b"


PATTERN = compile_pattern(None)
AUTHOR = Author(name="Dev", email="dev@example.com")


def _commit(sha: str, message: str) -> Commit:
    return Commit(
        sha=sha,
        message=message,
        author=AUTHOR,
        authored_date="2026-01-01T00:00:00Z",
        url=f"https://ghe.example.com/octo/repo/commit/{sha}",
        file_count=3,
    )


def _changes(**kw) -> RepoChanges:
    base = {
        "full_name": "octo/repo",
        "repo_id": "42",
        "url": "https://ghe.example.com/octo/repo",
    }
    base.update(kw)
    return RepoChanges(**base)


def _build(changes: RepoChanges, *, prevent_transitions: bool = True) -> dict | None:
    return build_devinfo_payload(
        changes,
        prevent_transitions=prevent_transitions,
        update_sequence_id=1234,
        pattern=PATTERN,
    )


def test_commit_with_key_included_displayid_and_fields() -> None:
    sha = "abcdef1234567890"
    payload = _build(_changes(commits=[_commit(sha, "ABC-1 do a thing")]))
    assert payload is not None
    repo = payload["repositories"][0]
    assert repo["id"] == "42"
    assert repo["name"] == "octo/repo"
    commit = repo["commits"][0]
    assert commit["id"] == sha
    assert commit["hash"] == sha
    assert commit["displayId"] == "abcdef1"
    assert commit["issueKeys"] == ["ABC-1"]
    assert commit["author"] == {"name": "Dev", "email": "dev@example.com"}
    assert commit["authorTimestamp"] == "2026-01-01T00:00:00Z"
    assert commit["fileCount"] == 3
    assert commit["updateSequenceId"] == 1234
    assert "branches" not in repo
    assert "pullRequests" not in repo


def test_commit_without_key_skipped_returns_none() -> None:
    assert _build(_changes(commits=[_commit("deadbeef00", "no key here")])) is None


def test_branch_keys_from_name_and_last_commit() -> None:
    last = _commit("1111111aaaa", "PROJ-7 landed")
    branch = Branch(
        name="feature/ABC-9-widget",
        head_sha="1111111aaaa",
        url="https://ghe.example.com/octo/repo/tree/feature/ABC-9-widget",
        last_commit=last,
    )
    payload = _build(_changes(branches=[branch]))
    assert payload is not None
    b = payload["repositories"][0]["branches"][0]
    assert b["id"] == "feature~2fABC-9-widget"  # "/" is not allowed in a Jira entity id
    assert b["name"] == "feature/ABC-9-widget"
    assert set(b["issueKeys"]) == {"ABC-9", "PROJ-7"}
    assert b["lastCommit"]["issueKeys"] == ["PROJ-7"]
    assert b["lastCommit"]["displayId"] == "1111111"


def test_branch_last_commit_empty_message_inherits_branch_keys() -> None:
    last = _commit("2222222bbbb", "")
    branch = Branch(
        name="bugfix/ABC-3",
        head_sha="2222222bbbb",
        url="https://ghe.example.com/octo/repo/tree/bugfix/ABC-3",
        last_commit=last,
    )
    payload = _build(_changes(branches=[branch]))
    b = payload["repositories"][0]["branches"][0]
    assert b["issueKeys"] == ["ABC-3"]
    assert b["lastCommit"]["issueKeys"] == ["ABC-3"]


def test_branch_without_keys_skipped() -> None:
    branch = Branch(
        name="chore/cleanup",
        head_sha="3333333cccc",
        url="https://ghe.example.com/octo/repo/tree/chore/cleanup",
        last_commit=_commit("3333333cccc", "just tidying"),
    )
    assert _build(_changes(branches=[branch])) is None


def _pr(**kw) -> PullRequest:
    base = {
        "number": 5,
        "title": "ABC-2 add feature",
        "state": "OPEN",
        "url": "https://ghe.example.com/octo/repo/pull/5",
        "author": AUTHOR,
        "source_branch": "feature/x",
        "destination_branch": "main",
        "last_update": "2026-02-02T12:00:00Z",
        "body": "",
        "comment_count": 4,
    }
    base.update(kw)
    return PullRequest(**base)


def test_pull_request_with_key() -> None:
    payload = _build(_changes(pull_requests=[_pr()]))
    assert payload is not None
    pr = payload["repositories"][0]["pullRequests"][0]
    assert pr["id"] == "5"
    assert pr["issueKeys"] == ["ABC-2"]
    assert pr["status"] == "OPEN"
    assert pr["sourceBranch"] == "feature/x"
    assert pr["destinationBranch"] == "main"
    assert pr["lastUpdate"] == "2026-02-02T12:00:00Z"
    assert pr["commentCount"] == 4
    assert pr["reviewers"] == []


def test_pull_request_key_from_body_or_source_branch() -> None:
    pr = _pr(title="no key", body="relates to DEF-1", source_branch="hotfix/GHI-2")
    payload = _build(_changes(pull_requests=[pr]))
    pr_obj = payload["repositories"][0]["pullRequests"][0]
    assert set(pr_obj["issueKeys"]) == {"DEF-1", "GHI-2"}


def test_pull_request_without_key_skipped() -> None:
    assert _build(_changes(pull_requests=[_pr(title="nothing", source_branch="x")])) is None


def test_none_when_nothing_matches() -> None:
    assert _build(_changes()) is None


def test_provider_metadata_and_prevent_transitions_passthrough() -> None:
    payload = _build(
        _changes(commits=[_commit("abcdef1234", "ABC-1 x")]),
        prevent_transitions=False,
    )
    assert payload["preventTransitions"] is False
    assert payload["providerMetadata"] == {
        "product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"
    }


def test_deleted_branch_names_ignored_here() -> None:
    changes = _changes(deleted_branch_names=["ABC-1-old"])
    assert _build(changes) is None
