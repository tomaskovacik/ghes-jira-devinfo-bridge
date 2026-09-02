# Jira Cloud Development Information API — current spec (captured 2026-09-02)

> **Source-quality note.** The authoritative machine-readable spec
> (`https://developer.atlassian.com/cloud/jira/software/swagger.v3.json`) **does not
> contain the devinfo/builds/deployments/featureflags paths** — that file is only
> boards / sprints / issues. The DevOps "integration" APIs are rendered from a
> separate spec that is not exposed as a standalone `.json` at a guessable URL, and
> the rendered docs page is JS-driven so `WebFetch` only ever sees a partial DOM.
> Everything below is assembled from: the rendered reference docs, the
> "Integrate with self-hosted tools" pages, a third-party schema mirror
> (`withone.ai/knowledge/jira/...`, which paraphrases the Atlassian docs), the
> Atlassian rate-limiting page, and cross-checked against the two canonical
> first-party client implementations (github-for-jira, gitlab). **Field-length
> numbers marked "(mirror)" are corroborated across ≥2 sources but should be
> re-verified against the live docs before relying on an exact cutoff.**

## Source URLs

- https://developer.atlassian.com/cloud/jira/software/rest/api-group-development-information/
- https://developer.atlassian.com/cloud/jira/software/integrate-jsw-cloud-with-onpremises-tools/
- https://developer.atlassian.com/cloud/jira/software/rest/api-group-deployments/
- https://developer.atlassian.com/cloud/jira/software/rest/api-group-builds/
- https://developer.atlassian.com/cloud/jira/software/rest/api-group-feature-flags/
- https://developer.atlassian.com/cloud/jira/software/rest/api-group-securityinfo/
- https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
- https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/
- https://support.atlassian.com/jira-cloud-administration/docs/integrate-with-self-hosted-tools-using-oauth/
- https://developer.atlassian.com/cloud/jira/software/swagger.v3.json  (does NOT contain devinfo)
- https://www.withone.ai/knowledge/jira/conn_mod_def::GJ4qTWi01wk::wWhglM-YSsGoi44pGT5mTQ  (schema mirror)
- https://www.withone.ai/knowledge/jira/conn_mod_def::GJ4qTWOiTzM::ZXgmrphLSq28Ki6EUPxF1A  (deleteEntity mirror)
- https://community.developer.atlassian.com/t/jira-devinfo-api/78068
- https://community.developer.atlassian.com/t/devinfo-rest-api-authentication/75956

---

## 1. Auth model (OAuth 2.0 client-credentials / 2LO for self-hosted tools)

From `integrate-jsw-cloud-with-onpremises-tools/` and the support page:

- **Token URL:** `https://api.atlassian.com/oauth/token`
- **Grant type:** `client_credentials`
- **Audience:** `api.atlassian.com`
- **Token lifetime:** *"It's set to 15 minutes, but you should check the value in the [`expires_in`] response."* No refresh token in the 2LO flow — just request a new token.
- **Credentials are created inside Jira**, not the developer console:
  Jira → **Settings → Apps → OAuth credentials → Create credentials**
  (older path: *Settings → Marketplace apps → OAuth credentials*). You supply
  App name, Data Center base URL, Logo URL, and tick **Permissions** (Builds,
  Deployments, Development information, Feature flags, Remote Links). Jira returns
  a client id + client secret. This is a system-to-system integration that does
  not depend on a user account.
- The classic 3LO scope strings (`read:jira-work`, `write:jira-work`,
  `offline_access`) and `auth.atlassian.com` **do not apply** here — the self-hosted
  OAuth credential's capability is set by the checkboxes at creation time, and the
  granular equivalents are `read:dev-info:jira` / `write:dev-info:jira` (Connect
  `READ` / `WRITE`). An Atlassian staff reply in community thread 75956 confirms
  devinfo needs the credential minted by the in-product integration procedure, not
  a normal OAuth app.

Token request body:

```json
{
  "audience": "api.atlassian.com",
  "grant_type": "client_credentials",
  "client_id": "CLIENT_ID",
  "client_secret": "CLIENT_SECRET"
}
```

