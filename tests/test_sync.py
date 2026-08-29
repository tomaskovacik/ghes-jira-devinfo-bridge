from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bridge import sync
from bridge.config import Settings
from bridge.models import (
    COMPARE_AHEAD,
    PR_OPEN,
    Author,
    Branch,
    BranchCommits,
    BranchScan,
    Commit,
    CompareResult,
    DevinfoResult,
    PullRequest,
    RepoMeta,
)
from bridge.state import State

AUTHOR = Author(name="Dev", email="dev@example.com")


def _commit(sha: str) -> Commit:
    return Commit(
        sha=sha,
        message=f"work {sha} ABC-1",
        author=AUTHOR,
        authored_date="2026-08-20T00:00:00Z",
        url=f"https://ghe.example.com/octo/repo/commit/{sha}",
    )


def _branch(name: str, head: str) -> Branch:
    return Branch(name=name, head_sha=head, url=f"https://ghe.example.com/octo/repo/tree/{name}")


def _meta(name: str = "octo/repo", repo_id: str = "1000") -> RepoMeta:
    return RepoMeta(
        full_name=name,
        repo_id=repo_id,
        url=f"https://ghe.example.com/{name}",
        default_branch="main",
    )


class FakeGhes:
    def __init__(
        self,
        *,
        repos: list[str],
        metas: dict[str, RepoMeta | None],
        branches: dict[str, list[Branch]],
        commits: dict[tuple[str, str], list[Commit]] | None = None,
        compares: dict[tuple[str, str, str], CompareResult] | None = None,
        prs: dict[str, list[PullRequest]] | None = None,
        get_repo_errors: dict[str, Exception] | None = None,
        head_commits: dict[tuple[str, str], Commit] | None = None,
        scans: dict[str, BranchScan] | None = None,
    ) -> None:
        self._repos = repos
        self._metas = metas
        self._branches = branches
        self._commits = commits or {}
        self._compares = compares or {}
        self._prs = prs or {}
        self._get_repo_errors = get_repo_errors or {}
        self._head_commits = head_commits or {}
        self._scans = scans or {}
        self.closed = False
        self.commits_since_calls: list[tuple[str, str, str]] = []
        self.compare_calls: list[tuple[str, str, str]] = []
        self.pr_since_calls: list[tuple[str, str]] = []
        self.head_commit_calls: list[tuple[str, str]] = []
        self.active_branches_calls: list[tuple[str, str]] = []

    def resolve_repos(self) -> list[str]:
        return list(self._repos)

    def get_repo(self, full_name: str) -> RepoMeta | None:
        if full_name in self._get_repo_errors:
            raise self._get_repo_errors[full_name]
        return self._metas.get(full_name)

    def list_branches(self, full_name: str) -> list[Branch]:
        return list(self._branches.get(full_name, []))

    def compare(self, full_name: str, base: str, head: str) -> CompareResult:
        self.compare_calls.append((full_name, base, head))
        if (full_name, base, head) in self._compares:
            return self._compares[(full_name, base, head)]
        return CompareResult(status="ahead", merge_base_sha=base, commits=[])

    def commits_since(self, full_name: str, branch: str, since_iso: str) -> list[Commit]:
        self.commits_since_calls.append((full_name, branch, since_iso))
        return list(self._commits.get((full_name, branch), []))

    def pull_requests_since(self, full_name: str, since_iso: str) -> list[PullRequest]:
        self.pr_since_calls.append((full_name, since_iso))
        return list(self._prs.get(full_name, []))

    def head_commit(self, full_name: str, sha: str) -> Commit | None:
        self.head_commit_calls.append((full_name, sha))
        return self._head_commits.get((full_name, sha))

    def active_branches(self, full_name: str, since_iso: str) -> BranchScan:
        self.active_branches_calls.append((full_name, since_iso))
        return self._scans.get(full_name, BranchScan(active=[], heads={}))

    def close(self) -> None:
        self.closed = True


class FakeJira:
    def __init__(self, *, result: DevinfoResult | None = None, push_error: Exception | None = None):
        self._result = result or DevinfoResult()
        self._push_error = push_error
        self.pushed: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.closed = False

    def push(self, payload: dict) -> DevinfoResult:
        if self._push_error is not None:
            raise self._push_error
        self.pushed.append(payload)
        return self._result

    def delete_branch(self, repo_id: str, branch_name: str) -> None:
        self.deleted.append((repo_id, branch_name))

    def close(self) -> None:
        self.closed = True


def _fake_build(changes, *, prevent_transitions, update_sequence_id, pattern):
    """Stand-in for the (not-yet-implemented) transform layer."""
    if changes.is_empty():
        return None
    return {
        "repositories": [{"id": changes.repo_id, "name": changes.full_name}],
        "preventTransitions": prevent_transitions,
        "updateSequenceId": update_sequence_id,
    }


