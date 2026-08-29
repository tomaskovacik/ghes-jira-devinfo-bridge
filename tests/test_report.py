from __future__ import annotations

from bridge.report import repo_lines


def test_repo_lines_renders_commits_branches_prs() -> None:
    repo = {
        "id": "2484",
        "name": "octo/db",
        "commits": [
            {
                "id": "a" * 40,
                "authorTimestamp": "2026-08-14T12:37:06Z",
                "issueKeys": ["JRA-4136"],
                "message": "JRA-4136 hotfix\n\nlong body ignored",
            },
            {"id": "b" * 40, "authorTimestamp": "", "issueKeys": [], "message": ""},
        ],
        "branches": [{"id": "release-1", "issueKeys": ["JRA-4136"]}],
        "pullRequests": [{"id": "7", "status": "MERGED", "issueKeys": ["JRA-4136"]}],
    }
    lines = repo_lines(repo)
    text = "\n".join(lines)

    assert lines[0] == "repo: octo/db (id 2484)"
    assert "commits in dev-info for repo octo/db: 2" in text
    assert "a" * 40 in text and "JRA-4136 hotfix" in text and "long body" not in text
    assert "b" * 40 in text  # keyless commit still listed with "-"
    assert "branches: 1" in text and "release-1  JRA-4136" in text
    assert "pull requests: 1" in text and "#7  MERGED  JRA-4136" in text


def test_repo_lines_handles_missing_keys() -> None:
    lines = repo_lines({"name": "x/y"})
    assert lines == [
        "repo: x/y (id ?)",
        "commits in dev-info for repo x/y: 0",
        "branches: 0",
        "pull requests: 0",
    ]
