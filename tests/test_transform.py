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


def _commit(sha: str, message: str, *, is_merge: bool = False) -> Commit:
    return Commit(
        sha=sha,
        message=message,
        author=AUTHOR,
        authored_date="2026-01-01T00:00:00Z",
        url=f"https://ghe.example.com/octo/repo/commit/{sha}",
        file_count=3,
        is_merge=is_merge,
    )


def _changes(**kw) -> RepoChanges:
    base = {
        "full_name": "octo/repo",
        "repo_id": "42",
        "url": "https://ghe.example.com/octo/repo",
    }
    base.update(kw)
    return RepoChanges(**base)


def _build(changes: RepoChanges, *, prevent_transitions: bool = True, **kw) -> dict | None:
    # Most tests here assert on issueKeys because it's the simplest linkage form
    # to check; the production default is associations (see the linkage tests).
    kw.setdefault("send_issue_keys", True)
    return build_devinfo_payload(
        changes,
        prevent_transitions=prevent_transitions,
        pattern=PATTERN,
        **kw,
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
    # repo gets base+0, the one commit gets base+1
    assert commit["updateSequenceId"] == repo["updateSequenceId"] + 1
    assert "flags" not in commit
    assert "branches" not in repo
    assert "pullRequests" not in repo


def test_commit_without_key_skipped_returns_none() -> None:
    assert _build(_changes(commits=[_commit("deadbeef00", "no key here")])) is None


def test_merge_commit_gets_flag() -> None:
    payload = _build(_changes(commits=[_commit("abc123", "ABC-1 merge", is_merge=True)]))
    assert payload["repositories"][0]["commits"][0]["flags"] == ["MERGE_COMMIT"]


def _commit0(payload: dict) -> dict:
    return payload["repositories"][0]["commits"][0]


def test_linkage_defaults_to_associations_only() -> None:
    # issueKeys is DEPRECATED in the Cloud API; issueKeys and associations are
    # mutually exclusive on one entity (Jira 400s a payload with both).
    payload = build_devinfo_payload(
        _changes(commits=[_commit("abcdef1234", "ABC-1 x")]),
        prevent_transitions=True,
        pattern=PATTERN,
    )
    commit = _commit0(payload)
    assert commit["associations"] == [{"associationType": "issueIdOrKeys", "values": ["ABC-1"]}]
    assert "issueKeys" not in commit


def test_linkage_can_use_deprecated_issue_keys_never_both() -> None:
    c = _changes(commits=[_commit("abcdef1234", "ABC-1 x")])

    keys = _commit0(_build(c, send_issue_keys=True))
    assert keys["issueKeys"] == ["ABC-1"] and "associations" not in keys

    # both off -> issueKeys fallback, never nothing
    both_off = _commit0(_build(c, send_issue_keys=False, send_associations=False))
    assert both_off["issueKeys"] == ["ABC-1"] and "associations" not in both_off


def test_issue_key_cap_truncates_both_forms() -> None:
    msg = "start " + " ".join(f"ABC-{i}" for i in range(600))
    changes = _changes(commits=[_commit("abcdef1234", msg)])
    assoc_commit = _commit0(_build(changes, send_issue_keys=False, key_cap=500))
    assert len(assoc_commit["associations"][0]["values"]) == 500
    keys_commit = _commit0(_build(changes, send_issue_keys=True, key_cap=500))
    assert len(keys_commit["issueKeys"]) == 500


def test_field_length_caps_applied() -> None:
    long_msg = "ABC-1 " + "x" * 5000
    c = _commit("abcdef1234", long_msg)
    c = Commit(**{**c.__dict__, "url": "https://ghe.example.com/" + "u" * 5000})
    payload = _build(_changes(commits=[c]))
    commit = payload["repositories"][0]["commits"][0]
    assert len(commit["message"]) == 1024
    assert len(commit["url"]) == 2000


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


def test_shared_sha_gets_one_update_sequence_id() -> None:
    """A commit that is both a standalone commits[] entry and a branch's
    lastCommit must carry the SAME updateSequenceId in both places, or Jira's
    'replace only if strictly greater' rule drops one copy."""
    sha = "beefbeef0001"
    last = _commit(sha, "ABC-9 head commit")
    branch = Branch(
        name="feature/ABC-9",
        head_sha=sha,
        url="https://ghe.example.com/octo/repo/tree/feature/ABC-9",
        last_commit=last,
    )
    payload = _build(_changes(commits=[last], branches=[branch]))
    repo = payload["repositories"][0]
    standalone = repo["commits"][0]
    nested = repo["branches"][0]["lastCommit"]
    assert standalone["id"] == nested["id"] == sha
    assert standalone["updateSequenceId"] == nested["updateSequenceId"]
    # branch entity itself is a different id -> different usid
    assert repo["branches"][0]["updateSequenceId"] != standalone["updateSequenceId"]


def test_update_sequence_ids_are_distinct_and_monotonic() -> None:
    payload = _build(
        _changes(
            commits=[_commit("aaa1", "ABC-1 a"), _commit("bbb2", "ABC-2 b")],
        )
    )
    repo = payload["repositories"][0]
    ids = [repo["updateSequenceId"], *(c["updateSequenceId"] for c in repo["commits"])]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


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
    assert "associations" not in pr
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


def test_envelope_fields() -> None:
    payload = _build(
        _changes(commits=[_commit("abcdef1234", "ABC-1 x")]),
        prevent_transitions=False,
        operation_type="BACKFILL",
        properties={"repositoryId": "42"},
    )
    assert payload["preventTransitions"] is False
    assert payload["operationType"] == "BACKFILL"
    assert payload["properties"] == {"repositoryId": "42"}
    assert payload["providerMetadata"] == {
        "product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"
    }


def test_operation_type_defaults_to_normal_and_properties_optional() -> None:
    payload = _build(_changes(commits=[_commit("abcdef1234", "ABC-1 x")]))
    assert payload["operationType"] == "NORMAL"
    assert "properties" not in payload


def test_deleted_branch_names_ignored_here() -> None:
    changes = _changes(deleted_branch_names=["ABC-1-old"])
    assert _build(changes) is None