@pytest.fixture(autouse=True)
def _patch_transform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.transform, "build_devinfo_payload", _fake_build)


@pytest.fixture
def cfg(settings: Settings, tmp_path: Path) -> Settings:
    return replace(settings, state_path=str(tmp_path / "state.json"), lookback_days=7)


def _saved(cfg: Settings) -> dict:
    return json.loads(Path(cfg.state_path).read_text())


def test_brand_new_repo_walks_default_and_compares_the_rest(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1"), _branch("feature/x", "sha2")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
        compares={
            ("octo/repo", "main", "sha2"): CompareResult(
                status=COMPARE_AHEAD, merge_base_sha="mb", commits=[_commit("c2"), _commit("c3")]
            ),
        },
    )
    jira = FakeJira()
    state = State()

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert summary.repos_pushed == 1
    assert summary.branches == 2
    assert summary.commits == 3  # c1 from the default-branch walk, c2/c3 from the compare
    assert len(jira.pushed) == 1
    # only the default branch is walked; every other branch is a compare-vs-default
    assert [c[1] for c in ghes.commits_since_calls] == ["main"]
    assert ("octo/repo", "main", "sha2") in ghes.compare_calls

    saved = _saved(cfg)
    assert saved["repos"]["octo/repo"]["branches"] == {"main": "sha1", "feature/x": "sha2"}
    assert saved["repos"]["octo/repo"]["last_success"].endswith("Z")


def test_branch_exclude_globs_skip_branches(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={
            "octo/repo": [
                _branch("main", "sha1"),
                _branch("renovate/lodash", "sha2"),
                _branch("feature/ABC-1", "sha3"),
            ]
        },
    )
    state = State()
    cfg2 = replace(cfg, ghes_branch_exclude=["renovate/*"])

    jira = FakeJira()
    sync.run_once(cfg2, ghes=ghes, jira=jira, state=state)

    touched = {c[1] for c in ghes.commits_since_calls} | {c[2] for c in ghes.compare_calls}
    assert "sha2" not in touched  # renovate/lodash head never fetched
    assert [c[1] for c in ghes.commits_since_calls] == ["main"]
    assert ("octo/repo", "main", "sha3") in ghes.compare_calls
    # excluded branch: never processed -> not in state, and not treated as deleted
    saved = _saved(cfg2)["repos"]["octo/repo"]["branches"]
    assert set(saved) == {"main", "feature/ABC-1"}
    assert jira.deleted == []


def test_keyed_branches_only_keeps_default_and_keyed(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={
            "octo/repo": [
                _branch("main", "sha1"),
                _branch("spike-no-key", "sha2"),
                _branch("bugfix/ABC-9", "sha3"),
            ]
        },
    )
    cfg2 = replace(cfg, keyed_branches_only=True)

    sync.run_once(cfg2, ghes=ghes, jira=FakeJira(), state=State())

    assert [c[1] for c in ghes.commits_since_calls] == ["main"]
    compared_heads = {c[2] for c in ghes.compare_calls}
    assert "sha3" in compared_heads  # bugfix/ABC-9 processed
    assert "sha2" not in compared_heads  # spike-no-key skipped


def test_graphql_mode_pushes_changed_active_branches_only(cfg: Settings) -> None:
    b_changed = Branch(name="release-2", head_sha="new", url="u", last_commit=_commit("h2"))
    b_same = Branch(name="feature/x", head_sha="same", url="u", last_commit=_commit("h1"))
    scan = BranchScan(
        active=[
            BranchCommits(branch=b_changed, commits=[_commit("h1"), _commit("h2")]),
            BranchCommits(branch=b_same, commits=[_commit("h1")]),
        ],
        heads={"release-2": "new", "feature/x": "same", "dead-branch": "z"},
    )
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={},
        scans={"octo/repo": scan},
    )
    state = State()
    state.repo("octo/repo").branches = {"feature/x": "same", "gone": "g"}
    jira = FakeJira()
    cfg2 = replace(cfg, use_graphql=True)

    summary = sync.run_once(cfg2, ghes=ghes, jira=jira, state=state)

    assert ghes.active_branches_calls == [("octo/repo", ghes.active_branches_calls[0][1])]
    assert ghes.commits_since_calls == [] and ghes.compare_calls == []  # no REST branch calls
    # feature/x unchanged -> skipped; release-2 changed -> pushed
    assert summary.branches == 1
    assert summary.commits == 2
    # "gone" was in state, absent from the repo now -> deleted
    assert jira.deleted == [("1000", "gone")]
    # state keeps prior known heads + the one we pushed; "gone" dropped;
    # inactive "dead-branch" is never recorded (stays "new" for a future backfill)
    assert _saved(cfg2)["repos"]["octo/repo"]["branches"] == {
        "release-2": "new",
        "feature/x": "same",
    }


