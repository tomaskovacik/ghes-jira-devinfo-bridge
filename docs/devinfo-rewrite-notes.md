# Devinfo rewrite notes — bringing the bridge to current spec + consensus pattern

> **Status: applied 2026-09-02.** This was the change plan for the devinfo
> payload/delete rewrite; it now describes what the code does and why. Line
> numbers are from the pre-rewrite versions and may not match `main`.

Files: `src/bridge/transform.py`, `jira.py`, `sync.py`, `config.py`, `models.py`,
`state.py`, `ghes.py`, `report.py`.

Cross-reference: `devinfo-api.md` (spec), `devinfo-reference-implementations.md` (consensus).

---

## 0. Summary of the four audit findings and the verdict

| finding | verdict after research | action |
|---|---|---|
| uses deprecated `issueKeys` (should be `associations`) | **Wrong.** `issueKeys` is not `deprecated:true`, every shipping client sends it, and the live API rejects a payload carrying **both** `issueKeys` and `associations` on one entity (`400`, "mutually exclusive"). | Send `issueKeys` (default); `JIRA_SEND_ISSUE_KEYS=false` switches to `associations` (`issueIdOrKeys`). Never both. |
| one identical `updateSequenceId` for every entity; `commits[]` vs `branches[].lastCommit` collide | **Confirmed real.** Spec: replace only if incoming `> stored`, per entity id; equal is ignored. GitLab has the same bug; github-for-jira dodges it with per-entity `Date.now()`. | Per-entity monotonic `base + index`; dedupe shared SHA to one id → one USID. |
| chunk size 5 (spec allows 400) | **Confirmed.** Both first-party clients use 400. | Default 400; keep chunking only as a safety valve. |
| `delete_branch` uses `bulkByProperties` with no `properties` object | **Confirmed broken.** `bulkByProperties` matches *whole repositories* by their submission `properties`; it is not a per-branch delete. Consensus: per-entity `DELETE repository/{repoId}/branch/{branchId}`. | Rewrite to per-entity endpoint + `?_updateSequenceId`. |

---

## 1. `models.py`

### 1.1 `Commit` (L29-37) — add merge flag + optional file detail

```python
@dataclass(frozen=True)
class Commit:
    sha: str
    message: str
    author: Author
    authored_date: str
    url: str
    file_count: int = 0
    is_merge: bool = False          # NEW  -> flags: ["MERGE_COMMIT"]
```

- `is_merge`: set by `ghes.py` from `len(parents) > 1` on the REST commit object,
  or the GraphQL `parents.totalCount > 1`. (`ghes.py` is out of rewrite scope but
  needs this one-line addition where `Commit(...)` is constructed.)
- Do **not** add a `files: list[...]` yet — `providerMetadata`/`FileData` is
  optional and GHES compare/commits payloads don't cheaply give per-file change
  types. Leave `file_count` as the only file signal.

### 1.2 `DevinfoResult` (L111-118) — unchanged shape, clarify semantics

Keep `accepted_devinfo_keys`, `unknown_issue_keys`, `unknown_associations`,
`failed_devinfo_keys`. Optionally add `accepted_count: int = 0` for summary
logging. `failed_devinfo_keys` now maps to `failedDevinfoEntities` (already the
name `jira.py` reads — good).

### 1.3 `state.py` — first-sight marker

`RepoState` already persists `repo_id`, `branches`, `pr_high_water`,
`last_success`. First sight of a repo = `not rs.branches and not rs.last_success`.
No new field strictly required, but add an explicit `backfilled: bool = False`
to `RepoState` so BACKFILL is sent exactly once even if the first run pushes zero
entities. Set it `True` in `run_once` right after the first successful push.

---

## 2. `transform.py` — the core rewrite

### 2.1 New module-level constants + helpers

```python
_MERGE_FLAG = "MERGE_COMMIT"
_ASSOC_TYPE = "issueIdOrKeys"
_KEY_CAP = 500                      # spec: total values per entity <= 500

# field length caps (see devinfo-api.md §3; verify vs live docs)
_CAP_MESSAGE = 1024
_CAP_URL = 2000
_CAP_NAME = 255
_CAP_BRANCH_NAME = 512
_CAP_DISPLAY_ID = 255
_CAP_ID = 1024

def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]

def _assoc(keys: list[str]) -> list[dict]:
    return [{"associationType": _ASSOC_TYPE, "values": keys}] if keys else []
```

