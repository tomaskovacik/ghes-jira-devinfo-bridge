"""Process entrypoint and CLI.

Subcommands (default is ``sync``):

  python -m bridge [sync]
      One pass, or a loop when ``SYNC_INTERVAL_SECONDS > 0``. SIGINT/SIGTERM
      finish the current pass and exit. Touches ``{state_path}.healthy`` after
      each successful pass for container healthchecks.

  python -m bridge inspect [--repo OWNER/NAME | --all] [--json]
      Show what Jira has stored for a repo's development information
      (commit/branch/PR counts, or the full document with --json).

  python -m bridge delete-repo (--repo OWNER/NAME | --repo-id ID | --all)
                               [--yes] [--reset-state]
      Purge all devinfo for a repo in Jira. Recovery for a repo whose async
      processing is wedged. --reset-state also drops it from state.json so the
      next sync rebuilds it.

  python -m bridge reprocess (--repo OWNER/NAME | --repo-id ID | --all)
      Delete every stored commit entity for a repo and re-push it (chunked,
      rebuilt from what Jira already holds). Forces Jira to rebuild issue
      associations that a large backfill left unbuilt; a plain re-push only
      updates and does not.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

import bridge
from bridge import report
from bridge.config import ConfigError, Settings
from bridge.ghes import GhesClient
from bridge.jira import JiraClient
from bridge.state import State
from bridge.sync import run_once

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _JsonFormatter(logging.Formatter):
    """One compact JSON object per line. Carries no secret values."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def _configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper() if level else "INFO")


def _log_summary(logger: logging.Logger, summary) -> None:
    logger.info(
        "pass complete: seen=%d pushed=%d failed=%d commits=%d branches=%d "
        "pull_requests=%d branches_deleted=%d",
        summary.repos_seen,
        summary.repos_pushed,
        summary.repos_failed,
        summary.commits,
        summary.branches,
        summary.pull_requests,
        summary.branches_deleted,
    )
    for err in summary.errors:
        logger.warning("repo error: %s", err)


def _touch(path: str) -> None:
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bridge")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("sync", help="run one sync pass (or a loop); the default")

    insp = sub.add_parser("inspect", help="show devinfo Jira has stored for a repo")
    grp = insp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--repo", metavar="OWNER/NAME")
    grp.add_argument("--all", action="store_true", help="every repo in state.json")
    fmt = insp.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the full document")
    fmt.add_argument(
        "--full", action="store_true", help="list every stored commit hash, branch and PR"
    )

    dele = sub.add_parser("delete-repo", help="purge all devinfo for a repo in Jira")
    dgrp = dele.add_mutually_exclusive_group(required=True)
    dgrp.add_argument("--repo", metavar="OWNER/NAME")
    dgrp.add_argument("--repo-id", metavar="ID")
    dgrp.add_argument("--all", action="store_true", help="every repo in state.json")
    dele.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    dele.add_argument(
        "--reset-state", action="store_true", help="also drop the repo(s) from state.json"
    )

    repro = sub.add_parser(
        "reprocess", help="delete + recreate stored commits so Jira rebuilds associations"
    )
    rgrp = repro.add_mutually_exclusive_group(required=True)
    rgrp.add_argument("--repo", metavar="OWNER/NAME")
    rgrp.add_argument("--repo-id", metavar="ID")
    rgrp.add_argument("--all", action="store_true", help="every repo in state.json")

    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------


