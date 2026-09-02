# Cross-forge devinfo-pusher implementation survey

Goal: find the *consensus* behaviour of real, production Jira Development
Information API writers, across GitHub, GitLab and Bitbucket/CI ecosystems, so the
bridge rewrite copies proven choices instead of guessing.

## Correction (2026-09-02)

This survey concluded "send both `issueKeys` and `associations`". That is wrong:
the rendered Cloud API reference badges `issueKeys` **DEPRECATED**, and the live
API `400`s any entity carrying **both** forms
(`issueKeysOrAssociationsOrNone.invalid`). The bridge now defaults to
`associations` (`associationType: issueIdOrKeys`) and only sends `issueKeys` when
`JIRA_SEND_ISSUE_KEYS=true`. Everything else below (chunk size 400, per-entity
`updateSequenceId`, per-entity deletes) stands.

## Implementations examined

| # | Project | Forge / role | Source pinned |
|---|---------|--------------|---------------|
| 1 | `atlassian/github-for-jira` | GitHub (Atlassian's own Connect app) | commit `332b5ae08042c631fdce6465386a6c338e819d99` (2024-01-09) — `main` is now a deprecation stub (repo moved private Feb 2024), this is the last real commit reachable via the GitHub API / `raw.githubusercontent.com` |
| 2 | `gitlab-org/gitlab` | GitLab (GitLab for Jira Connect app) | `master`, `lib/atlassian/jira/dev_info_client.rb`, `lib/atlassian/jira_connect/client.rb`, `lib/atlassian/jira_connect/serializers/*_entity.rb` |
| 3 | `CircleCI-Public/jira-connect-orb` | CI → Jira (builds/deployments) | `master`, `src/commands/notify.yml` |
| 4 | `jenkinsci/atlassian-jira-software-cloud-plugin` | Jenkins → Jira (builds/deployments) | `master`, `src/main/java/com/atlassian/jira/cloud/jenkins/common/client/JiraApi.java` (partial — file is a thin generic client) |

> Nos. 3 & 4 are builds/deployments pushers, not devinfo — included for the shared
> patterns (`updateSequenceNumber`, `properties`, `associations`, 202 handling,
> 429 handling) the task asked about.

---

## 1. `atlassian/github-for-jira` @ `332b5ae`

### Payload assembly — `src/jira/client/jira-client.ts` (`batchedBulkUpdate` / `updateJira`)

```ts
// dedupe commits by id, then chunk at 400
const dedupedCommits = dedupCommits(data.commits);          // ~line 587
const commitChunks: JiraCommit[][] = [];
do {
  commitChunks.push(dedupedCommits.splice(0, 400));         // ~line 611-613
} while (dedupedCommits.length);

const body = {                                              // ~line 615-625
  preventTransitions: options?.preventTransitions || false,
  operationType: options?.operationType || "NORMAL",
  repositories: [data],
  properties: { installationId }
};
// POST to  instance.post("/rest/devinfo/0.10/bulk", body)  // ~line 355 / re-used per chunk
```

- **Chunk size = 400 commits.** Matches the spec ceiling exactly.
- **`data` (the single repository object) is reused across chunks**, only
  `data.commits` is swapped per iteration → `branches` and `pullRequests` go out
  **on every chunk** (they are never cleared). So github-for-jira does *not* put
  branches/PRs "on the first chunk only"; each POST carries the full branch/PR set
  plus one 400-commit slice. Safe because branch/PR entity ids are stable and the
  repeated POSTs just re-assert them with a fresh `updateSequenceId`.
- **`operationType`** is passed through from the caller; `"NORMAL"` default,
  `"BACKFILL"` supplied by the backfill workers (`src/sync/*`, `src/backfill/*`).
- **`properties: { installationId }`** — a single stable key, used later for
  `DELETE /bulkByProperties?installationId=…` on app uninstall.
- **`preventTransitions`** passed through, default `false`.
- **No repo-level `_updateSequenceId` in the bulk body** at this commit (older
  versions had `_updateSequenceId: Date.now()` on the repo object; the transform
  layer now stamps `updateSequenceId` on the repo and every entity instead).

### `updateSequenceId` — per entity, `Date.now()`

- `src/transforms/transform-commit.ts:16` → `updateSequenceId: Date.now()`
- `src/sync/transforms/branch.ts:69` → branch `updateSequenceId: Date.now()`
- `src/sync/transforms/branch.ts:62` → **`lastCommit.updateSequenceId: Date.now()`
  — a separate `Date.now()` call** from the branch's own, and from any standalone
  `commits[]` copy of the same SHA.
- `src/transforms/transform-pull-request.ts:113` → PR `updateSequenceId: Date.now()`

So: **one `Date.now()` per entity object, evaluated at build time.** Not a single
value for the whole submission. Collisions between the standalone commit and the
`branches[].lastCommit` copy are possible only if both `Date.now()` calls land in
the same millisecond; in practice the second write usually has a strictly greater
value and wins. It is *not* a deliberate offset scheme — it just relies on wall
clock advancing.

### issueKeys — `issueKeys[]`, no `associations` for devinfo entities

- commit: `issueKeys: jiraIssueKeyParser(commit.message)` (`transform-commit.ts:9`)
- branch: `issueKeys: union(branchKeys, pullRequestKeys, commitKeys)` where the
  three come from `jiraIssueKeyParser()` over branch name, associated PR title,
  last-commit message (`src/sync/transforms/branch.ts:46-50`)
- PR: `issueKeys: extractIssueKeysFromPrRest(title, headRef, body)`
  (`transform-pull-request.ts:107`)
- **`associations` are NOT emitted for commits/branches/PRs.** Commit `associations`
  were explicitly turned off — the pinned commit's message is literally
  *"ARC-2803 skip sending commit association for now (#2626)"*. github-for-jira
  *has* association plumbing (`jira-client-issue-key-helper.ts`) that caps both
  `issueKeys` arrays and `association.values` arrays where `associationType ===
  "issueIdOrKeys"`, but for devinfo the shipped payload uses `issueKeys`.

### issue-key cap

`src/jira/client/jira-client-issue-key-helper.ts`:
```ts
export const ISSUE_KEY_API_LIMIT = 500;
const truncate = (array) => array.slice(0, ISSUE_KEY_API_LIMIT);
```
Applied by `updateRepositoryIssueKeys(repositoryObj, truncate)` across
`commits`, `branches` (incl. nested `lastCommit`), and `pullRequests`, to both
`issueKeys` arrays and `issueIdOrKeys` association `values`.

### Delete strategy — per-entity endpoint, with `_updateSequenceId`

`src/jira/client/jira-client.ts`:
```ts
// branch  (~line 493-502)
instance.delete("/rest/devinfo/0.10/repository/{transformedRepositoryId}/branch/{branchJiraId}",
                { params: { _updateSequenceId: Date.now() } });
// pull request  (~line 520-529)
instance.delete("/rest/devinfo/0.10/repository/{transformedRepositoryId}/pull_request/{pullRequestId}",
                { params: { _updateSequenceId: Date.now() } });
// whole repository  (~line 538-558)
instance.delete("/rest/devinfo/0.10/repository/{transformedRepositoryId}",
                { params: { _updateSequenceId: Date.now() } });
// app uninstall — by properties  (~line 505-532)
instance.delete("/rest/devinfo/0.10/bulkByProperties",   { params: { installationId } });
instance.delete("/rest/builds/0.1/bulkByProperties",     { params: { gitHubInstallationId } });
instance.delete("/rest/deployments/0.1/bulkByProperties", { params: { gitHubInstallationId } });
```
- Per-entity deletes use the **`repository/{id}/{entityType}/{entityId}`** endpoint
  (`branch`, `pull_request`), **always with `?_updateSequenceId=Date.now()`**.
- `bulkByProperties` is used **only** for the "nuke everything for this
  installation" case, keyed by the `properties.installationId` that was sent on
  every `POST /bulk`. It is *never* used to delete a single branch.

### Response handling

- Reads `response.data["unknownIssueKeys"]`, hashes them, logs. Does not fail the
  sync on unknown keys.
- No retry inside `batchedBulkUpdate` — retry/429 handling lives in the shared
  Axios interceptor (`src/jira/client/axios.ts`), which retries `429`/`5xx` with
  backoff and honours `Retry-After`.
- Does not assert on `acceptedDevinfoEntities`; does not act on
  `failedDevinfoEntities` beyond logging.

### PR status mapping

`MERGED` (merged) · `OPEN` (open, non-draft) · `DRAFT` (open draft) · `DECLINED`
(closed unmerged) · `UNKNOWN` (fallback).

---

## 2. `gitlab-org/gitlab` (GitLab for Jira Connect app)

### Client — `lib/atlassian/jira/dev_info_client.rb`

```ruby
def store_dev_info(project:, commits: nil, branches: nil, merge_requests: nil, update_sequence_id: nil)
  # builds a single RepositoryEntity and POSTs:
  #   { repositories: [repo], providerMetadata: { product: "GitLab #{Gitlab::VERSION}" } }
  # to  build_uri('/rest/devinfo/0.10/bulk')
end

def self.generate_update_sequence_id
  (Time.now.utc.to_f * 1000).round
end

def remove_branch_info(project_id, branch_name)
  # DELETE /rest/devinfo/0.10/repository/{project_id}/branch/{Digest::SHA256.hexdigest(branch_name)}
end
```

- **Path:** `/rest/devinfo/0.10/bulk`.
- **Body:** `repositories: [repo]` + `providerMetadata: { product: "GitLab <ver>" }`.
  **No `preventTransitions`, no `operationType`, no `properties`.**
- **No `.each_slice` in this class.** Volume is bounded upstream instead: the sync
  workers cap the initial sync at the **latest 400 commits** per repo
  (`gitlab-org/gitlab` work-items 419738 / MR that introduced the 400 limit), and
  branches/MRs are synced in their own worker passes. So GitLab's effective
  "chunk size" is 400, enforced before serialization.
- N+1 warning: `CommitEntity` does a Gitaly call per commit — a known perf issue,
  another reason they cap at 400.

### `updateSequenceId` — ONE value per submission

`lib/atlassian/jira_connect/serializers/base_entity.rb`:
```ruby
expose :update_sequence_id, as: :updateSequenceId do |_, options|
  options[:update_sequence_id] || Client.generate_update_sequence_id
end
```
`repository_entity.rb` computes it once and **passes the same
`options[:update_sequence_id]` down to `CommitEntity`, `BranchEntity`,
`PullRequestEntity`**. So every entity *and* the repository in one submission
carry an **identical** `updateSequenceId` — the same design the bridge currently
has, including the `branches[].lastCommit` vs standalone-`commits[]` collision.
GitLab gets away with it because it rarely sends the same SHA as both a standalone
commit and a branch head in the same payload (separate worker passes).

### issueKeys — `issueKeys[]` via `JiraIssueKeyExtractor`, no `associations`

- `commit_entity.rb`: `issueKeys { |c| JiraIssueKeyExtractor.new(c.safe_message).issue_keys }`
- `branch_entity.rb`: extractor over branch name (+ last commit)
- `pull_request_entity.rb`: `JiraIssueKeyExtractor.new(mr.title, mr.description).issue_keys`
  (title + description only — **not** the branch)
- **No `associations` array anywhere.** No explicit 500 cap in the serializers.

### Extras

- `commit_entity.rb` flags: `commit.merge_commit? ? ['MERGE_COMMIT'] : []`
- PR status map: `opened`→`OPEN`, `locked`→`OPEN`, `merged`→`MERGED`,
  `closed`→`DECLINED`, else `UNKNOWN`.
- Branch delete: **per-entity** `DELETE /rest/devinfo/0.10/repository/{project_id}/branch/{sha256hex(branch_name)}`,
  **no `_updateSequenceId` query param**. Branch id = `Digest::SHA256.hexdigest(name)`
  (github-for-jira uses a reversible char-escape instead; both are just
  "make it URL-safe and stable").

---

## 3. `CircleCI-Public/jira-connect-orb` (`src/commands/notify.yml`)

- Does **not** call `api.atlassian.com` directly — POSTs to CircleCI's proxy:
  `https://circleci.com/api/v1.1/project/{vcs}/{org}/{repo}/jira/<job_type>`
  (`job_type` ∈ `build` | `deployment`), CircleCI forwards to Jira.
- **Build payload:** `builds: [ { schemaVersion: "1.0", …, updateSequenceNumber:
  "<unix ts>", issueKeys: [...] } ]` — uses **`issueKeys`**, **no `operationType`**.
- **Deployment payload:** `deployments: [ { …, associations: [ { associationType:
  "issueKeys", values: [...] }, { associationType: "serviceIdOrKeys", values:
  [...] } ], environment: { id, displayName, type } } ]` — uses **`associations`**.
- `updateSequenceNumber` = unix timestamp string, one value per submission.
- Default issue-key regex: `[A-Z]{2,30}-[0-9]+`.

Confirms the ecosystem split: **build-style entities → `issueKeys`;
deployment-style entities → `associations`.**

---

## 4. `jenkinsci/atlassian-jira-software-cloud-plugin`

- `JiraApi.java` is a thin generic client: `String.format(this.apiEndpoint,
  cloudId)` where `apiEndpoint` is configured per API to
  `https://api.atlassian.com/jira/<type>/0.1/cloud/%s/bulk`.
- 429 handling: catches resilience4j `RequestNotPermitted` and surfaces
  *"Your OAuth client reached Jira's limits"* — i.e. it rate-limits itself
  client-side and does **not** blindly retry.
- Response is deserialized into per-API result POJOs
  (`BuildApiResponse` / `DeploymentApiResponse`) that expose
  `acceptedBuilds` / `rejectedBuilds` / `unknownIssueKeys` /
  `unknownAssociations` and the plugin logs rejected entities' error messages.
- Auth: OAuth 2.0 client-credentials against `https://api.atlassian.com/oauth/token`
  with `audience=api.atlassian.com` — identical to the bridge.

---

## Consensus matrix

| dimension | github-for-jira | gitlab | CircleCI orb | Jenkins plugin | **consensus / safe default** |
|-----------|-----------------|--------|--------------|----------------|------------------------------|
| linkage field (devinfo entities) | `issueKeys[]` | `issueKeys[]` | `issueKeys[]` (builds) | `issueKeys[]` (builds) | **`issueKeys[]` still works and is what everyone ships.** Add `associations` (`issueIdOrKeys`) alongside for forward-compat — do **not** drop `issueKeys`. |
| linkage field (deployment/security-style) | n/a | n/a | `associations` | `associations` | `associations` with `issueIdOrKeys` |
| `updateSequenceId` scheme | **per-entity `Date.now()`** | **one value per submission** | one per submission | one per submission | **No clean consensus.** github-for-jira's per-entity approach is the only one that avoids the `lastCommit` vs `commits[]` collision. → bridge should go **per-entity, monotonic** (base `now_ms` + index), which is stricter than either and strictly spec-correct. |
| chunk / batch size | **400 commits** (`splice(0,400)`) | **400 commits** (capped before serialize) | n/a (1 build) | n/a | **400** (the spec ceiling). Chunking below that is unnecessary. |
| branches/PRs per chunk | sent on **every** chunk (shared `data` obj) | single submission | n/a | n/a | If you chunk at all: repeat branches/PRs on every chunk is *fine* (stable ids, fresh USID). Bridge's "first chunk only" also fine. Don't split branch entities across chunks. |
| `operationType` | pass-through, `NORMAL` default, `BACKFILL` on initial import | omitted | omitted | omitted | **Send `NORMAL` normally, `BACKFILL` on first sight of a repo/branch.** Harmless to include. |
| `preventTransitions` | pass-through, default `false` | omitted (→ `false`) | omitted | omitted | Bridge default `true` is a deliberate, defensible choice for a periodic re-sync; keep it configurable. |
| `properties` | `{ installationId }` | omitted | `{ accountId, projectId }` (builds) | varies | **Include one stable key** (e.g. `{ repositoryId }` or a bridge instance id) so `DELETE /bulkByProperties` recovery is possible. ≤5 keys, no leading `_`. |
| `providerMetadata.product` | yes | `"GitLab <ver>"` | yes | yes | **yes** — `"ghes-jira-devinfo-bridge/<ver>"` |
| delete a branch | `DELETE repository/{id}/branch/{id}?_updateSequenceId=Date.now()` | `DELETE repository/{id}/branch/{sha256}` (no USID) | n/a | n/a | **per-entity `repository/{repoId}/branch/{branchId}` endpoint.** `bulkByProperties` is uninstall-only. **Include `?_updateSequenceId=<now_ms>`** (github-for-jira does; it's the spec's ordering guard). |
| delete a commit | `DELETE repository/{id}/commit/{id}?_updateSequenceId=` | (not deleted individually) | n/a | n/a | per-entity `.../commit/{id}?_updateSequenceId=<now_ms>` |
| `_updateSequenceId` on deletes | **yes, `Date.now()`** | no | n/a | n/a | **yes** — pass `int(time.time()*1000)` |
| merge-commit flag | `MERGE_COMMIT` when `>1` parent | `MERGE_COMMIT` when `commit.merge_commit?` | n/a | n/a | **emit `flags:["MERGE_COMMIT"]`** for merges |
| issue-key cap per entity | **500** (`ISSUE_KEY_API_LIMIT`), truncate | none explicit | none | none | **truncate `issueKeys` + association `values` to 500** total per entity |
| PR status values | `OPEN/DRAFT/MERGED/DECLINED/UNKNOWN` | `OPEN/MERGED/DECLINED/UNKNOWN` | n/a | n/a | `OPEN/MERGED/DECLINED` (+ `UNKNOWN` fallback); `DRAFT` optional |
| retry / 429 | shared Axios interceptor: retry 429/5xx, honour `Retry-After` | HTTParty + retry | self-throttle, no retry | self-throttle (resilience4j) | **retry 429/5xx, honour `Retry-After`, exp backoff + jitter, cap ~4** — bridge already does this |
| act on `unknownIssueKeys` | log only | log only | — | log rejected | **log only** — informational (issue not created yet / typo) |
| act on `failedDevinfoEntities` | log only | — | — | log error message | **log at WARN/ERROR** — this is a real payload problem |
| verify `acceptedDevinfoEntities` | no | no | no | no | not required; async anyway |

### The two hard "safe defaults" for the rewrite

1. **Linkage: `associations`, not `issueKeys`.** Superseded — see the
   `## Correction` note at the top of this file. The rendered Cloud API docs badge
   `issueKeys` DEPRECATED, and the live API rejects sending both `issueKeys` and
   `associations` on one entity. Default to `associations` (`issueIdOrKeys`);
   keep `issueKeys` only as an opt-in fallback.
2. **Per-entity monotonic `updateSequenceId`.** Only github-for-jira avoids the
   shared-SHA collision, and only by luck (wall clock). Do it deliberately:
   `base = int(time.time()*1000)`, then assign `base + i` to the i-th distinct
   entity in a stable order, with the `branches[].lastCommit` copy of a SHA using
   the **same** value as its standalone `commits[]` copy (dedupe to one id → one
   USID) — see `devinfo-rewrite-notes.md`.