### Cloud ID resolution

`GET https://<site>.atlassian.net/_edge/tenant_info` → `{ "cloudId": "<uuid>", ... }`
(no auth required). 3LO alternative: `GET https://api.atlassian.com/oauth/token/accessible-resources` with the bearer token.

### Base path

```
https://api.atlassian.com/jira/<type>/0.1/cloud/<cloudId>/<op>
```
`<type>` ∈ `devinfo | builds | deployments | featureflags`.
The Connect variant of the same API is `.../rest/devinfo/0.10/<op>` (note **0.10**,
not 0.1) reached via `https://api.atlassian.com/ex/jira/<cloudId>/rest/devinfo/0.10/...`.
Both variants share the same request/response schemas; the reference clients
(github-for-jira, gitlab) are Connect apps and hit `/rest/devinfo/0.10/...`.

---

## 2. Development Information operations

Paths shown in the OAuth-2LO form (`https://api.atlassian.com/jira/devinfo/0.1/cloud/{cloudId}` prefix omitted).

### 2.1 Store development information — `POST /bulk`

- **Request body:** `DevInformationUpdate` (see §3).
- **OAuth scope:** `write:dev-info:jira` (Connect `WRITE`). The credential must
  correspond to an app that defines the `jiraDevelopmentTool` module.
- **Responses:**
  | code | meaning (verbatim / paraphrased from docs) |
  |------|-------------------------------------------|
  | **202 Accepted** | *"Submission accepted. Each submitted repository and entity that is of a valid format will be eventually available in Jira."* Body = `StoreDevinfoResult` (see §4). |
  | **400 Bad Request** | Request payload was invalid / malformed JSON / a required field missing / a field exceeded its constraints. |
  | **401 Unauthorized** | Missing or invalid JWT / bearer token. |
  | **403 Forbidden** | The token's app does not define the `jiraDevelopmentTool` module, or lacks the `WRITE` scope. |
  | **413 Payload Too Large** | Request body exceeded the maximum accepted size. Split into smaller submissions. |
  | **429 Too Many Requests** | OAuth client hit Jira's rate limits. Honour `Retry-After`. |
  | **503 Service Unavailable** | Jira temporarily could not accept the submission (peak load / maintenance). Retry with backoff. |
  | **500 Internal Server Error** | Also observed in the rendered docs' response list. |

- **Async processing (verbatim):** *"Submissions are performed asynchronously.
  Submitted data will eventually be available in Jira; most updates are available
  within a short period of time, but may take some time during peak load and/or
  maintenance times."* The 202 is only a *format* acceptance — an entity listed in
  `acceptedDevinfoEntities` can still fail later association processing.

### 2.2 Get repository — `GET /repository/{repositoryId}`

- **Path param:** `repositoryId` (string).
- **Scope:** `read:dev-info:jira` (Connect `READ`).
- **Behaviour (verbatim, mirror):** *"retrieves the repository and the most recent
  **400** development information entities"*, and returns *"what is currently stored
  in Jira, ignoring any pending updates or deletes."*
  → You cannot use this endpoint to read back a full large repo; it is a spot-check
  of the newest 400 commits/branches/PRs combined.
- **Responses:** 200, 400, 401, 403, 404 (nothing stored for that id), 500.

### 2.3 Delete repository — `DELETE /repository/{repositoryId}`

- **Path param:** `repositoryId`.
- **Query param:** `_updateSequenceId` (integer, optional) — same semantics as in
  §2.4 (only data with `updateSequenceId` ≤ the supplied value is removed).
- **Scope:** `write:dev-info:jira` (`WRITE`).
- **Responses:** **202 Accepted** (async) / 204 (some docs), 400, 401, 403, 404, 500.
- Purges the repository plus all its commits, branches, PRs. Used for recovery when
  a repo's async processing is wedged.

### 2.4 Delete a development-information entity — `DELETE /repository/{repositoryId}/{entityType}/{entityId}`

(mirror page `conn_mod_def::GJ4qTWOiTzM`)