def test_branch_head_moved_ahead_uses_compare(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha2")]},
        compares={
            ("octo/repo", "sha1", "sha2"): CompareResult(
                status=COMPARE_AHEAD,
                merge_base_sha="sha1",
                commits=[_commit("c9"), _commit("c10")],
            )
        },
    )
    jira = FakeJira()
    state = State()
    state.repo("octo/repo").branches = {"main": "sha1"}

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert summary.commits == 2
    assert summary.branches == 1
    assert ghes.commits_since_calls == []  # compare path, not commits_since
    assert _saved(cfg)["repos"]["octo/repo"]["branches"] == {"main": "sha2"}


def test_unchanged_branch_produces_no_push(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
    )
    jira = FakeJira()
    state = State()
    state.repo("octo/repo").branches = {"main": "sha1"}

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert jira.pushed == []
    assert summary.repos_pushed == 1  # payload None still counts as a successful pass
    assert summary.branches == 0
    # state still advanced / rewritten
    assert _saved(cfg)["repos"]["octo/repo"]["branches"] == {"main": "sha1"}


def test_deleted_branch_calls_jira_and_drops_from_state(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
    )
    jira = FakeJira()
    state = State()
    state.repo("octo/repo").branches = {"main": "sha1", "feature/old": "sha-old"}

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    # delete uses the sanitized branch id, not the raw name
    assert jira.deleted == [("1000", "feature~2fold")]
    assert summary.branches_deleted == 1
    assert _saved(cfg)["repos"]["octo/repo"]["branches"] == {"main": "sha1"}


def test_get_repo_none_skips_but_counts(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/gone", "octo/repo"],
        metas={"octo/gone": None, "octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
    )
    jira = FakeJira()
    state = State()

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert summary.repos_seen == 2
    assert summary.repos_pushed == 1
    assert summary.repos_failed == 0
    assert set(_saved(cfg)["repos"]) == {"octo/repo"}


def test_per_repo_exception_isolated(cfg: Settings) -> None:
    ghes = FakeGhes(
        repos=["octo/bad", "octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
        get_repo_errors={"octo/bad": RuntimeError("kaboom")},
    )
    jira = FakeJira()
    state = State()

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert summary.repos_failed == 1
    assert summary.repos_pushed == 1
    assert summary.repos_seen == 2
    assert summary.errors and summary.errors[0].startswith("octo/bad: ")
    # the healthy repo was still processed and persisted
    assert set(_saved(cfg)["repos"]) == {"octo/repo"}


def test_log_entities_emits_lines(cfg: Settings, caplog: pytest.LogCaptureFixture) -> None:
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
    )
    cfg2 = replace(cfg, log_entities=True)
    with caplog.at_level("INFO", logger="bridge.sync"):
        sync.run_once(cfg2, ghes=ghes, jira=FakeJira(), state=State())
    assert any("commits in dev-info for repo octo/repo" in r.message for r in caplog.records)


def test_dry_run_skips_push_but_advances_state(cfg: Settings) -> None:
    dry = replace(cfg, dry_run=True)
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
    )
    jira = FakeJira()
    state = State()
    state.repo("octo/repo").branches = {"main": "sha1", "feature/old": "sha-old"}

    summary = sync.run_once(dry, ghes=ghes, jira=jira, state=state)

    assert jira.pushed == []
    assert jira.deleted == []  # delete_branch is a no-op in dry-run
    assert summary.repos_pushed == 1
    saved = _saved(dry)
    assert saved["repos"]["octo/repo"]["branches"] == {"main": "sha1"}


def test_pr_high_water_advances(cfg: Settings) -> None:
    pr = PullRequest(
        number=7,
        title="add thing ABC-1",
        state=PR_OPEN,
        url="https://ghe.example.com/octo/repo/pull/7",
        author=AUTHOR,
        source_branch="feature/x",
        destination_branch="main",
        last_update="2026-08-27T12:00:00Z",
    )
    ghes = FakeGhes(
        repos=["octo/repo"],
        metas={"octo/repo": _meta()},
        branches={"octo/repo": [_branch("main", "sha1")]},
        commits={("octo/repo", "main"): [_commit("c1")]},
        prs={"octo/repo": [pr]},
    )
    jira = FakeJira()
    state = State()

    summary = sync.run_once(cfg, ghes=ghes, jira=jira, state=state)

    assert summary.pull_requests == 1
    assert ghes.pr_since_calls[0][1].endswith("Z")  # fell back to lookback cutoff
    assert _saved(cfg)["repos"]["octo/repo"]["pr_high_water"] == "2026-08-27T12:00:00Z"