### 2.2 `updateSequenceId` scheme — specify exactly

**Algorithm.** One `base` per repo push, then a dense integer index per *distinct
entity id*:

1. `base = int(time.time() * 1000)` — sampled once in `build_devinfo_payload`
   (moved out of `sync.py`, see §4.2).
2. Build an **ordered id → usid map** so every entity that shares an id gets the
   *same* number and every distinct id gets a distinct number:
   - order: repository first, then commits in `changes.commits` order, then
     branch heads (`branch.last_commit.sha` / `branch.head_sha`) not already
     seen, then branches by `branch_id(name)`, then PRs by `str(pr.number)`.
   - `usid_for(kind, id) -> base + index`, where `index` is assigned on first
     sight of `(kind, id)` from a shared counter. Commits and branch-lastCommits
     are the **same kind** (`"commit"`), keyed by SHA, so
     `branches[].lastCommit` for SHA X and the standalone `commits[]` entry for
     SHA X resolve to **one** id → **one** usid → no collision, no race.
   - repository, branch, PR each have their own keyspace (a branch id can equal a
     commit SHA-prefix in theory; namespacing by kind avoids it).
3. Monotonic across runs: `base` is wall-clock ms and only grows; within a run
   indices are `< a few thousand` so `base + index` never overtakes the next
   run's `base` (runs are minutes apart). If two pushes for one repo happen in
   the same millisecond (shouldn't — one thread per repo), the second still wins
   because `sync` samples `base` after the first returns.

**Why not `Date.now()` per entity (github-for-jira):** ms resolution means the
shared-SHA copies can tie; `base + index` is collision-free by construction and
still monotonic.

Helper:

```python
class _Usid:
    def __init__(self, base: int) -> None:
        self._base = base
        self._n = 0
        self._seen: dict[tuple[str, str], int] = {}
    def get(self, kind: str, ident: str) -> int:
        key = (kind, ident)
        if key not in self._seen:
            self._seen[key] = self._n
            self._n += 1
        return self._base + self._seen[key]
```

### 2.3 `_commit_obj` (L66-78) — associations, flags, caps, usid

```python
def _commit_obj(commit, issue_keys, usid):
    keys = sorted(issue_keys)[:_KEY_CAP]
    obj = {
        "id": commit.sha,
        "hash": commit.sha,                       # keep == id (deprecated but harmless)
        "displayId": commit.sha[:7],
        "message": _clip(commit.message, _CAP_MESSAGE),
        "author": _author_obj(commit.author),
        "authorTimestamp": commit.authored_date,
        "url": _clip(commit.url, _CAP_URL),
        "fileCount": commit.file_count,
        "issueKeys": keys,                        # keep
        "associations": _assoc(keys),             # NEW
        "updateSequenceId": usid.get("commit", commit.sha),   # CHANGED
    }
    if commit.is_merge:
        obj["flags"] = [_MERGE_FLAG]              # NEW
    return obj
```
- Signature changes: `(commit, issue_keys, usid)` — drop the raw
  `update_sequence_id: int` param.

### 2.4 `_branch_obj` (L81-114) — usid, associations, lastCommit reuse

- Branch `updateSequenceId = usid.get("branch", branch_id(branch.name))`.
- `lastCommit`:
  - if `branch.last_commit` is not None → call `_commit_obj(last, last_keys,
    usid)` **unchanged** — because `_commit_obj` now derives its usid from
    `usid.get("commit", last.sha)`, it automatically matches any standalone
    `commits[]` entry for that SHA. **This is the collision fix.**
  - synthetic-fallback branch (no `last_commit`, L94-105): build the dict with
    `"updateSequenceId": usid.get("commit", branch.head_sha)` and
    `"associations": _assoc(sorted(last_keys)[:_KEY_CAP])`, `id`/`hash` =
    `branch.head_sha`, `displayId` = `branch.head_sha[:7]`.
- Branch dict: add `"associations": _assoc(sorted(issue_keys)[:_KEY_CAP])`,
  `"name": _clip(branch.name, _CAP_BRANCH_NAME)`, `"url": _clip(branch.url, _CAP_URL)`.
- `branch_id` (L55-59): keep. It already maps to `[A-Za-z0-9\-._~]` which is the
  documented id charset, and `~xx` escapes stay < 1024. Add a `_clip(_, _CAP_ID)`
  guard for pathological branch names. Keep it because it is what `sync.py` passes
  to `delete_branch` — reversibility/stability matters more than matching gitlab's
  sha256.

### 2.5 `_pull_request_obj` (L117-131) — usid, associations, caps

```python
keys = sorted(issue_keys)[:_KEY_CAP]
{
  "id": str(pr.number),
  "issueKeys": keys,
  "associations": _assoc(keys),                              # NEW
  "status": pr.state,                                        # OPEN/MERGED/DECLINED
  "title": _clip(pr.title, _CAP_MESSAGE),                    # 1024
  "url": _clip(pr.url, _CAP_URL),
  "author": _author_obj(pr.author),
  "sourceBranch": _clip(pr.source_branch, _CAP_NAME),        # 255
  "destinationBranch": _clip(pr.destination_branch, _CAP_NAME),
  "lastUpdate": pr.last_update,
  "commentCount": pr.comment_count,
  "reviewers": [],
  "updateSequenceId": usid.get("pr", str(pr.number)),        # CHANGED
}
```
- `models.PR_*` already = `OPEN/MERGED/DECLINED`. Add nothing; `UNKNOWN` fallback
  can be enforced in `ghes.py` if needed.

### 2.6 `build_devinfo_payload` (L134-183) — signature + envelope

New signature:
```python
def build_devinfo_payload(
    changes, *, prevent_transitions: bool, operation_type: str,
    properties: dict[str, str], pattern) -> dict | None:
```
- **Drop** `update_sequence_id: int` param. Sample `base = int(time.time()*1000)`
  inside and construct `usid = _Usid(base)`.
- **Order matters** — call `usid.get("repository", changes.repo_id)` first so the
  repository object gets `base + 0`, then build commits (L141-146), then branches
  (L148-154 — and inside `_branch_obj` the lastCommit reuses commit ids), then
  PRs (L156-161).
- Repository dict (L166-177): add nothing new except `updateSequenceId =
  usid.get("repository", changes.repo_id)`.
- Envelope (L179-183):
  ```python
  return {
      "repositories": [repository],
      "preventTransitions": prevent_transitions,
      "operationType": operation_type,                       # NEW: "NORMAL" | "BACKFILL"
      "properties": properties,                              # NEW: {"repositoryId": changes.repo_id}
      "providerMetadata": {"product": f"ghes-jira-devinfo-bridge/{bridge.__version__}"},
  }
  ```
- Keep the "return None if nothing carries a key" behaviour (L163-164).
- `properties` must be ≤5 keys, `^[a-zA-Z0-9_.\-]+$`, no leading `_`. Use
  `{"repositoryId": str(changes.repo_id)}` (single key). This enables
  `DELETE /bulkByProperties?repositoryId=…` recovery per repo.

### 2.7 New: a helper to build a delete's `_updateSequenceId`

Not in `transform.py` — see `jira.py` §3.5 (`_now_usid()`), since it is I/O-time.

---

## 3. `jira.py`

### 3.1 `_chunk_payload` (L59-82) — keep, but it now rarely fires

- No code change required, but note: with `push_chunk_size` default 400 and typical
  runs << 400 commits, `len(commits) <= chunk_size` (L69) short-circuits. Keep the
  function as the safety valve for a huge first backfill.
- The `updateSequenceId`s are already baked per-entity by `transform.py`, so
  splitting `commits` across chunks (L75-81) no longer risks collisions.
- Minor: L77-80 attach `branches`/`pullRequests` to the first chunk only. That is
  fine (stable ids). Leave as is. Optionally attach to every chunk to match
  github-for-jira — not necessary.

### 3.2 `push` / `_push_once` (L182-218) — response parsing

- Keep `_push_once`. It already accepts `200, 202` (L208) and reads
  `acceptedDevinfoEntities` / `unknownIssueKeys` / `unknownAssociations` /
  `failedDevinfoEntities` (L214-217). All four names are spec-correct
  (`failedDevinfoEntities`, not `rejectedDevinfoEntities`). No change.
- `_ids_from` (L32-56) defensive walker — keep; the real shape is
  `{repoId: {commits:[...], branches:[...], pullRequests:[...]}}` and the walker
  handles it.
- Consider adding: on `413`, do not retry — raise `JiraError` advising a smaller
  `push_chunk_size` (currently 413 is not in `_RETRY_STATUS`, so it already raises
  at L208-209; just improve the message).

### 3.3 `delete_branch` (L271-291) — **REWRITE** to per-entity endpoint

```python
def delete_branch(self, repo_id: str, branch_id: str) -> None:
    """DELETE .../repository/{repo_id}/branch/{branch_id}?_updateSequenceId=<now_ms>"""
    if self._settings.dry_run:
        logger.info("dry-run: skipping branch delete %s@%s", branch_id, repo_id)
        return
    url = (
        f"{self._settings.jira_api_base}/jira/devinfo/0.1/cloud/"
        f"{self.cloud_id()}/repository/{repo_id}/branch/{branch_id}"
    )
    resp = self._request(
        "DELETE", url,
        headers=self._auth_headers(),
        params={"_updateSequenceId": self._now_usid()},
    )
    if resp.status_code not in (202, 204):
        raise JiraError(f"branch delete failed: {resp.status_code} {resp.text}")
```
- Param renamed `branch_name -> branch_id` to match what `sync.py` L242 already
  passes (`transform.branch_id(gone)`). Fix the docstring accordingly.
- `entityType` path segment is the literal `branch` (spec enum:
  `commit` | `branch` | `pull_request`).

### 3.4 `delete_commit` (L254-269) — add `_updateSequenceId`

- Add `params={"_updateSequenceId": self._now_usid()}` to the `_request` call
  (L267). Path is already correct (`.../repository/{repo_id}/commit/{commit_id}`).

### 3.5 `delete_repository` (L238-252) — add `_updateSequenceId` (optional)

- Add `params={"_updateSequenceId": self._now_usid()}`. Harmless; matches
  github-for-jira.

### 3.6 New helper

```python
@staticmethod
def _now_usid() -> int:
    return int(time.time() * 1000)
```
Used by all three delete methods. The spec rule is "delete only data with
`updateSequenceId <= X`", so X must exceed every stored value → "now in ms" is
correct and safe (any earlier push has a smaller `base`).

### 3.7 `_request` (L111-131) — honour `X-RateLimit-Reset`

- In `_sleep_backoff` (L104-109), also parse `X-RateLimit-Reset` (ISO-8601
  timestamp) and `delay = max(delay, reset - now)` when present. Minor; current
  `Retry-After` handling already satisfies Atlassian guidance.

### 3.8 New: `delete_pull_request` (optional, not currently needed)

`sync.py` never deletes PRs. Skip unless PR lifecycle handling is added.

---

## 4. `sync.py`

### 4.1 `delete_branch` call (L240-243) — unchanged call site

`jira.delete_branch(meta.repo_id, transform.branch_id(gone))` already passes the
transformed id; after §3.3 the signature matches. No change beyond confirming the
`dry_run` guard is now inside `jira.delete_branch` (it is) so L241
`if not settings.dry_run:` becomes redundant but harmless — leave or drop.

### 4.2 `build_devinfo_payload` call (L250-255) — new args, drop usid

```python
first_sight = not rs.branches and not rs.last_success and not getattr(rs, "backfilled", False)
payload = transform.build_devinfo_payload(
    changes,
    prevent_transitions=settings.prevent_transitions,
    operation_type=("BACKFILL" if (first_sight and settings.backfill_on_first_sight) else "NORMAL"),
    properties={"repositoryId": str(meta.repo_id)},
    pattern=pattern,
)
```
- **Remove** `update_sequence_id=int(time.time() * 1000)` (L253) — `transform` now
  owns it.
- Update the module docstring line 26-27 ("``update_sequence_id`` = ...") to
  describe the per-entity scheme.

### 4.3 First-sight detection — where

- **Repo-level BACKFILL** (simplest, recommended): `first_sight` as in §4.2.
  Covers the "newly added repo, index its history" case the spec's BACKFILL text
  describes.
- After a successful push (L299 area), set `rs.backfilled = True` and
  `state.save(...)` so a repo that first syncs with zero keyed entities still
  flips out of BACKFILL next run.
- **Per-branch BACKFILL** (optional finer grain): `_process_branch` (L79-120)
  already knows `previous is None` (L98) = first encounter of a branch. Could
  return that flag up so a payload containing only brand-new branches uses
  BACKFILL even for an established repo. Not required; repo-level is enough and
  matches github-for-jira (which switches the whole backfill job to BACKFILL).

### 4.4 dedupe (L224-230) — keep, it now also feeds the usid map

The existing SHA dedupe of `changes.commits` (L226-230) is still needed so
`_Usid` sees each SHA once in `commits[]`; the branch `lastCommit` path dedupes
against it via the shared `("commit", sha)` key. No change.

### 4.5 chunk size (L262) — unchanged call, new default via config

`jira.push(payload, chunk_size=settings.push_chunk_size)` — no code change;
`settings.push_chunk_size` default becomes 400 (see §5).

---

## 5. `config.py`

### 5.1 Changed defaults

| setting | line | old | new | env |
|---|---|---|---|---|
| `push_chunk_size` | L77 / L156 | `5` | **`400`** | `SYNC_PUSH_CHUNK` |
| `prevent_transitions` | L73 / L152 | `True` | keep `True` | `SYNC_PREVENT_TRANSITIONS` |

`L156`: `push_chunk_size=max(0, _int(env, "SYNC_PUSH_CHUNK", 400))`.
(`0` still means "never chunk" — keep that escape hatch. Also clamp an
over-large value: `min(400, ...)` if `> 0`, since 400 is the spec ceiling.)

### 5.2 New settings

```python
# in Settings dataclass
backfill_on_first_sight: bool = True     # send operationType=BACKFILL the first time a repo is synced
send_issue_keys: bool = True             # keep the (non-deprecated) issueKeys array
send_associations: bool = True           # also send associations[issueIdOrKeys]
issue_key_cap: int = 500                 # per-entity cap on issueKeys + association values
```
`from_env` additions:
```python
backfill_on_first_sight=_bool(env, "SYNC_BACKFILL_FIRST_SIGHT", True),
send_issue_keys=_bool(env, "JIRA_SEND_ISSUE_KEYS", True),
send_associations=_bool(env, "JIRA_SEND_ASSOCIATIONS", True),
issue_key_cap=max(1, _int(env, "JIRA_ISSUE_KEY_CAP", 500)),
```
- `transform.build_devinfo_payload` takes `send_issue_keys` /
  `send_associations` / `issue_key_cap` (or read from a passed `Settings`); emit
  `issueKeys` only if `send_issue_keys`, `associations` only if
  `send_associations`. Guard against emitting neither (fall back to `issueKeys`).

### 5.3 No change needed

- `jira_token_url` default `https://api.atlassian.com/oauth/token` (L11) —
  **correct** for 2LO self-hosted. Keep.
- `jira_api_base` default `https://api.atlassian.com` (L12) — correct.
- audience `api.atlassian.com` is hard-coded in `jira.py` L143 — correct, leave.

---

## 6. Field-length / payload hygiene (applies in `transform.py`)

| field | cap | source |
|---|---|---|
| `commit.message`, `pr.title`, `*.description` | 1024 | docs (server truncates anyway; clip client-side to keep payload small) |
| `*.url` (commit/branch/PR/repo/source/dest) | 2000 | docs |
| `repo.name`, `pr.sourceBranch`, `pr.destinationBranch`, `displayId`, `hash` | 255 | docs |
| `branch.name` | 512 | docs |
| `*.id` (repo/commit/branch/pr) | 1024 | docs |
| `issueKeys` + all association `values` per entity | 500 total | schema ("must not exceed 500") |

All "(mirror)"-sourced in `devinfo-api.md §3` — **verify against the live
rendered docs before shipping the exact numbers**; the clipping is defensive so a
slightly-wrong cap only over-trims a pathological outlier.

`flags`: emit `["MERGE_COMMIT"]` when `commit.is_merge`. Only enum value defined.

`providerMetadata`: `{"product": "ghes-jira-devinfo-bridge/<version>"}` — already
present, keep.

---

## 7. Where spec and reference impls disagree (call-outs)

1. **`issueKeys` vs `associations`.** Docs steer toward `associations` and the
   newer sibling APIs (deployments, security) dropped bare `issueKeys`. **But**
   every devinfo client in the wild sends `issueKeys`, github-for-jira *disabled*
   commit `associations` ("ARC-2803"), and — confirmed against the live API
   2026-09-02 — the two are **mutually exclusive on one entity** (`400`,
   `issueKeysOrAssociationsOrNone.invalid`). → Send `issueKeys` by default;
   `JIRA_SEND_ISSUE_KEYS=false` switches to `associations`. Never both.
2. **`updateSequenceId` granularity.** github-for-jira = per-entity `Date.now()`;
   gitlab = one per submission (same bug as the bridge). Neither uses a
   deliberate `base+index`. → Plan picks `base+index` (stricter than both) as the
   only scheme that is provably collision-free for the shared-SHA case.
3. **`_updateSequenceId` on deletes.** github-for-jira always sends it; gitlab
   never does; spec says it is optional and acts as a "delete only if `<= X`"
   gate. → Plan sends it (`now_ms`) — it can only help ordering, never hurt.
4. **`operationType` / `preventTransitions` / `properties`.** Spec defines all
   three; gitlab sends none; github-for-jira sends all three. → Plan sends all
   three (`preventTransitions` stays `True` by config, which is a bridge-specific
   choice for a periodic re-sync, not a consensus value).
5. **Chunking branches/PRs.** github-for-jira repeats them on every commit chunk;
   the bridge puts them on the first chunk only. Spec is silent. → Keep
   first-chunk-only; it is fewer bytes and equally correct with stable ids.

---

## 8. Test implications

`tests/test_transform.py` (new/expand):
- USID: assert repository gets `base`, distinct entities get distinct
  `base+i`, and a SHA present both as `commits[]` and as a `branches[].lastCommit`
  gets the **same** `updateSequenceId` in both places.
- `associations`: assert `[{ "associationType": "issueIdOrKeys", "values": [...] }]`
  mirrors `issueKeys`; assert both suppressed correctly by config flags; assert
  never-both-empty fallback.
- caps: 2000-char url trimmed; >500 keys trimmed to 500 in both `issueKeys` and
  `associations[0].values`.
- `flags`: merge commit → `["MERGE_COMMIT"]`; non-merge → key absent.
- envelope: `operationType`, `properties={"repositoryId": ...}` present.
- `build_devinfo_payload` signature no longer takes `update_sequence_id`.

`tests/test_jira.py` (modify — already `M` in git):
- `delete_branch` now issues `DELETE
  .../repository/{repoId}/branch/{branchId}?_updateSequenceId=<int>` and accepts
  202/204; assert the old `bulkByProperties` URL is gone.
- `delete_commit` / `delete_repository` now send `_updateSequenceId`.
- `push` default `chunk_size` path: a 6-commit payload is **one** POST now
  (was chunked at 5).
- 413 → `JiraError` (no retry) with a "reduce chunk size" hint.

`tests/test_sync.py` (modify — already `M`):
- first sync of a repo → payload `operationType == "BACKFILL"`; second run →
  `"NORMAL"`; `rs.backfilled` persisted.
- no `update_sequence_id=` kwarg passed to `transform.build_devinfo_payload`.
- deleted-branch path calls the rewritten `jira.delete_branch` with the
  `transform.branch_id(...)` value.

`tests/test_main.py` (modify — already `M`):
- config: `SYNC_PUSH_CHUNK` unset → `push_chunk_size == 400`.
- new env vars parse (`SYNC_BACKFILL_FIRST_SIGHT`, `JIRA_SEND_ASSOCIATIONS`,
  `JIRA_SEND_ISSUE_KEYS`, `JIRA_ISSUE_KEY_CAP`).

`tests/test_models.py`:
- `Commit(is_merge=True)` default `False`; `RepoState.backfilled` default `False`.

---

## 9. Suggested change order (smallest blast radius first)

1. `models.py` (`Commit.is_merge`, `state.RepoState.backfilled`) + `ghes.py`
   one-liner to populate `is_merge`.
2. `config.py` (defaults + new settings).
3. `transform.py` (USID class, associations, caps, flags, new signature).
4. `sync.py` (call-site: drop usid arg, add operation_type/properties, first-sight).
5. `jira.py` (`delete_branch` rewrite, `_now_usid`, `_updateSequenceId` on
   commit/repo deletes, 413 message).
6. Tests.