- **Path params:**
  - `repositoryId` (string)
  - `entityType` — **enum: `commit` | `branch` | `pull_request`** (literal
    `pull_request` with an underscore)
  - `entityId` (string) — the entity's `id` as it was submitted.
- **Query param:** `_updateSequenceId` (integer, optional) — verbatim (mirror):
  *"Only stored data with an updateSequenceId **≤** this value will be deleted.
  Useful to ensure submit/delete ordering when requests occur close together."*
  → to actually delete, pass a value **≥ every stored `updateSequenceId`** for that
  entity (i.e. `int(time.time()*1000)` at delete time). Omitting it deletes
  unconditionally. github-for-jira always passes `Date.now()`.
- **Scope:** `write:dev-info:jira` (`WRITE`).
- **Responses:**
  - **202 Accepted** — *"Deletion request has been accepted. Data will eventually be
    removed from Jira if it exists."* (also 204 in some renderings)
  - **400** — wrong `entityType`.
  - **401** — bad token.
  - **403** — token's app lacks `jiraDevelopmentTool` / `DELETE` scope.
  - **404** — repository/entity unknown (not always returned since delete is async).

### 2.5 Delete by properties — `DELETE /bulkByProperties`

- **No path params.** Selection is entirely by **query string**, where each
  query pair is matched against the top-level `properties` object that was sent on
  the original `POST /bulk`. *Repositories whose `properties` match **ALL** supplied
  pairs are deleted, together with their commits/branches/PRs.*
- Recognised query params (from github-for-jira + builds/deployments siblings):
  arbitrary property keys you defined (e.g. `installationId=123`,
  `repositoryId=456`, `accountId=...`, `projectId=...`), **plus**
  `_updateSequenceId` (integer, optional) with the same "≤ this value" rule as §2.4.
- **Scope:** `write:dev-info:jira` (`WRITE`).
- **Responses:** 202, 400, 401, 403, 413, 429, 503.
- **This is NOT a per-branch delete.** It matches whole repositories by their
  submission `properties`. The bridge's current `delete_branch` mis-uses it.

### 2.6 Check exists by properties — `GET /existsByProperties`

- Query params: property key/value pairs (same matching as §2.5).
- Returns whether any devinfo data exists that matches ALL supplied properties.
- **Scope:** `read:dev-info:jira` (`READ`).
- **Responses:** 200, 400, 401, 403.

### 2.7 Get by properties — `GET /byProperties` (a.k.a. "Get development information for the supplied properties")

- Query params: property key/value pairs.
- Returns the matching devinfo (bounded, like §2.2 to the most recent entities).
- **Scope:** `read:dev-info:jira` (`READ`).
- **Responses:** 200, 400, 401, 403.

---

## 3. Request schema tree — `DevInformationUpdate` (the `POST /bulk` body)

```
DevInformationUpdate
├─ repositories        array<Repository>   REQUIRED
├─ preventTransitions  boolean             default false
├─ operationType       string enum         default "NORMAL"   (NORMAL | BACKFILL)
├─ properties          object<string,string>  0..5 pairs
└─ providerMetadata    ProviderMetadata
```

### Top-level fields

- **`repositories`** — required, array of `Repository`. Each is validated
  individually; a malformed one is rejected without failing the others.
- **`preventTransitions`** *(boolean, default `false`)* — verbatim intent:
  *"When true, the submitted development information will be stored but will NOT
  cause any Jira issue workflow transitions (e.g. smart-commit `#close`
  transitions) to fire."* Set **true** for a bulk backfill / periodic re-sync so
  historical data does not re-trigger automation.
- **`operationType`** *(string enum, default `NORMAL`)* — verbatim:
  *"The type of operation performed by the provider system. `NORMAL` is used for
  data received during normal operation of the system (e.g. a user pushing a
  branch). `BACKFILL` is used for data received while backfilling existing data
  (e.g. indexing a newly-connected account)."*
  Practical effect of `BACKFILL`: Jira treats the batch as lower-priority / bulk —
  it is queued on the backfill lane, may be processed more slowly, and (per
  github-for-jira usage) is the mode used during the initial import of a repo's
  history. It does **not** change the payload shape. `preventTransitions` is
  usually paired with it.
