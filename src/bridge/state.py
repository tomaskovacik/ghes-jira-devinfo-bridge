"""Persistent sync state. Implemented by agent C.

JSON file at ``settings.state_path``. Written atomically (temp file in the same
directory + ``os.replace``). Persisted by the caller only after a successful
push, so a crash replays rather than loses work.

On-disk shape::

    {
      "version": 1,
      "repos": {
        "org/repo": {
          "repo_id": "123",
          "branches": {"main": "<sha>", "feature/x": "<sha>"},
          "pr_high_water": "2026-08-28T10:00:00Z",
          "last_success": "2026-08-28T10:05:00Z"
        }
      }
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field

STATE_VERSION = 1


@dataclass
class RepoState:
    repo_id: str = ""
    branches: dict[str, str] = field(default_factory=dict)  # branch name -> head sha
    pr_high_water: str = ""  # ISO 8601
    last_success: str = ""  # ISO 8601
    backfilled: bool = False  # first-sight BACKFILL push has been sent


@dataclass
class State:
    repos: dict[str, RepoState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> State:
        """Return parsed state, or an empty :class:`State` if the file is absent.

        Older ``version`` values are migrated forward; only ``v1`` exists today so
        there is nothing to migrate. An unknown or newer ``version`` raises, as
        does a corrupt file, so the operator notices rather than silently
        re-syncing everything.
        """
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return cls()

        if not isinstance(data, dict):
            raise RuntimeError(f"state file {path}: expected a JSON object")

        version = data.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise RuntimeError(
                f"state file {path}: unsupported version {version!r} "
                f"(this build understands version {STATE_VERSION})"
            )

        repos: dict[str, RepoState] = {}
        for name, raw in (data.get("repos") or {}).items():
            raw = raw or {}
            repos[name] = RepoState(
                repo_id=str(raw.get("repo_id", "")),
                branches=dict(raw.get("branches") or {}),
                pr_high_water=str(raw.get("pr_high_water", "")),
                last_success=str(raw.get("last_success", "")),
                backfilled=bool(raw.get("backfilled", False)),
            )
        return cls(repos=repos)

    def save(self, path: str) -> None:
        """Atomically write current state to ``path`` (creates parent dir if needed)."""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)

        payload = {
            "version": STATE_VERSION,
            "repos": {
                name: {
                    "repo_id": rs.repo_id,
                    "branches": dict(rs.branches),
                    "pr_high_water": rs.pr_high_water,
                    "last_success": rs.last_success,
                    "backfilled": rs.backfilled,
                }
                for name, rs in self.repos.items()
            },
        }

        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed via the `with` below
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=directory,
            prefix=".state-",
            suffix=".tmp",
        )
        try:
            with tmp:
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise

    def repo(self, full_name: str) -> RepoState:
        """Get-or-create the :class:`RepoState` for ``full_name``."""
        rs = self.repos.get(full_name)
        if rs is None:
            rs = RepoState()
            self.repos[full_name] = rs
        return rs
