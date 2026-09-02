"""Internal domain types shared across modules.

This module is the frozen contract between the GHES client, the transform layer
and the sync orchestrator. Do not add I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Pull request states as expected by the Jira devinfo API.
PR_OPEN = "OPEN"
PR_MERGED = "MERGED"
PR_DECLINED = "DECLINED"

# Values returned in the ``status`` field of the GHES compare API.
COMPARE_AHEAD = "ahead"
COMPARE_BEHIND = "behind"
COMPARE_IDENTICAL = "identical"
COMPARE_DIVERGED = "diverged"


@dataclass(frozen=True)
class Author:
    name: str
    email: str


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str
    author: Author
    authored_date: str  # ISO 8601
    url: str  # web URL on the GHES instance
    file_count: int = 0
    is_merge: bool = False  # >1 parent -> devinfo flags: ["MERGE_COMMIT"]


@dataclass(frozen=True)
class Branch:
    name: str
    head_sha: str
    url: str  # web URL on the GHES instance
    last_commit: Commit | None = None


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    state: str  # one of PR_OPEN / PR_MERGED / PR_DECLINED
    url: str
    author: Author
    source_branch: str
    destination_branch: str
    last_update: str  # ISO 8601
    body: str = ""
    comment_count: int = 0
    review_count: int = 0


@dataclass(frozen=True)
class RepoMeta:
    full_name: str  # "org/repo"
    repo_id: str
    url: str  # web URL
    default_branch: str


@dataclass(frozen=True)
class CompareResult:
    status: str  # COMPARE_* constant
    merge_base_sha: str
    commits: list[Commit]  # commits unique to head, oldest first


@dataclass(frozen=True)
class BranchCommits:
    """A branch plus the commits on it since the lookback cutoff (oldest first)."""

    branch: Branch
    commits: list[Commit]


@dataclass(frozen=True)
class BranchScan:
    """Result of the GraphQL branch scan for one repo."""

    active: list[BranchCommits]  # branches with a commit at/after the cutoff
    heads: dict[str, str]  # every branch name -> head sha (all branches)


@dataclass
class RepoChanges:
    """Everything discovered for a single repo in one sync run."""

    full_name: str
    repo_id: str
    url: str
    commits: list[Commit] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    pull_requests: list[PullRequest] = field(default_factory=list)
    deleted_branch_names: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.commits or self.branches or self.pull_requests or self.deleted_branch_names
        )


@dataclass
class DevinfoResult:
    """Parsed response of a devinfo bulk push."""

    accepted_devinfo_keys: list[str] = field(default_factory=list)
    unknown_issue_keys: list[str] = field(default_factory=list)
    unknown_associations: list[str] = field(default_factory=list)
    failed_devinfo_keys: list[str] = field(default_factory=list)


@dataclass
class SyncSummary:
    repos_seen: int = 0
    repos_pushed: int = 0
    repos_failed: int = 0
    commits: int = 0
    branches: int = 0
    pull_requests: int = 0
    branches_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def merge_push(self, changes: RepoChanges) -> None:
        self.commits += len(changes.commits)
        self.branches += len(changes.branches)
        self.pull_requests += len(changes.pull_requests)
        self.branches_deleted += len(changes.deleted_branch_names)