- **`properties`** *(object, string→string)* — verbatim (docs / siblings):
  *"Optional properties associated with the repositories being submitted. These
  are used for delete-by-properties and exists-by-properties. **A maximum of 5
  properties** is allowed. Property keys must match `^[a-zA-Z0-9_.\-]+$` and keys
  **beginning with an underscore (`_`) are reserved for internal use** (e.g.
  `_updateSequenceId`)."* Values are compared as strings. github-for-jira sends
  `{ "installationId": <n> }`; the CircleCI orb build path sends
  `{ "accountId": ..., "projectId": ... }`.
- **`providerMetadata`** — `{ "product": string }` — free-text identifier of the
  submitting product/version, shown in the Jira dev panel tooltip. e.g.
  `"GitLab 16.9"`, `"Bamboo 6.10.2"`.

### `Repository`

| field | type | constraint (mirror) | notes |
|-------|------|---------------------|-------|
| `id` | string | ≤ 1024, charset `[A-Za-z0-9\-._~]+` | REQUIRED. Stable provider id for the repo. |
| `name` | string | ≤ 255 | REQUIRED. `org/repo` is fine. |
| `description` | string | ≤ 1024 | optional |
| `url` | string | ≤ 2000 | repo web URL |
| `avatar` | string | ≤ 2000 | optional |
| `avatarDescription` | string | ≤ 1024 | optional |
| `forkOf` | string | ≤ 1024 | id of parent repo if a fork |
| `updateSequenceId` | integer (int64) | — | REQUIRED. See §5. |
| `commits` | array<Commit> | — | optional |
| `branches` | array<Branch> | — | optional |
| `pullRequests` | array<PullRequest> | — | optional |

### `Commit`

| field | type | constraint (mirror) | notes |
|-------|------|---------------------|-------|
| `id` | string | ≤ 1024, charset alnum + `~ . - _` | REQUIRED. The commit SHA. |
| `hash` | string | ≤ 255 | **DEPRECATED — "use `commits[].id`"**. Historically the SHA; keep sending it equal to `id` for older consumers, or drop. |
| `message` | string | ≤ 1024 (**longer values truncated server-side**) | REQUIRED |
| `author` | `Author` | — | REQUIRED |
| `authorTimestamp` | string (ISO-8601 UTC) | — | REQUIRED |
| `displayId` | string | ≤ 255 | REQUIRED. Short SHA (`id[:7]`). |
| `fileCount` | integer | — | total files added+removed+modified |
| `files` | array<FileData> | ≤ 10 items (siblings) | optional, per-file diff detail |
| `url` | string | ≤ 2000 | commit web URL |
| `updateSequenceId` | integer (int64) | — | REQUIRED. See §5. |
| `flags` | array<string> | enum: **`MERGE_COMMIT`** | optional. Only value currently defined. |
| `issueKeys` | array<string> | each key ≤ 255; list capped at 500 (see §6) | **DEPRECATED** in the rendered Cloud API docs — use `associations`. Still accepted and still what github-for-jira / gitlab ship. **Mutually exclusive with `associations` — sending both 400s the request** (see §6). |
| `associations` | array<Association> | total `values` ≤ 500 | The non-deprecated linkage form (`associationType: "issueIdOrKeys"`); never send alongside `issueKeys`. See §6. |

### `Branch`

| field | type | constraint (mirror) | notes |
|-------|------|---------------------|-------|
| `id` | string | ≤ 1024, charset alnum + `~ . - _` | REQUIRED. Provider branch id — must be URL-safe because it becomes a path segment in `DELETE .../branch/{id}`. |
| `name` | string | ≤ **512** | REQUIRED. Raw branch name (may contain `/`). |
| `url` | string | ≤ 2000 | branch web URL |
| `createPullRequestUrl` | string | ≤ 2000 | optional ("create PR from this branch" link) |
| `lastCommit` | `Commit` | — | REQUIRED. A full nested `Commit` object (its own `id`, `updateSequenceId`, etc). |
| `issueKeys` | array<string> | — | see §6 |
| `associations` | array<Association> | — | see §6 |
| `updateSequenceId` | integer (int64) | — | REQUIRED. See §5. |

