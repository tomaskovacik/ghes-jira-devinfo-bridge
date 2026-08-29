"""Human-readable rendering of a devinfo repository object.

Works on both a payload's ``repositories[0]`` and the ``GET .../repository/{id}``
response — they share the same shape.
"""

from __future__ import annotations


def _first_line(message: str, width: int = 60) -> str:
    lines = (message or "").splitlines()
    return (lines[0] if lines else "")[:width]


def repo_lines(repo: dict) -> list[str]:
    commits = repo.get("commits") or []
    branches = repo.get("branches") or []
    prs = repo.get("pullRequests") or []
    name = repo.get("name", "?")

    out = [
        f"repo: {name} (id {repo.get('id', '?')})",
        f"commits in dev-info for repo {name}: {len(commits)}",
    ]
    for c in commits:
        keys = ",".join(c.get("issueKeys") or []) or "-"
        out.append(
            f"  {c.get('id', ''):40}  {c.get('authorTimestamp', ''):20}  "
            f"{keys:16}  {_first_line(c.get('message', ''))}"
        )
    out.append(f"branches: {len(branches)}")
    for b in branches:
        keys = ",".join(b.get("issueKeys") or []) or "-"
        out.append(f"  {b.get('id') or b.get('name', '')}  {keys}")
    out.append(f"pull requests: {len(prs)}")
    for p in prs:
        keys = ",".join(p.get("issueKeys") or []) or "-"
        out.append(f"  #{p.get('id', '')}  {p.get('status', '')}  {keys}")
    return out
