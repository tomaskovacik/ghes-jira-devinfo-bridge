"""Sync orchestration. Implemented by agent C.

``run_once`` performs exactly one pass over every repo and returns a
:class:`bridge.models.SyncSummary`. It must not raise for a single-repo failure:
log it, record it in ``summary.errors`` / ``summary.repos_failed``, continue.

Per repo:
  1. ``ghes.get_repo`` -> skip (warn) on ``None``.
  2. ``ghes.list_branches``; diff head shas against ``state.repo(name).branches``:
       - unknown branch  -> ``ghes.commits_since(branch, lookback_iso)``, emit branch + commits
       - head moved      -> ``ghes.compare(old, new)``:
             status ahead     -> emit compare.commits
             status diverged  -> emit commits_since(branch, lookback_iso)
             identical/behind -> emit branch entity only
       - unchanged       -> nothing
  3. branches in state but absent now -> ``changes.deleted_branch_names``,
     call ``jira.delete_branch`` per name, drop from state.
  4. if ``settings.include_prs``: ``ghes.pull_requests_since(pr_high_water or lookback_iso)``.
  5. ``transform.build_devinfo_payload(...)``; if ``None`` -> still commit branch
     head/pr_high_water bookkeeping, skip push.
  6. ``jira.push(payload)`` unless ``settings.dry_run``; log unknown/failed keys.
  7. On success: update ``state.repo(name)`` (repo_id, branch heads, pr_high_water,
     last_success) and ``state.save(settings.state_path)``.

``lookback_iso`` = now - ``settings.lookback_days``, ISO 8601 UTC ``Z``.
``updateSequenceId`` is assigned per-entity by ``transform`` (``base + index`` from
one ``int(time.time()*1000)`` sample), so a commit that is also a branch's
``lastCommit`` gets one shared id. First sight of a repo is pushed as
``operationType: BACKFILL``.
"""

from __future__ import annotations

import fnmatch
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial

from bridge import issuekeys, report, transform
from bridge.config import Settings
from bridge.ghes import GhesClient
from bridge.jira import JiraClient
from bridge.models import (
    COMPARE_AHEAD,
    COMPARE_DIVERGED,
    Branch,
    Commit,
    RepoChanges,
    RepoMeta,
    SyncSummary,
)
from bridge.state import State

logger = logging.getLogger(__name__)