### `PullRequest`

| field | type | constraint (mirror) | notes |
|-------|------|---------------------|-------|
| `id` | string | ≤ 1024 | REQUIRED. PR number as string. |
| `status` | string enum | **`OPEN` \| `MERGED` \| `DECLINED` \| `UNKNOWN`** (github-for-jira also emits `DRAFT`) | REQUIRED |
| `title` | string | ≤ 1024 | REQUIRED |
| `url` | string | ≤ 2000 | REQUIRED |
| `author` | `Author` | — | REQUIRED |
| `commentCount` | integer | — | optional |
| `sourceBranch` | string | ≤ 255 | optional |
| `sourceBranchUrl` | string | ≤ 2000 | optional |
| `destinationBranch` | string | ≤ 255 | optional |
| `destinationBranchUrl` | string | ≤ 2000 | optional |
| `displayId` | string | ≤ 255 | optional (e.g. `#123`) |
| `lastUpdate` | string (ISO-8601 UTC) | — | REQUIRED |
| `reviewers` | array<Reviewer> | — | optional |
| `issueKeys` | array<string> | — | see §6 |
| `associations` | array<Association> | — | see §6 |
| `updateSequenceId` | integer (int64) | — | REQUIRED. See §5. |

### `Association`

```json
{ "associationType": "issueIdOrKeys", "values": ["ABC-1", "10001"] }
```

- **`associationType`** — string enum. For Jira issues:
  - **`issueKeys`** — values are issue *keys* only. **Older form.**
  - **`issueIdOrKeys`** — values may be issue keys **or** numeric issue ids.
    **This is the current recommended type.**
  - (`serviceIdOrKeys` also exists, used by Deployments, not devinfo.)
- **`values`** — array of strings. *"The number of values counted across all
  `associationType`s must not exceed a limit of **500**."* `minItems` 1.
- The separate `EntityAssociation` schema (`associationType` ∈ `commit` |
  `repository`, values are `{commitHash,repositoryId}` / `{repositoryId}`,
  `maxItems` 500, each string ≤ 255) is for associating *builds/deployments to
  commits*, not for issue linkage — not needed by the bridge.

### `Author` / `Reviewer`

| field | type | constraint (mirror) | notes |
|-------|------|---------------------|-------|
| `name` | string | ≤ 255 | "Deprecated display name" per mirror, but still the primary human-readable field; keep sending. |
| `email` | string | ≤ 255 (Reviewer: ≤ 254) | **The field Jira uses to map to a Jira user.** Most important. |
| `username` | string | ≤ 255 | deprecated |
| `url` | string | ≤ 2000 | deprecated |
| `avatar` | string | ≤ 2000 | deprecated |
| `accountId` | string | ≤ 128 | Atlassian account id (AAID), if known |
| `approvalStatus` | string enum | `APPROVED` \| `UNAPPROVED` (default `UNAPPROVED`) | **Reviewer only** |

### `FileData`

| field | type | constraint (mirror) |
|-------|------|---------------------|
| `path` | string | ≤ 1024 |
| `url` | string | ≤ 2000 |
| `changeType` | string enum | `ADDED` \| `COPIED` \| `DELETED` \| `MODIFIED` \| `MOVED` \| `UNKNOWN` |
| `linesAdded` | integer | — |
| `linesRemoved` | integer | — |

---

## 4. `StoreDevinfoResult` — the 202 body

Confirmed field names (rendered docs + both clients read these):

```json
{
  "acceptedDevinfoEntities": {
    "<repositoryId>": {
      "commits":      ["<commitId>", ...],
      "branches":     ["<branchId>", ...],
      "pullRequests": ["<pullRequestId>", ...]
    }
  },
  "failedDevinfoEntities": {
    "<repositoryId>": {
      "commits":      [ { "id": "<commitId>", "errors": [ { "message": "...", "errorTraceId": "..." } ] } ],
      "branches":     [ ... ],
      "pullRequests": [ ... ]
    }
  },
  "unknownIssueKeys": ["ABC-999", ...],
  "unknownAssociations": [ { "associationType": "issueIdOrKeys", "values": ["ABC-999"] } ]
}
```