def _run_sync(settings: Settings, logger: logging.Logger) -> int:
    ghes = GhesClient(settings)
    jira = JiraClient(settings)
    state = State.load(settings.state_path)
    health_path = f"{settings.state_path}.healthy"

    if settings.interval_seconds <= 0:
        try:
            summary = run_once(settings, ghes=ghes, jira=jira, state=state)
        finally:
            ghes.close()
            jira.close()
        _log_summary(logger, summary)
        if not summary.repos_failed:
            _touch(health_path)
        return 1 if summary.repos_failed else 0

    stop = {"requested": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        logger.info("signal %d received; finishing the current pass then exiting", signum)
        stop["requested"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not stop["requested"]:
            try:
                summary = run_once(settings, ghes=ghes, jira=jira, state=state)
                _log_summary(logger, summary)
            except Exception:
                logger.exception("sync pass failed; continuing")
            else:
                _touch(health_path)

            slept = 0
            while slept < settings.interval_seconds and not stop["requested"]:
                time.sleep(1)
                slept += 1
    finally:
        ghes.close()
        jira.close()

    return 0


# --------------------------------------------------------------------------
# inspect / delete-repo
# --------------------------------------------------------------------------


def _resolve_targets(
    args: argparse.Namespace, settings: Settings, ghes: GhesClient
) -> list[tuple[str, str]]:
    """Return ``[(name, repo_id), ...]`` for the CLI selection."""
    if getattr(args, "all", False):
        state = State.load(settings.state_path)
        return [(name, rs.repo_id) for name, rs in state.repos.items() if rs.repo_id]
    if getattr(args, "repo_id", None):
        return [(args.repo_id, args.repo_id)]
    meta = ghes.get_repo(args.repo)
    if meta is None:
        raise SystemExit(f"repo {args.repo} not accessible on GHES")
    return [(meta.full_name, meta.repo_id)]


def _print_full(name: str, repo_id: str, doc: dict) -> None:
    print("\n".join(report.repo_lines({**doc, "name": name, "id": repo_id})))
    print()


def _run_inspect(args: argparse.Namespace, settings: Settings) -> int:
    ghes = GhesClient(settings)
    jira = JiraClient(settings)
    try:
        for name, repo_id in _resolve_targets(args, settings, ghes):
            doc = jira.get_repository(repo_id)
            if args.json:
                print(json.dumps({"name": name, "id": repo_id, "devinfo": doc}, indent=2))
            elif args.full:
                _print_full(name, repo_id, doc)
            else:
                print(
                    f"{name} (id {repo_id}): "
                    f"commits={len(doc.get('commits') or [])} "
                    f"branches={len(doc.get('branches') or [])} "
                    f"pullRequests={len(doc.get('pullRequests') or [])} "
                    f"lastUpdated={doc.get('lastUpdated')}"
                )
    finally:
        ghes.close()
        jira.close()
    return 0


def _run_delete_repo(args: argparse.Namespace, settings: Settings) -> int:
    ghes = GhesClient(settings)
    jira = JiraClient(settings)
    try:
        targets = _resolve_targets(args, settings, ghes)
        if not targets:
            print("nothing to delete")
            return 0
        if not args.yes:
            listing = ", ".join(f"{n} (id {i})" for n, i in targets)
            reply = input(f"Purge devinfo for {listing}? [y/N] ").strip().lower()
            if reply not in {"y", "yes"}:
                print("aborted")
                return 1

        state = State.load(settings.state_path) if args.reset_state else None
        for name, repo_id in targets:
            jira.delete_repository(repo_id)
            print(f"deleted devinfo for {name} (id {repo_id})")
            if state is not None:
                state.repos.pop(name, None)
        if state is not None:
            state.save(settings.state_path)
            print("state.json updated")
    finally:
        ghes.close()
        jira.close()
    return 0


def _one_linkage(entity: dict) -> dict:
    """Jira 400s an entity carrying both issueKeys and associations. Stored
    entities read back from Jira only carry issueKeys, but guard anyway: if both
    are present, keep issueKeys and drop associations."""
    if entity.get("issueKeys") and "associations" in entity:
        return {k: v for k, v in entity.items() if k != "associations"}
    return dict(entity)


def _run_reprocess(args: argparse.Namespace, settings: Settings) -> int:
    ghes = GhesClient(settings)
    jira = JiraClient(settings)
    chunk = settings.push_chunk_size or 5
    try:
        targets = _resolve_targets(args, settings, ghes)
        if not targets:
            print("no repos to reprocess (state.json empty or has no repo ids)")
            return 0
        for name, repo_id in targets:
            doc = jira.get_repository(repo_id)
            commits = doc.get("commits") or []
            if not commits:
                print(f"{name} (id {repo_id}): no stored commits, skipping")
                continue

            for commit in commits:
                commit_id = commit.get("id") or commit.get("hash")
                if commit_id:
                    jira.delete_commit(repo_id, commit_id)
            if settings.dry_run:
                print(f"{name} (id {repo_id}): dry-run, would recreate {len(commits)} commits")
                continue
            print(f"{name} (id {repo_id}): deleted {len(commits)} commits, waiting 10s")
            time.sleep(10)

            # distinct sha per commit -> one id per entity; base + index keeps them
            # monotonic and strictly above anything previously stored.
            base = int(time.time() * 1000)
            payload = {
                "repositories": [
                    {
                        "id": repo_id,
                        "name": doc.get("name") or name,
                        "url": doc.get("url") or f"{settings.ghes_base_url}/{name}",
                        "updateSequenceId": base,
                        "commits": [
                            {**_one_linkage(c), "updateSequenceId": base + 1 + i}
                            for i, c in enumerate(commits)
                        ],
                    }
                ],
                "preventTransitions": settings.prevent_transitions,
                "operationType": "BACKFILL",
                "properties": {"repositoryId": str(repo_id)},
                "providerMetadata": {"product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"},
            }
            result = jira.push(payload, chunk_size=chunk)
            print(
                f"{name} (id {repo_id}): recreated {len(commits)} commits"
                f" (unknown_keys={result.unknown_issue_keys}"
                f" unknown_assoc={result.unknown_associations}"
                f" failed={result.failed_devinfo_keys})"
            )
    finally:
        ghes.close()
        jira.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _configure_logging(settings.log_level, settings.log_format)
    logger = logging.getLogger("bridge")

    if args.command == "inspect":
        return _run_inspect(args, settings)
    if args.command == "delete-repo":
        return _run_delete_repo(args, settings)
    if args.command == "reprocess":
        return _run_reprocess(args, settings)
    return _run_sync(settings, logger)


if __name__ == "__main__":
    sys.exit(main())