def _iso_z(moment: datetime) -> str:
    """Format an aware datetime as ISO 8601 UTC (second precision) with ``Z``."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _with_last_commit(ghes: GhesClient, repo: str, branch: Branch, fetched: list[Commit]) -> Branch:
    """Attach a real head commit to a branch about to be pushed: reuse one we
    already fetched, otherwise a single ``GET /commits/{sha}``."""
    for commit in fetched:
        if commit.sha == branch.head_sha:
            return replace(branch, last_commit=commit)
    head = ghes.head_commit(repo, branch.head_sha)
    return replace(branch, last_commit=head) if head is not None else branch


_COMPARE_CAP = 250  # GHES compare API returns at most this many commits


def _process_branch(
    ghes: GhesClient,
    repo: str,
    default_branch: str,
    known: dict[str, str],
    lookback_iso: str,
    branch: Branch,
) -> tuple[Branch | None, list[Commit]]:
    """Resolve one branch. Returns the branch entity to emit (or ``None`` if
    unchanged) and the commits to add.

    First encounter:
      * default branch     -> walk its history since the lookback cutoff
      * any other branch    -> ``compare(default_branch, head)``, i.e. only the
        commits unique to this branch (not the whole shared history)
    Subsequent encounters: ``compare(previous_head, head)`` -> just the delta.
    """
    previous = known.get(branch.name)

    if previous is None:
        if branch.name == default_branch or not default_branch:
            fetched = ghes.commits_since(repo, branch.name, lookback_iso)
            return _with_last_commit(ghes, repo, branch, fetched), fetched
        cmp = ghes.compare(repo, default_branch, branch.head_sha)
        if len(cmp.commits) >= _COMPARE_CAP:  # truncated -> fall back to a bounded walk
            fetched = ghes.commits_since(repo, branch.name, lookback_iso)
            return _with_last_commit(ghes, repo, branch, fetched), fetched
        # cmp.commits are exactly the commits unique to this branch
        return _with_last_commit(ghes, repo, branch, cmp.commits), list(cmp.commits)

    if previous == branch.head_sha:
        return None, []

    cmp = ghes.compare(repo, previous, branch.head_sha)
    fetched = list(cmp.commits)
    emit: list[Commit] = []
    if cmp.status == COMPARE_AHEAD:
        emit = list(cmp.commits)
    elif cmp.status == COMPARE_DIVERGED:
        fetched = ghes.commits_since(repo, branch.name, lookback_iso)
        emit = fetched
    return _with_last_commit(ghes, repo, branch, fetched), emit


def _in_scope(settings: Settings, meta: RepoMeta, pattern, name: str) -> bool:
    if settings.default_branch_only:
        return name == meta.default_branch
    if settings.keyed_branches_only:
        return name == meta.default_branch or bool(issuekeys.extract(name, pattern))
    return True


def _collect_rest(
    settings: Settings,
    ghes: GhesClient,
    meta: RepoMeta,
    repo: str,
    known: dict[str, str],
    lookback_iso: str,
    pattern,
    changes: RepoChanges,
) -> tuple[set[str], dict[str, str]]:
    all_branches = ghes.list_branches(repo)
    branches = [
        b
        for b in all_branches
        if not _excluded(b.name, settings.ghes_branch_exclude)
        and _in_scope(settings, meta, pattern, b.name)
    ]
    resolve = partial(_process_branch, ghes, repo, meta.default_branch, known, lookback_iso)
    workers = max(1, min(settings.concurrency, len(branches) or 1))
    pushed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for emitted, commits in pool.map(resolve, branches):
            if emitted is not None:
                changes.branches.append(emitted)
                pushed[emitted.name] = emitted.head_sha
            changes.commits.extend(commits)
    # present = every branch that exists; pushed = only what we processed this run
    return ({b.name for b in all_branches}, pushed)


def _collect_graphql(
    settings: Settings,
    ghes: GhesClient,
    meta: RepoMeta,
    repo: str,
    known: dict[str, str],
    lookback_iso: str,
    pattern,
    changes: RepoChanges,
) -> tuple[set[str], dict[str, str]]:
    scan = ghes.active_branches(repo, lookback_iso)
    logger.info(
        "%s: graphql scan -> %d branches, %d active since %s",
        repo,
        len(scan.heads),
        len(scan.active),
        lookback_iso,
    )
    pushed: dict[str, str] = {}
    for bc in scan.active:
        if _excluded(bc.branch.name, settings.ghes_branch_exclude):
            continue
        if not _in_scope(settings, meta, pattern, bc.branch.name):
            continue
        if known.get(bc.branch.name) == bc.branch.head_sha:
            continue  # unchanged since last run
        changes.branches.append(bc.branch)
        changes.commits.extend(bc.commits)
        pushed[bc.branch.name] = bc.branch.head_sha
    # present = every branch that exists; pushed = only what we processed this run
    return (set(scan.heads), pushed)


def run_once(
    settings: Settings,
    *,
    ghes: GhesClient,
    jira: JiraClient,
    state: State,
) -> SyncSummary:
    summary = SyncSummary()
    lookback_iso = _iso_z(datetime.now(UTC) - timedelta(days=settings.lookback_days))
    pattern = issuekeys.compile_pattern(settings.issue_key_regex)

    for name in ghes.resolve_repos():
        try:
            meta = ghes.get_repo(name)
            if meta is None:
                logger.warning("repo %s is not accessible; skipping", name)
                summary.repos_seen += 1
                continue

            rs = state.repo(name)
            known = dict(rs.branches)

            changes = RepoChanges(full_name=name, repo_id=meta.repo_id, url=meta.url)
            collect = _collect_graphql if settings.use_graphql else _collect_rest
            present, pushed_heads = collect(
                settings, ghes, meta, name, known, lookback_iso, pattern, changes
            )

            # A commit reachable from several branches is collected once per branch;
            # collapse to one entry per sha, keeping first-seen order.
            seen_sha: set[str] = set()
            deduped: list = []
            for commit in changes.commits:
                if commit.sha not in seen_sha:
                    seen_sha.add(commit.sha)
                    deduped.append(commit)
            changes.commits = deduped

            # ``present`` covers every branch that exists (both modes), so an
            # excluded or merely-inactive branch is not mistaken for a deleted one.
            deleted = [
                b
                for b in known
                if b not in present and not _excluded(b, settings.ghes_branch_exclude)
            ]
            changes.deleted_branch_names = deleted
            for gone in deleted:
                if not settings.dry_run:
                    jira.delete_branch(meta.repo_id, transform.branch_id(gone))
                logger.info("branch %s removed from %s", gone, name)

            if settings.include_prs:
                changes.pull_requests = ghes.pull_requests_since(
                    name, rs.pr_high_water or lookback_iso
                )

            first_sight = not rs.branches and not rs.last_success and not rs.backfilled
            operation_type = (
                "BACKFILL" if (first_sight and settings.backfill_on_first_sight) else "NORMAL"
            )
            payload = transform.build_devinfo_payload(
                changes,
                prevent_transitions=settings.prevent_transitions,
                operation_type=operation_type,
                properties={"repositoryId": str(meta.repo_id)},
                pattern=pattern,
                send_issue_keys=settings.send_issue_keys,
                send_associations=settings.send_associations,
                key_cap=settings.issue_key_cap,
            )

            if settings.log_entities and payload is not None:
                for line in report.repo_lines(payload["repositories"][0]):
                    logger.info(line)

            if payload is not None and not settings.dry_run:
                result = jira.push(payload, chunk_size=settings.push_chunk_size)
                if result.unknown_issue_keys:
                    logger.warning(
                        "%s: Jira did not recognise issue keys %s",
                        name,
                        result.unknown_issue_keys,
                    )
                if result.unknown_associations:
                    logger.warning(
                        "%s: Jira could not associate devinfo entities %s",
                        name,
                        result.unknown_associations,
                    )
                if result.failed_devinfo_keys:
                    logger.warning(
                        "%s: Jira rejected devinfo entities %s",
                        name,
                        result.failed_devinfo_keys,
                    )
            elif payload is not None:
                logger.info("%s: dry-run, not pushing payload: %s", name, payload)

            rs.repo_id = meta.repo_id
            rs.backfilled = True  # first-sight BACKFILL (if any) has now been sent
            # Remember a branch head only once we have actually processed it, so a
            # branch that was inactive / out of scope stays "new" and gets a full
            # backfill when it later becomes relevant.
            rs.branches = {**known, **pushed_heads}
            for gone in deleted:
                rs.branches.pop(gone, None)
            if changes.pull_requests:
                rs.pr_high_water = max(
                    rs.pr_high_water,
                    *(pr.last_update for pr in changes.pull_requests),
                )
            rs.last_success = _iso_z(datetime.now(UTC))
            state.save(settings.state_path)

            summary.repos_pushed += 1
            summary.merge_push(changes)
        except Exception as exc:  # noqa: BLE001 - per-repo isolation is intentional
            logger.exception("repo %s failed", name)
            summary.repos_failed += 1
            summary.errors.append(f"{name}: {exc}")

        summary.repos_seen += 1

    return summary