- **`acceptedDevinfoEntities`** — object keyed by `repositoryId`; each value is an
  object with `commits` / `branches` / `pullRequests` arrays of the entity ids
  that passed *format* validation and were queued. Presence here ≠ association
  succeeded (that is async).
- **`failedDevinfoEntities`** — same shape but each element carries an `errors`
  array (message + trace id). This is where a too-long field / bad enum shows up.
  (Some older renderings call this `rejectedDevinfoEntities`; current is
  `failedDevinfoEntities`.)
- **`unknownIssueKeys`** — issue keys referenced in `issueKeys` that Jira could not
  resolve to an issue in the site. The entity is still stored, just not linked.
- **`unknownAssociations`** — `Association` objects whose `values` Jira could not
  resolve. Same "stored but unlinked" semantics.

The bridge should treat `failedDevinfoEntities` as a hard problem to log/alert on,
and `unknownIssueKeys` / `unknownAssociations` as "issue key typo or issue not
created yet" — informational.

### When are associations (re)built?

- Association resolution runs **asynchronously after the 202**, on Jira's
  ingestion queue. There is no synchronous confirmation.
- A `POST /bulk` for an entity id that already exists **replaces** the stored
  entity iff the incoming `updateSequenceId` is greater (see §5). On replace, the
  **full `issueKeys` / `associations` set of the incoming payload is
  re-materialised** — i.e. Jira recomputes the issue links from the new payload;
  it does not merge with previously-stored keys. So an unchanged association set on
  a replace *does* re-assert the same links (harmless), and a *shrunk* set on a
  replace *removes* the dropped links.
- **Caveat observed by github-for-jira** (commit `332b5ae`, "ARC-2803 skip sending
  commit association for now"): commit-level `associations` processing was
  intentionally disabled by Atlassian's own client at one point due to a
  server-side issue; they fell back to `issueKeys` on commits. Treat commit
  `associations` as "supported but historically flaky"; `issueKeys` on commits is
  the safe path.
- If a re-`POST` with a **stale** `updateSequenceId` is sent, it is silently
  dropped (no error, not in `failedDevinfoEntities`) — this is the trap the
  bridge's "one id per submission" scheme risks.

---

## 5. `updateSequenceId` semantics

Verbatim (docs + mirror, consistent):

> *"Existing repository and entity data for the same ID will be replaced if the
> `updateSequenceId` of existing data is **less than** the incoming data.
> Updates with a lower or equal `updateSequenceId` than what is currently stored
> are ignored."*
> *"A monotonically increasing ID used to order updates."*

Consequences:

1. It is compared **per entity id** (per commit id, per branch id, per PR id) and
   separately for the repository object itself. There is **no global ordering** —
   each id has its own high-water mark.
2. `<=` is ignored, only `>` replaces. So re-sending the exact same
   `updateSequenceId` for an entity is a **no-op** (does not re-run association
   processing reliably).
3. The same commit SHA appears twice in one payload when it is both a standalone
   `commits[]` entry **and** a `branches[].lastCommit`. These share the entity id
   (`= SHA`). If both copies carry the **same** `updateSequenceId`, whichever Jira
   processes second is `<=` the first and is dropped — you get a race over which
   copy's `issueKeys`/`fileCount` wins.
4. `int64`. Wall-clock milliseconds (`int(time.time()*1000)`) is the universal
   convention and is what both reference clients use.
5. For **deletes**, `_updateSequenceId` is a "delete only data with
   `updateSequenceId <= X`" gate — pass a value ≥ everything stored to force the
   delete (i.e. "now" in ms).

**Design rule for the bridge:** every entity in a submission needs a value that is
(a) strictly greater than anything previously sent for that id, and (b) unique
across the two copies of a shared SHA. A monotonic `base + per-entity-index`
scheme derived from `int(time.time()*1000)` satisfies both.

---

## 6. `associations` vs `issueKeys`

- **They are MUTUALLY EXCLUSIVE on one entity.** Sending both on a `Commit`
  (and, by extension, `Branch` / `PullRequest`) fails the whole `POST /bulk`
  with `400` and, per entity:
  `issueKeys and associations are mutually exclusive. Either only specify
  issueKeys or pass issueKeys as an associationType.`
  (error key `devInformation.repository.commit.issueKeysOrAssociationsOrNone.invalid`).
  Observed against the live API 2026-09-02. An earlier version of this file
  wrongly recommended sending both.
- **`issueKeys: string[]`** — **DEPRECATED** in the rendered Cloud API reference
  (explicit badge on the `issueKeys` field of every devinfo entity). Still
  accepted; github-for-jira and gitlab still send it, and github-for-jira
  *disabled* commit `associations` deliberately once ("ARC-2803"). Available as
  a fallback.
- **`associations: [{associationType, values}]`** — the non-deprecated form. For
  issue linkage use `associationType: "issueIdOrKeys"` (accepts keys *or*
  numeric ids). The newer sibling APIs (deployments, security) use only this.
- Cap: the value list (whichever form) must not exceed **500** per entity;
  github-for-jira truncates at `ISSUE_KEY_API_LIMIT = 500`.
- Bridge: sends `associations` by default; `JIRA_SEND_ISSUE_KEYS=true` uses the
  deprecated `issueKeys` array instead (never both).

---

## 7. Rate limiting (OAuth 2.0 apps)

Source: `platform/rate-limiting/`. There is **no separately published per-second
quota for the `api.atlassian.com/jira/devinfo/...` endpoints** — they fall under
the general Jira Cloud app rate limits. Enforcement of the new points model
begins **2 March 2026**.

- **429** on any limit breach. Response headers:
  - `Retry-After: <seconds>` — *"wait this many seconds before reissuing. Reissuing
    early fails and returns the same or a longer `Retry-After`."*
  - `X-RateLimit-Limit`, `X-RateLimit-Remaining`
  - `X-RateLimit-Reset` — *"Only with 429. ISO-8601 timestamp when the window
    resets."*
  - `X-RateLimit-NearLimit: true` — when <20% capacity remains (on 2xx).
  - `RateLimit-Reason` — one of `jira-quota-global-based`,
    `jira-quota-tenant-based`, `jira-burst-based`, `jira-per-issue-on-write`.
- **Points model (hourly):** Tier-1 global pool 65,000 pts/hr across all tenants;
  Tier-2 per-tenant `100,000 + 10×users` (Standard) up to `150,000 + 30×users`
  (Enterprise), hard-capped at 500,000/hr. A write (`POST/PUT/PATCH/DELETE`) costs
  **1 point**. A `POST /bulk` with 400 commits is still 1 request = ~1 point, so the
  points model is not the binding constraint for the bridge.
- **Burst (per-second, per endpoint, per tenant):** default `POST` 100 req/s,
  `GET` 100 req/s, `PUT` 50 req/s, `DELETE` 50 req/s, token-bucket with a
  steady-state refill well below the burst ceiling. *"Design around the
  steady-state refill rate, not the burst buffer."* → the binding constraint for a
  chatty per-branch delete loop.
- **Recommended retry (verbatim guidance):** initial delay ~2 s; respect
  `Retry-After` as a *minimum*; double each retry (2, 4, 8, 16); multiply by a
  random factor 0.7–1.3 (jitter); cap at ~4 attempts.
- The bridge's `jira.py._request` (backoff `min(2**attempt, 30)` + `Retry-After`
  floor + `uniform(0,1)` jitter, `max_retries=4`) already matches this; only
  addition worth making is honouring `X-RateLimit-Reset`.

---

## 8. Adjacent DevOps APIs (shared limits & patterns)

All four share: the same base path shape (`.../jira/<type>/0.1/cloud/{cloudId}/bulk`),
the same OAuth-2LO auth, the same 202/400/401/403/413/429/503 response set, async
processing, a top-level `properties` (≤5) + `providerMetadata`, an
`updateSequenceNumber`/`updateSequenceId` "replace iff greater" rule, and a
`.../bulkByProperties` delete. They are **not documented as a single shared
rate-limit bucket**, but they are all "Jira Cloud app" traffic and count against
the same tenant points/burst pools per §7.

### Builds API — `POST /jira/builds/0.1/cloud/{cloudId}/bulk`
- Body: `builds[]` + `properties` + `providerMetadata`.
- `build`: `schemaVersion:"1.0"`, `pipelineId`, `buildNumber`, `updateSequenceNumber`
  (int, ms), `displayName`, `description`, `label`, `url`, `state`
  (`pending|in_progress|successful|failed|cancelled|unknown`), `lastUpdated`
  (ISO-8601), `issueKeys[]` **or** `references[]` (`{commit:{id,repositoryUri},
  ref:{name,uri}}`), `testInfo:{totalNumber,numberPassed,numberFailed,numberSkipped}`.
- 202 body: `acceptedBuilds` / `rejectedBuilds` (`{pipelineId,buildNumber,errors[]}`)
  / `unknownIssueKeys` / `unknownAssociations`.

### Deployments API — `POST /jira/deployments/0.1/cloud/{cloudId}/bulk`
- Body: `deployments[]` + `properties` + `providerMetadata`.
- `deployment`: `schemaVersion`, `deploymentSequenceNumber`, `updateSequenceNumber`,
  `displayName`, `url`, `description`, `lastUpdated`, `label`, `state`
  (`unknown|pending|in_progress|cancelled|failed|rolled_back|successful`),
  `pipeline:{id,displayName,url}`, `environment:{id,displayName,type}` where
  **`type` ∈ `unmapped|development|testing|staging|production`**, and
  **`associations[]`** — this API leans on `associations` (types `issueIdOrKeys`
  and `serviceIdOrKeys`) rather than a bare `issueKeys`.
- *"each is validated individually prior to submission."*
- 202 body: `acceptedDeployments` / `rejectedDeployments` / `unknownIssueKeys` /
  `unknownAssociations`.

### Feature Flags API — `POST /jira/featureflags/0.1/cloud/{cloudId}/bulk`
- Body: `flags[]` + `properties` + `providerMetadata`.
- `flag`: `schemaVersion`, `id`, `key`, `updateSequenceId`, `displayName`,
  `issueKeys[]`, `summary:{url, status:{enabled, defaultValue, rollout:{percentage|rules}},
  details:[{url, lastUpdated, environmentKey, environmentType}]}`.
- Also has `DELETE /flag/{flagId}` and `DELETE /bulkByProperties`.
- 202 body: `acceptedFeatureFlags` / `failedFeatureFlags` / `unknownIssueKeys`.

### Security Information API — `POST /jira/security/1.0/cloud/{cloudId}/bulk`
- Note version **`1.0`**, not `0.1`.
- Body: `vulnerabilities[]` + `properties` + `providerMetadata`.
- `vulnerability`: `schemaVersion`, `id`, `updateSequenceNumber`, `containerId`,
  `displayName`, `description`, `url`, `type`, `introducedDate`, `lastUpdated`,
  `severity:{level: critical|high|medium|low}`, `identifiers[]`,
  `status` (`open|closed|ignored|unknown`), `additionalInfo`,
  **`associations[]`** (uses `issueIdOrKeys`; no bare `issueKeys`).
- 202 body: `acceptedVulnerabilities` / `failedVulnerabilities` /
  `unknownIssueKeys` / `unknownAssociations`.
- `DELETE /bulkByProperties`, `DELETE /{vulnerabilityId}`.

**Takeaways for the bridge:** the newer APIs (deployments, security) dropped the
bare `issueKeys` field entirely and use `associations` with `issueIdOrKeys` —
that is the direction of travel and the reason to add `associations` now, while
keeping `issueKeys` because devinfo (unlike deployments) still honours it and the
reference clients still send it.
